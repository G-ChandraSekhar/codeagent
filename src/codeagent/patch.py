"""Milestone 1 slice B / ADR 0006 patch.py-hardening slice: one
deliberately narrow, controlled patch operation — exact expected-text
replacement in a single file — plus a real, structurally-verified Git
commit as the checkpoint.

This is NOT the Milestone 2 patch engine. It supports **exactly one**
operation (enforced at construction) and nothing else — no add/delete/
rename, no multiple operations, no fuzzy matching, no general path
validation beyond what this one operation needs. Multiple operations
against the same file starting from the same original content would
overwrite one another; rather than build the bookkeeping to do that
safely, this is deferred to Milestone 2's atomic multi-file patch
engine, where it belongs.

Every check below — including approved-path authorization, see
`apply()` — runs to completion *before* anything is written to disk or
committed wherever possible, but not every failure is pre-write:

- A **pre-write validation failure** (`PATCH_VALIDATION_FAILED`,
  raised before the target file is ever opened for writing) leaves the
  worktree exactly as it was: no write, no `git add`, no commit.
- A **post-write failure** (`PATCH_APPLICATION_FAILED`, raised after
  the target file has already been written — including a staging,
  write-tree, commit, or commit-verification failure) leaves the
  disposable worktree in a genuinely mutated, uncommitted state. This
  module does **not** roll that mutation back in place; per ADR 0003's
  accepted recovery model, the caller must discard and recreate the
  worktree from the last accepted checkpoint rather than trust or
  resume a worktree after such a failure.

ADR 0006's patch.py-hardening slice adds, on top of the original slice
B behavior:

- Every Git invocation goes through `codeagent._git_safety.run_git`/
  `run_git_bounded`, carrying the hardened baseline (hook/fsmonitor/
  autocrlf/submodule-recursion neutralization, `--no-replace-objects` +
  `GIT_NO_REPLACE_OBJECTS=1`, `--literal-pathspecs` +
  `GIT_LITERAL_PATHSPECS=1`) on every call — never a bare `subprocess`
  invocation.
- Binary-safe, bounded I/O: the original file, the intended replacement,
  and the post-write observation are each read/written as raw bytes
  (never `Path.read_text`/`write_text`, which silently perform
  universal-newline translation) and bounded by `MAX_PATCH_BLOB_BYTES`.
- A clean-staged-index precondition before any write.
- The repository's Git object format is detected once and every OID
  this module reuses (HEAD, staged entries, tree entries, the new
  commit's parent/tree/blob) is validated against it before reuse.
- The full six-attribute ADR 0006 safety check (`filter`, `text`,
  `eol`, `ident`, `working-tree-encoding`, legacy `crlf`) is applied to
  the patch target itself — both the staged (cached) and working-tree
  views, since an unstaged `.gitattributes` edit still governs what
  `git add` does — immediately before the write and again immediately
  before `add`, with filter-driver enumeration freshly re-run
  immediately before each of `add`/`write-tree`/`commit` (never one
  shared snapshot reused across all three).
- **`.gitattributes` patch targets (top-level or nested) are currently
  refused categorically**, before any read, write, stage, or commit —
  see the paragraph below.
- A pre-mutation check that every object the current staged index
  references is actually present locally (`check_index_blob_availability`),
  guarding against a partial clone that never fetched a blob the patch
  target's tree depends on.
- Strict commit acceptance: the expected parent, tree, and target
  blob/mode are captured (via path-safe `lookup_staged_entry` and an
  explicit `write-tree`, not implicitly trusted from `commit`'s own
  exit code) before `commit` runs once; HEAD is observed exactly once
  afterward and structurally verified (single expected parent, matching
  tree, matching target entry, exact-byte and SHA-256 match on the
  retrieved blob) before being accepted — an unchanged HEAD is failure
  even if commit reported success, and a changed-but-nonmatching HEAD
  is treated as contaminated/unconfirmed, never as success.

`.gitattributes` patch targets are refused, not partially supported: a
real, reproduced probe (ADR 0006 Amendment 3) showed that a nested
`.gitattributes` file's own attribute classification can be masked by
that same file's own staged content (a parent directory's rule for the
nested file's path can be silently shadowed by a self-referential rule
inside the nested file itself), so there is currently no trusted way
to validate a `.gitattributes` target's own safety independently of
the content being validated. `apply()` therefore refuses any
`.gitattributes` target (top-level or nested) with
`PATCH_UNSUPPORTED_GIT_SUBSTRATE` before any mutation.
`_check_repository_wide_attribute_safety` is preserved, unused, for
potential future reuse once a trusted independent-layer inspection
mechanism exists — see its own docstring for why it does not by itself
close this gap.

Persisted error messages are sanitized: no raw Git stderr, no absolute
filesystem paths, no repository content, and any caller-supplied path
value is stripped of non-printable characters and length-bounded before
inclusion. See `_safe_path_fragment`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from codeagent import _git_safety
from codeagent.controller import PatchResult
from codeagent.errors import ErrorCode, OperationalError

_MAX_MESSAGE_PATH_LENGTH = 200

_COMMIT_IDENTITY_ARGS = (
    "-c",
    "commit.gpgSign=false",
    "-c",
    "user.name=CodeAgent",
    "-c",
    "user.email=codeagent@example.invalid",
)


def _safe_path_fragment(value: str) -> str:
    """Render a caller-supplied path value safely for inclusion in a
    persisted message: strip non-printable characters (defends against
    log-injection via control characters) and bound the length."""
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in value)
    if len(cleaned) > _MAX_MESSAGE_PATH_LENGTH:
        cleaned = cleaned[:_MAX_MESSAGE_PATH_LENGTH] + "...(truncated)"
    return cleaned


@dataclass(frozen=True)
class PatchOperation:
    """Replace exactly one occurrence of `expected_text` with `new_text`
    in the file at `relative_path` (relative to the worktree root)."""

    relative_path: str
    expected_text: str
    new_text: str


class _ValidationFailure(Exception):
    """A pre-write refusal: nothing has been mutated. Maps to
    `ErrorCode.PATCH_VALIDATION_FAILED`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _ApplicationFailure(Exception):
    """A post-write refusal or failure: the filesystem has already been
    mutated. Maps to `ErrorCode.PATCH_APPLICATION_FAILED`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _SubstrateFailure(Exception):
    """The repository's Git substrate itself is not one this hardened
    policy can safely operate on, independent of this patch proposal.
    Maps to `ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _ObjectsUnavailableFailure(Exception):
    """A required repository object could not be confirmed present or
    retrieved intact. Maps to `ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _validate_path(worktree: Path, relative_path: str) -> Path:
    if not relative_path:
        raise _ValidationFailure("relative_path must be nonempty")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise _ValidationFailure(
            f"relative_path must not be absolute: {_safe_path_fragment(relative_path)!r}"
        )
    if any(part == ".." for part in candidate.parts):
        raise _ValidationFailure(
            f"relative_path must not contain '..': {_safe_path_fragment(relative_path)!r}"
        )

    # `Path.resolve()`/`is_symlink()`/`is_file()` can each raise `OSError`
    # (e.g. a permission failure, `ELOOP` from a symlink cycle) *or*
    # `RuntimeError` (pathlib's own symlink-loop detection, which some
    # platforms/Python versions raise instead of `OSError`) — both
    # commonly embed the absolute filesystem path in their message.
    # Never let either raw exception (or its message) reach a persisted
    # result; `from None` also suppresses it from the exception chain
    # so it cannot resurface via `__cause__`/`__context__` either.
    try:
        resolved_worktree = worktree.resolve()
        target = (resolved_worktree / candidate).resolve()
    except (OSError, RuntimeError):
        raise _ValidationFailure(
            f"relative_path could not be resolved: {_safe_path_fragment(relative_path)!r}"
        ) from None
    if not (target == resolved_worktree or resolved_worktree in target.parents):
        raise _ValidationFailure(
            f"relative_path resolves outside the worktree: {_safe_path_fragment(relative_path)!r}"
        )

    # Reject if any existing path component is a symlink — a symlink
    # partway along the path could resolve on-disk writes outside the
    # worktree even though the textual path looks contained.
    probe = resolved_worktree
    for part in candidate.parts:
        probe = probe / part
        try:
            is_symlink = probe.is_symlink()
        except (OSError, RuntimeError):
            raise _ValidationFailure(
                f"path could not be inspected: {_safe_path_fragment(relative_path)!r}"
            ) from None
        if is_symlink:
            raise _ValidationFailure(
                f"path escapes through a symlink: {_safe_path_fragment(relative_path)!r}"
            )
        if probe == target:
            break

    try:
        target_is_file = target.is_file()
    except (OSError, RuntimeError):
        raise _ValidationFailure(
            f"target file could not be inspected: {_safe_path_fragment(relative_path)!r}"
        ) from None
    if not target_is_file:
        raise _ValidationFailure(
            f"target file does not exist: {_safe_path_fragment(relative_path)!r}"
        )

    return target


