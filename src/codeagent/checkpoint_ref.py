"""Milestone 2, slice A: the trusted checkpoint-ref primitive.

Implements exactly the ref mechanics of ADR 0003 Amendment 1 (Accepted
2026-09-15) — nothing else. Each lifecycle owns exactly one hidden ref,
`refs/codeagent/runs/<lifecycle_id>/checkpoint`, which is observed and
changed only through compare-and-swap `git update-ref --no-deref`
against the *trusted source repository*.

Scope boundary, deliberately narrow:

- This module performs trusted ref observation and mutation only. It
  does **not** write ADR 0004's durable `checkpoint_ref` transition
  record (`creating`/`advancing`/`removing` intent). The lifecycle store
  in the next slice writes that intent around these calls, and ADR 0004
  §8's dead-run reconciliation consumes it.
- It is not wired into `RunController`, `GitWorktree`, patch
  application, cancellation, or any CLI yet.
- It never decides run outcomes and never constructs an
  `OperationalError`. Failures raise `CheckpointRefError` carrying a
  categorical `reason`; the integrating slice translates one failure
  occurrence into exactly one `OperationalError` with one `error_id`,
  preserving the "one occurrence, one error" rule ADR 0003 Amendment 1
  requires. This mirrors `codeagent.workspace.GitWorktree`'s existing
  split, where the Git-facing module stays independent of the error
  taxonomy.

Safety properties this module enforces:

- **Trusted repository only.** A linked worktree is refused at
  construction: in a linked worktree Git reports a `--git-dir` that
  differs from `--git-common-dir`, so a disposable worktree's `.git`
  file can never be the thing whose refs are mutated.
- **Owned namespace only.** `lifecycle_id` must match
  `^[0-9a-f]{32}$`, so a caller cannot escape or reshape the owned
  namespace, and the ref name contains no user-controlled text beyond
  those 32 hex characters.
- **Object-format correctness, no SHA-1 assumption.** The repository's
  object format is discovered with `git rev-parse --show-object-format`
  and must be `sha1` (40 hex) or `sha256` (64 hex). Every object ID is
  validated against it. The correctly sized all-zero OID is used only
  as a Git *argument* for create; absence is represented as
  `RefObservation(present=False, oid=None)`, never as a zero-OID value.
- **Never an unconditional mutation.** Every mutation passes an
  expected old value to `git update-ref`, so a concurrent writer's
  value is never overwritten — a losing compare-and-swap leaves the ref
  exactly as it was.
- **Symbolic refs are refused, including under a substitution race.**
  This is load-bearing, not advisory: `git update-ref --no-deref <ref>
  <new> <resolved-old>` on a symbolic ref *succeeds* and silently
  rewrites it into a regular ref, so compare-and-swap alone does not
  protect the owned name — and `verify` behaves the same way, so a
  single-shot transaction cannot express "must be a direct ref". Every
  mutation therefore runs inside a `git update-ref --stdin`
  transaction: `start` → `option no-deref` → the update → `prepare`
  (which validates the old value and *locks* the ref) → the ref is
  re-observed **while locked** → `commit` only if it is still a direct
  ref at the expected value, otherwise `abort`. A symbolic ref
  substituted at any point before `prepare` is refused and left
  symbolic; after `prepare` no other process can change it.
- **Environment cannot redirect Git.** Every invocation runs with all
  `GIT_*` variables removed from the environment, so a hostile or stale
  `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`,
  `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_REPLACE_REF_BASE`, or
  `GIT_CONFIG_*` injection cannot point an operation at another
  repository, object store, or ref namespace. Only `git -C <trusted
  repo>` selects the repository.
- **Repository configuration cannot run host code.** Clearing `GIT_*`
  does *not* disable hooks: `core.hooksPath` in repository, global, or
  system configuration still makes a ref mutation execute a
  `reference-transaction` hook on the host. Every invocation here
  therefore passes `-c core.hooksPath=/dev/null`, which is
  command-line configuration and so outranks every configuration file.
  `/dev/null` is not a directory on the supported macOS and Linux
  platforms, so no hook is ever found. This covers **this module only**
  — see `docs/threat-model.md` T-M3 for the same exposure in
  `patch.py` and `workspace.py`, which is not fixed here.
- **Fail closed, and never guess an outcome.** A failed or unparseable
  observation is never treated as absence. After any reported mutation
  failure or ambiguous result, the ref is re-observed and the outcome
  classified honestly: the intended state means it actually succeeded;
  the expected old state means it failed without advancing; anything
  else, or a symbolic ref, fails closed; and a failed observation stays
  explicitly unknown (`MUTATION_OUTCOME_UNKNOWN`) rather than being
  reported as unchanged.
- **The observed outcome is reported alongside the reason.** Every
  `CheckpointRefError` carries a `MutationOutcome`, because the reason
  alone is not sufficient to drive a write-ahead transition record: a
  timeout, launch failure, or protocol failure can accompany either a
  ref confirmed still in its pre-state (`UNCHANGED`, safe to collapse)
  or a ref whose state is unknown (`UNKNOWN`, never collapsible), and
  the reason is identical in both cases. The classification this module
  already performs is therefore published rather than discarded, so no
  caller needs a second observation to act on it.
  `TRANSACTION_CLEANUP_UNCONFIRMED` dominates every other
  classification and is always `UNKNOWN`: a child process that could
  not be confirmed dead may still hold the ref lock, so no observation
  taken around it is authoritative — whether it sees the intended
  value, the pre-state, an unexpected value, a symbolic ref, or fails
  outright. That original error is raised as the same object rather
  than being replaced by a `SYMBOLIC_REF` or `MUTATION_OUTCOME_UNKNOWN`
  that would misattribute the cause.
- **Sanitized errors.** Messages are fixed, categorical text plus the
  owned ref name and validated hex object IDs. Raw Git stderr, host
  paths, environment contents, and repository contents never appear.

Exercised against real repositories on both the `files` and `reftable`
ref backends, and on SHA-1 and SHA-256 object formats, wherever the
installed Git supports them (the tests skip with a precise reason
otherwise). Only Git porcelain is used; `.git` internals are never read
or written directly. What remains outside this guarantee is a writer
that bypasses Git's ref locking by editing `.git` internals itself,
which the threat model already places outside scope (assumption A4).
"""

from __future__ import annotations

import os
import re
import secrets
import selectors
import subprocess
import time
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

REF_NAMESPACE = "refs/codeagent/runs"
LIFECYCLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Bounded so a hung Git invocation cannot stall a caller indefinitely.
GIT_TIMEOUT_SECONDS = 30.0

# Tab-separated: a Git refname can contain neither a space nor an ASCII
# control character, so tabs make the three fields unambiguous even when
# %(symref) is empty (a space-separated format would collapse).
_OBSERVE_FORMAT = "%(objectname)\t%(symref)\t%(refname)"

# Command-line configuration outranks repository, global, and system
# configuration, so this cannot be re-enabled by the repository being
# operated on. Ref mutations otherwise run that repository's
# `reference-transaction` hook on the host.
_NO_HOOKS_ARGS: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null")


@unique
class ObjectFormat(str, Enum):
    """The repository's Git object format. Only these two exist today;
    anything else fails closed rather than being guessed at."""

    SHA1 = "sha1"
    SHA256 = "sha256"

    @property
    def hex_length(self) -> int:
        return 40 if self is ObjectFormat.SHA1 else 64

    @property
    def zero_oid(self) -> str:
        """The all-zero object ID of this format. Used only as a Git
        argument meaning "this ref must not exist yet" — never as a
        persisted or returned value for "no ref"."""
        return "0" * self.hex_length