def _read_bounded_bytes(target: Path, relative_path: str) -> bytes:
    """Binary-safe, bounded read: never `Path.read_text` (which
    performs universal-newline translation independent of any Git
    attribute) and never an unbounded `Path.read_bytes()` for
    patch-controlled content."""
    try:
        with target.open("rb") as f:
            raw = f.read(_git_safety.MAX_PATCH_BLOB_BYTES + 1)
    except OSError:
        raise _ValidationFailure(
            f"target file could not be read: {_safe_path_fragment(relative_path)!r}"
        ) from None
    if len(raw) > _git_safety.MAX_PATCH_BLOB_BYTES:
        raise _ValidationFailure(
            f"target file exceeds the {_git_safety.MAX_PATCH_BLOB_BYTES}-byte size limit: "
            f"{_safe_path_fragment(relative_path)!r}"
        )
    return raw


def _is_gitattributes_path(relative_path: str) -> bool:
    """True for both a top-level and a nested `.gitattributes` target
    (e.g. `sub/.gitattributes`) — either can change other tracked
    paths' attribute classification, not just its own directory's."""
    return Path(relative_path).name == ".gitattributes"


def _check_attribute_safety(
    worktree: Path, relative_path: str, *, cached: bool, description: str
) -> None:
    """Check every ADR 0006 safety attribute (`filter`, `text`, `eol`,
    `ident`, `working-tree-encoding`, and legacy `crlf`) for
    `relative_path`, not just `filter`: any live content-fidelity
    transformation (finding 7's `eol=crlf`, `ident` keyword expansion,
    a `working-tree-encoding` re-encoding) is exactly as unsafe here as
    an unsafe filter driver, and `filter`'s classification remains
    driver-name-aware via `AttributeRecord.is_safe`.

    Any `GitSafetyError` from the underlying enumeration/inspection
    calls (a timeout, malformed check-attr output, an exceeded bound)
    is translated to `_ValidationFailure` here, never left to escape
    as a raw, unstructured exception — the caller (pre-write context
    directly, pre-add context via its own wrapping) decides the final
    `ErrorCode`."""
    try:
        neutralization = _git_safety.enumerate_filter_neutralization(worktree)
        records = _git_safety.check_attributes_for_paths(worktree, [relative_path], cached=cached)
    except _git_safety.GitSafetyError as exc:
        raise _ValidationFailure(
            f"the target's attribute safety could not be verified ({description}): "
            f"{_safe_path_fragment(relative_path)!r}"
        ) from exc
    unsafe = [
        record
        for record in records
        if not record.is_safe(configured_driver_names=neutralization.driver_names)
    ]
    if unsafe:
        attribute_names = ", ".join(sorted({record.attribute for record in unsafe}))
        raise _ValidationFailure(
            f"the target has an unsafe attribute ({attribute_names}) ({description}): "
            f"{_safe_path_fragment(relative_path)!r}"
        )


class GitPatchApplier:
    """Applies exactly one PatchOperation inside a real worktree and
    commits it as the checkpoint. Constructed with the operation to
    apply — see module docstring for why this is fixed at construction
    rather than accepting arbitrary patches at call time, and why it's
    exactly one rather than a tuple.
    """

    def __init__(self, worktree_path: Path, operations: tuple[PatchOperation, ...]) -> None:
        if len(operations) != 1:
            raise ValueError(
                "GitPatchApplier supports exactly one PatchOperation "
                f"(got {len(operations)}) — multi-operation transactional patching "
                "is Milestone 2 work, not partially implemented here"
            )
        self._worktree_path = Path(worktree_path)
        self._operation = operations[0]
        self._next_error_id_seq = 0

    def _next_error_id(self, run_id: str) -> str:
        self._next_error_id_seq += 1
        return f"{run_id}-patch-err-{self._next_error_id_seq}"

    def _result(self, run_id: str, code: ErrorCode, message: str) -> PatchResult:
        return PatchResult(
            success=False,
            operation_count=0,
            changed_paths=(),
            diff_bytes=0,
            commit_hash=None,
            error=OperationalError(
                code=code,
                error_id=self._next_error_id(run_id),
                message=message,
            ),
        )

    def apply(
        self, run_id: str, iteration: int, approved_paths: frozenset[str]
    ) -> PatchResult:
        """Apply the configured operation, or reject it, and report what
        actually happened.

        `approved_paths` is the *preventive* authorization boundary:
        the operation's target must be one of these paths — normally
        `PlanProposal.proposed_file_paths` — checked before any
        validation, read, write, or Git command runs at all. This is
        the primary enforcement; RunController may additionally check
        `PatchResult.changed_paths` against the same set afterward as
        defense in depth, not as the thing that actually prevents an
        unapproved write (by the time that check ran, the mutation
        would already have happened) — see controller.py's
        `_dispatch_apply_patch` docstring.
        """
        operation = self._operation

        if operation.relative_path not in approved_paths:
            return self._result(
                run_id,
                ErrorCode.PATCH_VALIDATION_FAILED,
                f"target path is not in the approved plan's file list: "
                f"{_safe_path_fragment(operation.relative_path)!r}",
            )

        try:
            object_format = self._preflight()
            target, new_bytes = self._validate_and_prepare(object_format)
            commit_hash, diff_bytes = self._write_stage_and_commit(
                run_id, iteration, target, new_bytes, object_format
            )
        except _ValidationFailure as exc:
            return self._result(run_id, ErrorCode.PATCH_VALIDATION_FAILED, exc.message)
        except _ApplicationFailure as exc:
            return self._result(run_id, ErrorCode.PATCH_APPLICATION_FAILED, exc.message)
        except _SubstrateFailure as exc:
            return self._result(run_id, ErrorCode.PATCH_UNSUPPORTED_GIT_SUBSTRATE, exc.message)
        except _ObjectsUnavailableFailure as exc:
            return self._result(
                run_id, ErrorCode.PATCH_REPOSITORY_OBJECTS_UNAVAILABLE, exc.message
            )

        return PatchResult(
            success=True,
            operation_count=1,
            changed_paths=(operation.relative_path,),
            diff_bytes=diff_bytes,
            commit_hash=commit_hash,
        )

    # ------------------------------------------------------------------
    # Pre-write: substrate detection, object availability, read+compute
    # ------------------------------------------------------------------

    def _preflight(self) -> _git_safety.ObjectFormat:
        try:
            _git_safety.check_git_preflight()
        except _git_safety.GitSafetyError as exc:
            raise _SubstrateFailure(
                "the local git installation does not meet CodeAgent's hardened policy "
                "requirements"
            ) from exc
        try:
            return _git_safety.detect_object_format(self._worktree_path)
        except _git_safety.GitSafetyError as exc:
            raise _SubstrateFailure(
                "the repository's git object format could not be determined or is unsupported"
            ) from exc

    def _check_clean_baseline(self) -> None:
        try:
            result = _git_safety.run_git(
                self._worktree_path, "diff", "--cached", "--quiet", "HEAD", "--"
            )
        except _git_safety.GitSafetyError as exc:
            raise _SubstrateFailure(
                "the repository's staged-index cleanliness could not be determined"
            ) from exc
        if result.returncode == 0:
            return
        if result.returncode == 1:
            raise _ValidationFailure(
                "the worktree already has staged changes before patch application"
            )
        raise _SubstrateFailure(
            "the repository's staged-index cleanliness could not be determined"
        )

    def _validate_and_prepare(
        self, object_format: _git_safety.ObjectFormat
    ) -> tuple[Path, bytes]:
        operation = self._operation
        if not operation.expected_text:
            raise _ValidationFailure("expected_text must be nonempty")
        if operation.expected_text == operation.new_text:
            raise _ValidationFailure(
                "expected_text and new_text must differ — this would be a no-op replacement"
            )

        if _is_gitattributes_path(operation.relative_path):
            raise _SubstrateFailure(
                "a .gitattributes patch target is not currently supported by this hardened "
                "patch engine"
            )

        target = _validate_path(self._worktree_path, operation.relative_path)
        self._check_clean_baseline()

        try:
            _git_safety.check_index_blob_availability(
                self._worktree_path, object_format=object_format
            )
        except _git_safety.GitSafetyError as exc:
            if exc.reason is _git_safety.GitSafetyFailure.OBJECT_MISSING:
                raise _ObjectsUnavailableFailure(
                    "the repository is missing an object referenced by its staged index"
                ) from exc
            raise _SubstrateFailure(
                "the repository's staged-index object availability could not be verified"
            ) from exc

        raw = _read_bounded_bytes(target, operation.relative_path)
        try:
            original_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _ValidationFailure(
                f"target file is not valid UTF-8: {_safe_path_fragment(operation.relative_path)!r}"
            ) from None

        occurrences = original_text.count(operation.expected_text)
        if occurrences == 0:
            raise _ValidationFailure(
                f"expected_text not found in {_safe_path_fragment(operation.relative_path)!r}"
            )
        if occurrences > 1:
            raise _ValidationFailure(
                f"expected_text matches {occurrences} times in "
                f"{_safe_path_fragment(operation.relative_path)!r}, ambiguous — "
                "must match exactly once"
            )

        new_text = original_text.replace(operation.expected_text, operation.new_text, 1)
        try:
            new_bytes = new_text.encode("utf-8")
        except UnicodeEncodeError:
            # Caller-provided (model-proposed) new_text can contain an
            # unpaired UTF-16 surrogate — a legal Python str character
            # that is not encodable to UTF-8. Never let the raw
            # exception (which can embed the offending character) reach
            # a persisted result.
            raise _ValidationFailure(
                f"the intended replacement is not valid UTF-8: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            ) from None
        if len(new_bytes) > _git_safety.MAX_PATCH_BLOB_BYTES:
            raise _ValidationFailure(
                f"the intended replacement exceeds the "
                f"{_git_safety.MAX_PATCH_BLOB_BYTES}-byte size limit: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            )

        self._check_attribute_pair(operation.relative_path, stage="pre-write")
        return target, new_bytes

    def _check_attribute_pair(self, relative_path: str, *, stage: str) -> None:
        _check_attribute_safety(
            self._worktree_path, relative_path, cached=True, description=f"{stage}, cached"
        )
        _check_attribute_safety(
            self._worktree_path, relative_path, cached=False, description=f"{stage}, working tree"
        )

    # ------------------------------------------------------------------
    # Write, stage, write-tree, commit, and structural acceptance
    # ------------------------------------------------------------------

    def _write_stage_and_commit(
        self,
        run_id: str,
        iteration: int,
        target: Path,
        new_bytes: bytes,
        object_format: _git_safety.ObjectFormat,
    ) -> tuple[str, int]:
        operation = self._operation

        try:
            with target.open("wb") as f:
                f.write(new_bytes)
        except OSError:
            raise _ApplicationFailure(
                f"failed to write the patched file: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            ) from None

        # Everything from here on has already mutated the filesystem —
        # any further refusal is a post-write PATCH_APPLICATION_FAILED,
        # never PATCH_VALIDATION_FAILED.
        try:
            observed = _read_bounded_bytes(target, operation.relative_path)
        except _ValidationFailure as exc:
            raise _ApplicationFailure(exc.message) from exc
        if observed != new_bytes:
            raise _ApplicationFailure(
                f"the file on disk after writing does not match the intended content: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            )

        try:
            self._check_attribute_pair(operation.relative_path, stage="pre-add")
        except _ValidationFailure as exc:
            raise _ApplicationFailure(exc.message) from exc

        expected_parent = self._observe_head(object_format)

        # Filter enumeration is freshly re-run immediately before each
        # of add/write-tree/commit — never one shared snapshot — so this
        # never widens the accepted, narrowly-scoped filter-driver race.
        self._reenumerate_and_recheck_cached(operation.relative_path, stage="pre-add")
        try:
            result = _git_safety.run_git(
                self._worktree_path, "add", "--", operation.relative_path
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("failed to stage the patched file") from exc
        if result.returncode != 0:
            raise _ApplicationFailure("failed to stage the patched file")

        try:
            staged_entry = _git_safety.lookup_staged_entry(
                self._worktree_path, operation.relative_path, object_format=object_format
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                f"the staged entry could not be confirmed: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            ) from exc
        if staged_entry.mode != "100644" and staged_entry.mode != "100755":
            raise _ApplicationFailure(
                f"the staged entry is not a regular file: "
                f"{_safe_path_fragment(operation.relative_path)!r}"
            )

        try:
            scope = _git_safety.run_git(
                self._worktree_path, "diff", "--cached", "--name-only", "-z", "HEAD", "--"
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("failed to determine the staged change scope") from exc
        if scope.returncode != 0:
            raise _ApplicationFailure("failed to determine the staged change scope")
        staged_paths = [p for p in scope.stdout.split("\0") if p != ""]
        if staged_paths != [operation.relative_path]:
            raise _ApplicationFailure(
                "the staged change scope is not exactly the approved patch target"
            )

        try:
            diff_result = _git_safety.run_git(
                self._worktree_path, "diff", "--no-color", "--cached"
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("failed to compute the patch diff") from exc
        if diff_result.returncode != 0:
            raise _ApplicationFailure("failed to compute the patch diff")
        diff_bytes = len(diff_result.stdout.encode("utf-8", "surrogateescape"))

        self._reenumerate_and_recheck_cached(operation.relative_path, stage="pre-write-tree")
        try:
            write_tree_result = _git_safety.run_git(self._worktree_path, "write-tree")
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("failed to compute the write-tree result") from exc
        if write_tree_result.returncode != 0:
            raise _ApplicationFailure("failed to compute the write-tree result")
        expected_tree = write_tree_result.stdout.strip()
        try:
            _git_safety.validate_oid(object_format, expected_tree)
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                "git write-tree reported a malformed tree object id"
            ) from exc

        self._reenumerate_and_recheck_cached(operation.relative_path, stage="pre-commit")
        # The commit's own reported exit code — and even a raised
        # GitSafetyError from the invocation itself (e.g. a timeout) —
        # is deliberately not branched on here: it is neither trusted
        # on success (HEAD must actually move and structurally verify)
        # nor treated as dispositive on failure (a reported failure or
        # an execution failure, with an exact structural match below,
        # is still accepted as success). HEAD observation below is
        # what actually decides the outcome.
        try:
            _git_safety.run_git(
                self._worktree_path,
                *_COMMIT_IDENTITY_ARGS,
                "commit",
                "--quiet",
                "-m",
                f"codeagent: apply patch (iteration {iteration})",
            )
        except _git_safety.GitSafetyError:
            pass

        observed_head = self._observe_head(object_format)
        if observed_head == expected_parent:
            # HEAD did not move — failure regardless of what commit
            # reported.
            raise _ApplicationFailure("the checkpoint commit did not advance HEAD")

        self._verify_commit(
            observed_head,
            expected_parent=expected_parent,
            expected_tree=expected_tree,
            expected_entry=staged_entry,
            expected_bytes=new_bytes,
            object_format=object_format,
        )

        return observed_head, diff_bytes

    def _observe_head(self, object_format: _git_safety.ObjectFormat) -> str:
        try:
            result = _git_safety.run_git(self._worktree_path, "rev-parse", "HEAD")
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("failed to resolve the current commit hash") from exc
        if result.returncode != 0:
            raise _ApplicationFailure("failed to resolve the current commit hash")
        head = result.stdout.strip()
        try:
            return _git_safety.validate_oid(object_format, head)
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure("git reported a malformed commit object id") from exc

    def _reenumerate_and_recheck_cached(self, relative_path: str, *, stage: str) -> None:
        try:
            _check_attribute_safety(
                self._worktree_path, relative_path, cached=True, description=stage
            )
        except _ValidationFailure as exc:
            raise _ApplicationFailure(exc.message) from exc

    def _check_repository_wide_attribute_safety(self) -> None:
        """**Not currently called from `apply()` — not a complete
        control.** `GitPatchApplier.apply()` categorically refuses any
        `.gitattributes` patch target (top-level or nested) before any
        mutation (see `_validate_and_prepare`), because a real,
        reproduced probe (this session, macOS/Git 2.54.0) showed that a
        nested `.gitattributes` file's own content can *mask* a parent
        directory's rule for the nested file's own path: a top-level
        `.gitattributes` assigning `sub/.gitattributes filter=parent`
        was silently shadowed by a self-referential rule inside
        `sub/.gitattributes` itself (`.gitattributes -filter`), and
        `check-attr --cached` reported `unset` (safe) for
        `sub/.gitattributes` despite the parent's unsafe assignment.
        This means a `.gitattributes` target's own attribute-safety
        classification is not trustworthy when it is derived (even in
        part) from that same file's own staged content — exactly the
        case this method's single-target check and this method's
        repository-wide re-check both depend on. There is currently no
        trusted mechanism in this module to inspect a path's governing
        parent/`.git/info/attributes`/global attribute layers
        independently of the target file's own content.

        This method is preserved, unused, for potential future reuse
        once such a trusted mechanism exists — it correctly enumerates
        every tracked path under `list_tracked_paths`'s fixed bounds
        and re-checks the full ADR 0006 attribute set for all of them
        against the current staged index, refusing as
        `PATCH_UNSUPPORTED_GIT_SUBSTRATE` if the repository's shape
        exceeds what it can safely re-validate — but by itself it does
        **not** close the masking gap above, and must not be described
        or relied on as a complete `.gitattributes` safety control.
        """
        try:
            tracked_paths = _git_safety.list_tracked_paths(self._worktree_path)
        except _git_safety.GitSafetyError as exc:
            if exc.reason in (
                _git_safety.GitSafetyFailure.TRACKED_PATH_COUNT_EXCEEDED,
                _git_safety.GitSafetyFailure.TRACKED_PATH_TOO_LONG,
            ):
                raise _SubstrateFailure(
                    "the repository's tracked-path shape exceeds what this hardened patch "
                    "engine can safely re-validate for a .gitattributes change"
                ) from exc
            raise _ApplicationFailure(
                "the repository's tracked paths could not be listed to re-validate "
                ".gitattributes safety"
            ) from exc
        if not tracked_paths:
            return

        try:
            neutralization = _git_safety.enumerate_filter_neutralization(self._worktree_path)
            records = _git_safety.check_attributes_for_paths(
                self._worktree_path, tracked_paths, cached=True
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                "tracked-path attribute safety could not be re-validated after staging "
                ".gitattributes"
            ) from exc

        if any(
            not record.is_safe(configured_driver_names=neutralization.driver_names)
            for record in records
        ):
            raise _ApplicationFailure(
                "staging the .gitattributes change makes at least one other tracked path unsafe"
            )

    def _verify_commit(
        self,
        observed_head: str,
        *,
        expected_parent: str,
        expected_tree: str,
        expected_entry: _git_safety.IndexEntry,
        expected_bytes: bytes,
        object_format: _git_safety.ObjectFormat,
    ) -> None:
        try:
            header = _git_safety.read_commit_header(
                self._worktree_path, observed_head, object_format=object_format
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                "the new commit's structure could not be confirmed"
            ) from exc

        if header.parents != (expected_parent,):
            raise _ApplicationFailure(
                "the new commit does not have exactly the expected single parent — "
                "unconfirmed, treated as contaminated"
            )
        if header.tree != expected_tree:
            raise _ApplicationFailure(
                "the new commit's tree does not match the expected write-tree result — "
                "unconfirmed, treated as contaminated"
            )

        try:
            tree_entry = _git_safety.lookup_tree_entry(
                self._worktree_path,
                observed_head,
                self._operation.relative_path,
                object_format=object_format,
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                "the committed target entry could not be confirmed"
            ) from exc
        if tree_entry is None:
            raise _ApplicationFailure(
                "the committed target entry is missing — unconfirmed, treated as contaminated"
            )
        if tree_entry.mode != expected_entry.mode or tree_entry.oid != expected_entry.oid:
            raise _ApplicationFailure(
                "the committed target entry's mode or object id does not match the staged "
                "entry — unconfirmed, treated as contaminated"
            )

        try:
            blob = _git_safety.read_blob_bytes(
                self._worktree_path, tree_entry.oid, object_format=object_format
            )
        except _git_safety.GitSafetyError as exc:
            raise _ApplicationFailure(
                "the committed target's content could not be retrieved for verification"
            ) from exc
        if blob != expected_bytes or hashlib.sha256(blob).digest() != hashlib.sha256(
            expected_bytes
        ).digest():
            raise _ApplicationFailure(
                "the committed target's content does not exactly match the intended "
                "replacement — unconfirmed, treated as contaminated"
            )