@unique
class MutationOutcome(str, Enum):
    """What actually happened to the ref during a mutation attempt, as
    distinct from *why* the attempt failed (`CheckpointRefFailure`).

    A categorical failure reason alone cannot drive a write-ahead
    transition record: the same reason (a timeout, a launch failure, a
    protocol failure) can accompany either a ref that was confirmed
    still in its pre-state or a ref whose state could not be determined
    at all, and those two demand opposite handling. This enum carries
    the observed outcome alongside the reason so a caller never has to
    perform a second observation to find out — see
    `codeagent.checkpoint_session.CheckpointSession`, which branches on
    `(operation, outcome)` and never re-observes.

    - `APPLIED`: the ref was confirmed at the intended value. Signalled
      by a mutation returning normally, never by an error.
    - `UNCHANGED`: the ref was confirmed still in its pre-state, so the
      mutation demonstrably did not take effect.
    - `UNEXPECTED`: the ref was confirmed at some third direct value —
      neither the pre-state nor the intended state.
    - `SYMBOLIC`: the ref was confirmed to be a symbolic ref, which this
      module never accepts.
    - `UNKNOWN`: the outcome could not be confirmed. The conservative
      default: any path that has not positively established one of the
      above reports this, so an un-annotated failure fails closed
      rather than inviting a collapse it cannot justify.
    """

    APPLIED = "applied"
    UNCHANGED = "unchanged"
    UNEXPECTED = "unexpected"
    SYMBOLIC = "symbolic"
    UNKNOWN = "unknown"


@unique
class CheckpointRefFailure(str, Enum):
    """Categorical reason a checkpoint-ref operation failed. These are
    for callers to branch on; `CheckpointRefError.message` is for humans
    and must never be parsed."""

    GIT_EXECUTABLE_UNAVAILABLE = "git_executable_unavailable"
    GIT_COMMAND_TIMEOUT = "git_command_timeout"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    OBJECT_FORMAT_UNAVAILABLE = "object_format_unavailable"
    OBJECT_FORMAT_UNSUPPORTED = "object_format_unsupported"
    OBSERVATION_FAILED = "observation_failed"
    AMBIGUOUS_OBSERVATION = "ambiguous_observation"
    SYMBOLIC_REF = "symbolic_ref"
    UNEXPECTED_VALUE = "unexpected_value"
    COMPARE_AND_SWAP_REJECTED = "compare_and_swap_rejected"
    # The mutation's outcome could not be determined, because the ref
    # could not be observed afterwards. Never reported as "unchanged":
    # the integrating lifecycle layer must treat this as ambiguous and
    # leave its transition record in place (ADR 0004 §5).
    MUTATION_OUTCOME_UNKNOWN = "mutation_outcome_unknown"
    # The transaction's git child process could not be confirmed
    # terminated after bounded abort/kill/wait attempts. An OS process
    # cannot always be killed (e.g. stuck in uninterruptible I/O), so
    # this is reported explicitly rather than assuming the process — and
    # therefore the ref's lock — was actually released.
    TRANSACTION_CLEANUP_UNCONFIRMED = "transaction_cleanup_unconfirmed"


class CheckpointRefError(Exception):
    """A checkpoint-ref operation failed or was refused.

    `reason` is the stable, matchable identifier. `message` is already
    sanitized: fixed text plus the owned ref name and validated hex
    object IDs only. Callers that need an `OperationalError` translate
    this once, at the boundary that records the occurrence — this module
    deliberately does not know the error taxonomy (same split as
    `codeagent.workspace.GitWorktreeError`).

    `outcome` reports what happened to the ref itself (see
    `MutationOutcome`). It is deliberately a mutable attribute rather
    than a constructor-only value: a failure raised before the ref has
    been observed is annotated *in place* once the outcome becomes
    known, and re-raised as the very same object. Constructing a
    replacement exception instead would silently drop this occurrence's
    traceback and would require hand-copying `__cause__`,
    `__context__`, and `__suppress_context__` — the exact chaining
    state several existing tests assert on. Annotating preserves all of
    it for free.
    """

    def __init__(
        self,
        reason: CheckpointRefFailure,
        message: str,
        *,
        observed_oid: str | None = None,
        expected_oid: str | None = None,
        outcome: MutationOutcome = MutationOutcome.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        # Structured detail so a caller never has to parse `message`.
        self.observed_oid = observed_oid
        self.expected_oid = expected_oid
        # Conservative default: anything that has not positively
        # established an outcome reports UNKNOWN, which no caller may
        # collapse a transition record on.
        self.outcome = outcome


@dataclass(frozen=True)
class RefObservation:
    """What the owned ref is right now. Absence is `present=False` with
    `oid=None` — never a zero OID."""

    present: bool
    oid: str | None = None


def new_lifecycle_id() -> str:
    """Mint a fresh lifecycle id: 128 bits of `secrets` randomness as 32
    lowercase hex characters (ADR 0004 section 1).

    **Accepts no seed or input of any kind.** A lifecycle id must never
    be derived from `run_id`, a repository path, a task statement, or
    any other model- or operator-supplied text — those are public,
    attacker-influenced, or unsafe as a path/ref component, and ADR
    0004 rejects reusing them. This function offers no parameter
    through which such a value could be threaded.

    That is a property of *this function only*, not yet a
    system-wide guarantee: `CheckpointRef` still accepts any correctly
    shaped lifecycle id from any source. Slice 2B-2 must ensure the
    trusted composition root mints ids exclusively through here; until
    then the broader claim is not structurally enforced.

    The freshly generated value is validated against the same
    `LIFECYCLE_ID_RE` every consumer enforces, so this function can
    never become a way to introduce an id that `CheckpointRef` would
    later refuse.
    """
    lifecycle_id = secrets.token_hex(16)
    if not LIFECYCLE_ID_RE.fullmatch(lifecycle_id):
        raise RuntimeError("generated lifecycle id did not match the required format")
    return lifecycle_id


def git_environment() -> dict[str, str]:
    """The environment every Git invocation here runs with: the parent
    environment minus **every** `GIT_*` variable.

    Git's repository, worktree, common-dir, namespace, index, object
    store, alternates, replace-ref base, and command-line config
    injection are all selected by `GIT_*` variables, and a stale or
    hostile one silently redirects an otherwise correct `git -C <repo>`
    invocation into a different repository or namespace. Removing the
    whole prefix is the only way to cover them without maintaining a
    denylist that drifts as Git adds variables.

    `GIT_EXEC_PATH` is removed too. In an exotic installation that
    depends on it, Git fails loudly rather than operating on redirected
    state — the intended trade-off. Everything a subprocess needs to
    launch normally (`PATH`, `HOME`, locale, and so on) is preserved.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Structured argv only: never a shell, never a string-built
    command. Raises CheckpointRefError for failures to launch or a hung
    invocation; a nonzero exit is returned for the caller to classify."""
    try:
        return subprocess.run(
            ["git", *_NO_HOOKS_ARGS, "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=git_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckpointRefError(
            CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
            "a git command for the checkpoint ref did not finish within its time limit",
        ) from exc
    except OSError as exc:
        raise CheckpointRefError(
            CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE,
            "the git executable could not be launched",
        ) from exc


class CheckpointRef:
    """The one hidden ref owned by a single lifecycle.

    Construction validates the lifecycle id, refuses a linked worktree,
    and discovers the repository's object format — so every later call
    operates on a known-good repository and a fixed, owned ref name.
    """

    def __init__(self, source_repo_path: Path | str, lifecycle_id: str) -> None:
        if not isinstance(lifecycle_id, str) or not LIFECYCLE_ID_RE.fullmatch(lifecycle_id):
            # Deliberately does not echo the rejected value: it is
            # caller-supplied and must never reach a message.
            raise ValueError(
                "lifecycle_id must be exactly 32 lowercase hexadecimal characters"
            )
        repo_path = Path(source_repo_path).resolve()
        if not repo_path.is_dir():
            raise ValueError("source_repo_path must be an existing directory")

        self._repo_path = repo_path
        self._lifecycle_id = lifecycle_id
        self._ref_name = f"{REF_NAMESPACE}/{lifecycle_id}/checkpoint"
        self._require_trusted_repository()
        self._object_format = self._discover_object_format()

    @property
    def ref_name(self) -> str:
        return self._ref_name

    @property
    def lifecycle_id(self) -> str:
        return self._lifecycle_id

    @property
    def object_format(self) -> ObjectFormat:
        return self._object_format

    def _require_trusted_repository(self) -> None:
        """Refuse a linked worktree. `--git-dir` and `--git-common-dir`
        are identical in a repository's main worktree and differ in a
        linked one, which is how a disposable worktree is structurally
        excluded (ADR 0003 Amendment 1: never operate through a
        worktree's `.git` file). Relative results are resolved against
        the repository path rather than relying on a newer Git's
        `--path-format`."""
        result = _run_git(self._repo_path, "rev-parse", "--git-dir", "--git-common-dir")
        if result.returncode != 0:
            raise CheckpointRefError(
                CheckpointRefFailure.REPOSITORY_UNAVAILABLE,
                "the source repository could not be inspected as a git repository",
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 2:
            raise CheckpointRefError(
                CheckpointRefFailure.REPOSITORY_UNAVAILABLE,
                "git did not report both a git directory and a common git directory",
            )
        git_dir, common_dir = ((self._repo_path / line).resolve() for line in lines)
        if git_dir != common_dir:
            raise ValueError(
                "source_repo_path must be the trusted source repository, "
                "not a linked worktree"
            )

    def _discover_object_format(self) -> ObjectFormat:
        result = _run_git(self._repo_path, "rev-parse", "--show-object-format")
        if result.returncode != 0:
            raise CheckpointRefError(
                CheckpointRefFailure.OBJECT_FORMAT_UNAVAILABLE,
                "the repository's git object format could not be determined",
            )
        raw = result.stdout.strip()
        try:
            return ObjectFormat(raw)
        except ValueError as exc:
            raise CheckpointRefError(
                CheckpointRefFailure.OBJECT_FORMAT_UNSUPPORTED,
                "the repository's git object format is not a supported format "
                "(expected sha1 or sha256)",
            ) from exc

    def _validate_oid(self, name: str, value: str) -> str:
        expected_length = self._object_format.hex_length
        if (
            not isinstance(value, str)
            or len(value) != expected_length
            or not re.fullmatch(r"[0-9a-f]+", value)
        ):
            raise ValueError(
                f"{name} must be exactly {expected_length} lowercase hexadecimal "
                f"characters for this repository's {self._object_format.value} object format"
            )
        return value

    def observe(self) -> RefObservation:
        """Report whether the owned ref is absent or points directly at
        an object. Never follows a symbolic ref and never reports a
        failed inspection as absence."""
        result = _run_git(
            self._repo_path, "for-each-ref", f"--format={_OBSERVE_FORMAT}", self._ref_name
        )
        if result.returncode != 0:
            raise CheckpointRefError(
                CheckpointRefFailure.OBSERVATION_FAILED,
                f"the checkpoint ref {self._ref_name} could not be inspected",
                outcome=MutationOutcome.UNKNOWN,
            )

        records = [line for line in result.stdout.splitlines() if line.strip()]
        if not records:
            return RefObservation(present=False, oid=None)
        if len(records) != 1:
            raise CheckpointRefError(
                CheckpointRefFailure.AMBIGUOUS_OBSERVATION,
                f"git reported more than one record for the checkpoint ref {self._ref_name}",
                outcome=MutationOutcome.UNKNOWN,
            )

        fields = records[0].split("\t")
        if len(fields) != 3:
            raise CheckpointRefError(
                CheckpointRefFailure.OBSERVATION_FAILED,
                f"git reported an unreadable record for the checkpoint ref {self._ref_name}",
                outcome=MutationOutcome.UNKNOWN,
            )
        object_name, symref_target, refname = fields

        if refname != self._ref_name:
            raise CheckpointRefError(
                CheckpointRefFailure.AMBIGUOUS_OBSERVATION,
                f"git reported a different ref than the owned {self._ref_name}",
                outcome=MutationOutcome.UNKNOWN,
            )
        if symref_target:
            raise CheckpointRefError(
                CheckpointRefFailure.SYMBOLIC_REF,
                f"the checkpoint ref {self._ref_name} is a symbolic ref and is refused",
                outcome=MutationOutcome.SYMBOLIC,
            )
        if len(object_name) != self._object_format.hex_length or not re.fullmatch(
            r"[0-9a-f]+", object_name
        ):
            raise CheckpointRefError(
                CheckpointRefFailure.OBSERVATION_FAILED,
                f"git reported a malformed object id for the checkpoint ref {self._ref_name}",
                outcome=MutationOutcome.UNKNOWN,
            )
        return RefObservation(present=True, oid=object_name)

    def create(self, new_oid: str) -> None:
        """Create the owned ref from absence, compare-and-swap against
        the zero OID."""
        self._validate_oid("new_oid", new_oid)
        self._apply(
            update_line=f"update {self._ref_name} {new_oid} {self._object_format.zero_oid}",
            expected_before=RefObservation(present=False, oid=None),
            intended_after=RefObservation(present=True, oid=new_oid),
        )

    def advance(self, *, expected_old_oid: str, new_oid: str) -> None:
        """Move the owned ref from an expected old value to a new one,
        compare-and-swap against that old value."""
        self._validate_oid("expected_old_oid", expected_old_oid)
        self._validate_oid("new_oid", new_oid)
        if expected_old_oid == new_oid:
            raise ValueError("new_oid must differ from expected_old_oid")
        self._apply(
            update_line=f"update {self._ref_name} {new_oid} {expected_old_oid}",
            expected_before=RefObservation(present=True, oid=expected_old_oid),
            intended_after=RefObservation(present=True, oid=new_oid),
        )

    def delete(self, *, expected_oid: str) -> None:
        """Delete the owned ref, compare-and-swap against its expected
        value."""
        self._validate_oid("expected_oid", expected_oid)
        self._apply(
            update_line=f"delete {self._ref_name} {expected_oid}",
            expected_before=RefObservation(present=True, oid=expected_oid),
            intended_after=RefObservation(present=False, oid=None),
        )

    def _apply(
        self,
        *,
        update_line: str,
        expected_before: RefObservation,
        intended_after: RefObservation,
    ) -> None:
        """The only mutation path.

        1. Pre-check (precise refusal, no mutation attempted).
        2. `start`/`option no-deref`/<update>/`prepare` — Git validates
           the expected old value and locks the ref.
        3. Re-observe **while locked**: still a direct ref at the
           expected value, or `abort`. This is what closes the symbolic
           substitution race, since `prepare` blocks other writers.
        4. `commit`, then confirm the end state by observation.

        Any failure or ambiguity at any step is classified by observing
        the ref, never assumed.
        """
        self._require_state(expected_before, when="before the update")

        original: CheckpointRefError | None = None
        try:
            with _RefTransaction(self._repo_path) as transaction:
                transaction.begin(update_line)
                # Locked: no other process can change the ref now.
                locked = self.observe()
                if locked != expected_before:
                    transaction.abort()
                    self._raise_unexpected(locked, expected_before, when="while locked")
                transaction.commit()
        except CheckpointRefError as exc:
            if exc.reason in (
                CheckpointRefFailure.SYMBOLIC_REF,
                CheckpointRefFailure.UNEXPECTED_VALUE,
            ):
                raise
            # A reported failure or an ambiguous result. Keep the
            # precise cause: if the ref turns out unchanged, the caller
            # needs to know *why* (launch failure, timeout, protocol
            # failure), not merely that a compare-and-swap did not
            # take effect.
            original = exc

        self._classify_outcome(intended_after, expected_before, original)

    def _require_state(self, expected: RefObservation, *, when: str) -> None:
        """Refuses a symbolic ref (via observe) and any value other than
        the expected one."""
        observation = self.observe()
        if observation != expected:
            self._raise_unexpected(observation, expected, when=when)

    def _raise_unexpected(
        self, observed: RefObservation, expected: RefObservation, *, when: str
    ) -> None:
        if expected.present:
            detail = (
                f"is absent" if not observed.present else f"is at {observed.oid}"
            ) + f" but was expected at {expected.oid}"
        else:
            detail = f"already exists at {observed.oid} but was expected to be absent"
        raise CheckpointRefError(
            CheckpointRefFailure.UNEXPECTED_VALUE,
            f"the checkpoint ref {self._ref_name} {detail} ({when})",
            observed_oid=observed.oid,
            expected_oid=expected.oid,
            # The ref was successfully observed at a direct value that is
            # neither the pre-state nor the intended state.
            outcome=MutationOutcome.UNEXPECTED,
        )

    def _classify_outcome(
        self,
        intended_after: RefObservation,
        expected_before: RefObservation,
        original: CheckpointRefError | None = None,
    ) -> None:
        """Decide what actually happened by observing the ref, per
        ADR 0004's state distinctions. Returns normally only when the
        intended end state is observed.

        `original` is the categorical failure already reported by the
        transaction, if any. When the ref turns out unchanged, that
        cause is preserved rather than being flattened into a generic
        compare-and-swap rejection — and is annotated *in place* with
        `MutationOutcome.UNCHANGED` so the caller learns both the real
        cause and the confirmed outcome from one object, without a
        second observation.

        `TRANSACTION_CLEANUP_UNCONFIRMED` dominates every other
        classification and is handled first, before any observation is
        attempted. An unconfirmed child process may still hold Git's ref
        lock, so *no* observation taken here is authoritative — not one
        that sees the intended value, the pre-state, an unexpected
        value, a symbolic ref, or one that fails outright. Observing
        anyway would only invite the misreading that the observed value
        changed the answer. The original error is therefore raised as
        the same object with `UNKNOWN`, never replaced by a
        `SYMBOLIC_REF` or `MUTATION_OUTCOME_UNKNOWN` that would
        misattribute the cause.
        """
        if (
            original is not None
            and original.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED
        ):
            original.outcome = MutationOutcome.UNKNOWN
            raise original

        try:
            observed = self.observe()
        except CheckpointRefError as exc:
            if exc.reason is CheckpointRefFailure.SYMBOLIC_REF:
                # `exc` is re-raised as itself, so a bare `raise` alone
                # would not attach `original` as its cause. Chain it
                # explicitly when one exists; otherwise leave `exc`
                # exactly as observe() raised it.
                if original is not None:
                    raise exc from original
                raise
            # Cannot tell: explicitly unknown, never "unchanged".
            raise CheckpointRefError(
                CheckpointRefFailure.MUTATION_OUTCOME_UNKNOWN,
                f"the outcome of the update to the checkpoint ref {self._ref_name} could "
                "not be determined, because the ref could not be observed afterwards",
                expected_oid=intended_after.oid,
                outcome=MutationOutcome.UNKNOWN,
            ) from (original or exc)

        if observed == intended_after:
            return  # The mutation actually took effect.
        if observed == expected_before:
            if original is not None:
                # The real cause — launch failure, timeout, protocol
                # failure — is what the caller must see, now carrying
                # the confirmed outcome. Annotated in place and
                # re-raised as the same object, so this occurrence's
                # traceback, __cause__, __context__ and
                # __suppress_context__ all survive untouched.
                original.outcome = MutationOutcome.UNCHANGED
                raise original
            raise CheckpointRefError(
                CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                f"the compare-and-swap update of the checkpoint ref {self._ref_name} did "
                f"not take effect; it was observed still "
                + (f"at {expected_before.oid}" if expected_before.present else "absent"),
                observed_oid=observed.oid,
                expected_oid=intended_after.oid,
                outcome=MutationOutcome.UNCHANGED,
            )
        try:
            self._raise_unexpected(observed, intended_after, when="after the update")
        except CheckpointRefError as exc:
            raise exc from original


class _RefTransaction:
    """One `git update-ref --stdin` transaction, used so the owned ref
    can be verified while Git holds its lock.

    Git acknowledges each stage on stdout (`start: ok`, `prepare: ok`,
    `commit: ok`, `abort: ok`), which is what makes the locked window
    observable deterministically rather than by guessing at timing.
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path
        self._process: subprocess.Popen[str] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._buffer = ""
        self._finished = False

    def __enter__(self) -> "_RefTransaction":
        try:
            self._process = subprocess.Popen(
                [
                    "git",
                    *_NO_HOOKS_ARGS,
                    "-C",
                    str(self._repo_path),
                    "update-ref",
                    "--stdin",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Git's stderr is deliberately never retained or
                # reported (it names host paths), and nothing drains it
                # during the interactive transaction — a pipe could
                # therefore fill and deadlock the child while it holds
                # the ref lock. Discarding it removes that failure mode
                # entirely.
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=git_environment(),
            )
        except OSError as exc:
            raise CheckpointRefError(
                CheckpointRefFailure.GIT_EXECUTABLE_UNAVAILABLE,
                "the git executable could not be launched",
            ) from exc
        self._selector = selectors.DefaultSelector()
        os.set_blocking(self._process.stdout.fileno(), False)
        self._selector.register(self._process.stdout, selectors.EVENT_READ)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Always attempts to end the transaction, so Git releases the
        ref lock — but never claims that it did.

        A clean `abort` is attempted first; if anything about it fails
        — including a timeout waiting for its acknowledgement — the
        child is killed instead. Either way, the child's exit is
        confirmed with a bounded `wait`, retried once after a further
        `kill` if the first wait times out. An OS process cannot always
        be killed (e.g. stuck in uninterruptible I/O): if it still
        cannot be confirmed exited after these bounded attempts, that is
        raised as `TRANSACTION_CLEANUP_UNCONFIRMED` rather than silently
        returning as if the lock were known released. If the `with`
        body raised, Python's own exception chaining preserves it as
        this error's `__context__`; an `abort` failure that preceded the
        unconfirmed cleanup is preserved explicitly as `__cause__`.
        """
        process = self._process
        if process is None:
            return

        abort_failure: BaseException | None = None
        if not self._finished and process.poll() is None:
            try:
                self.abort()
            except BaseException as caught:  # noqa: BLE001 — cleanup must not depend on abort
                abort_failure = caught

        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:  # noqa: BLE001
                pass
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=GIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=GIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass

        if process.poll() is None:
            raise CheckpointRefError(
                CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED,
                "the git ref transaction process could not be confirmed terminated "
                "after bounded cleanup attempts; whether its checkpoint-ref lock was "
                "released is unknown",
            ) from abort_failure

    def begin(self, update_line: str) -> None:
        """Send the update and prepare it, which locks the ref."""
        self._send("start")
        self._expect("start: ok")
        self._send("option no-deref")
        self._send(update_line)
        self._send("prepare")
        self._expect("prepare: ok")

    def commit(self) -> None:
        self._send("commit")
        self._expect("commit: ok")
        self._finish()

    def abort(self) -> None:
        self._send("abort")
        self._expect("abort: ok")
        self._finish()

    def _finish(self) -> None:
        self._finished = True
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            if process.wait(timeout=GIT_TIMEOUT_SECONDS) != 0:
                raise CheckpointRefError(
                    CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                    "the git ref transaction did not complete successfully",
                )
        except subprocess.TimeoutExpired as exc:
            raise CheckpointRefError(
                CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
                "the git ref transaction did not finish within its time limit",
            ) from exc

    def _send(self, line: str) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CheckpointRefError(
                CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                "the git ref transaction ended before the update could be sent",
            ) from exc

    def _expect(self, acknowledgement: str) -> None:
        line = self._read_line()
        if line != acknowledgement:
            raise CheckpointRefError(
                CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                "git did not acknowledge the ref transaction stage",
            )

    def _read_line(self) -> str | None:
        """Read one acknowledgement line, bounded by the shared timeout.
        Returns None at end of stream (Git exited)."""
        process = self._process
        assert process is not None and process.stdout is not None
        selector = self._selector
        assert selector is not None
        deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
        while "\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckpointRefError(
                    CheckpointRefFailure.GIT_COMMAND_TIMEOUT,
                    "the git ref transaction did not respond within its time limit",
                )
            if not selector.select(timeout=remaining):
                continue
            try:
                chunk = os.read(process.stdout.fileno(), 4096).decode("utf-8", "replace")
            except BlockingIOError:
                continue
            except OSError as exc:
                raise CheckpointRefError(
                    CheckpointRefFailure.COMPARE_AND_SWAP_REJECTED,
                    "the git ref transaction could not be read",
                ) from exc
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split("\n", 1)
        return line
