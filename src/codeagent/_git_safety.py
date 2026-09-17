"""Shared Git-safety foundation for ADR 0006
(`docs/adr/0006-git-safety-policy-for-filters-hooks-and-content-fidelity.md`,
Accepted).

Scope boundary:

- This module owns the building blocks ADR 0006 specifies: Git >= 2.45
  / `--no-lazy-fetch` preflight, sanitized environment construction,
  the fixed hardened baseline argv, bounded filter-driver
  enumeration/neutralization, bounded/chunked/NUL-safe tracked-path
  listing and filter-attribute inspection, and the safe/unsafe
  attribute-state classification (including the driver-set-dependent
  `filter` rule).
- `src/codeagent/workspace.py` is wired into this module: every Git
  invocation in `GitWorktree` (worktree creation, index population,
  the pre-materialization filter-safety check, the hardened
  materializing checkout, and `snapshot_source()`'s hardened `status`)
  goes through `run_git`/`check_git_preflight`/
  `evaluate_tracked_filter_safety` here. This closes threat-model T-M3
  for workspace creation and source-repository inspection only.
- `patch.py`, the controller, `checkpoint_ref.py` integration,
  lifecycle storage, cancellation, reconciliation, and CLI/frontend
  work are **not** wired into this module yet — those remain later
  slices. `workspace.py`'s own module docstring records what its
  integration does and does not cover, including the pre-existing
  `__exit__` prune/rmtree fallback gap (ADR 0004 I2, not resolved by
  either module).
- It never decides run outcomes and never constructs an
  `OperationalError`. Failures raise `GitSafetyError` carrying a
  categorical `reason`; an integrating module translates one failure
  occurrence into its own categorical error (`workspace.py`'s
  `GitWorktreeError`) or, eventually, exactly one `OperationalError`,
  mirroring `codeagent.checkpoint_ref.CheckpointRefError`'s existing
  split.

Safety properties this module enforces:

- **Git >= 2.45 is required, verified two ways.** `check_git_preflight`
  parses `git --version` and requires the reported version to be
  `>= 2.45` — the version Git's global `--no-lazy-fetch` option was
  introduced in (ADR 0006 §6) — and additionally proves the option is
  actually recognized by running `git --no-pager --no-lazy-fetch
  --version`. Either failure is unsupported substrate: a passing
  version number alone is never treated as sufficient proof that the
  installed binary behaves as documented.
- **Environment cannot redirect Git, and cannot silently re-enable lazy
  fetch.** `git_environment()` strips every inherited `GIT_*` variable
  (same rationale as `checkpoint_ref.git_environment()`) and then
  deliberately sets `GIT_NO_LAZY_FETCH=1`, overriding any inherited
  value rather than trusting it.
- **A fixed hardened baseline applies to every governed invocation.**
  `BASELINE_ARGS` is the exact ADR 0006 §5 set: `-c
  core.hooksPath=/dev/null`, `-c core.fsmonitor=false` (an explicit
  boolean — an empty string does not suppress fsmonitor), `-c
  core.autocrlf=false`, `-c submodule.recurse=false`, `--no-pager`, and
  `--no-lazy-fetch` (redundant with the environment variable above,
  deliberately — a call site that forgets one still has the other).
- **Filter-driver neutralization only touches subkeys that actually
  exist, and only `clean`/`smudge`, never `required`.**
  `enumerate_filter_neutralization` overrides `clean`/`smudge` to the
  fixed, verified absolute path `/bin/cat` (never `shutil.which`) only
  for drivers where that subkey is actually configured, and clears
  `process` (empty, never `/bin/cat` — `.process` speaks a persistent
  pkt-line protocol `/bin/cat` cannot) only when it is actually
  configured. `filter.<name>.required` is read but never overridden:
  ADR 0006 §1/finding 10 proved `required=false` only masks the
  enumerator's own construction bugs and is unnecessary once overrides
  are emitted correctly.
- **Enumeration is NUL-safe and bounded.** `git config -z
  --get-regexp` output is parsed on NUL boundaries, never
  newline-split (a driver name or value could legitimately contain
  characters a naive line-based parser would misread). Driver names are
  deduplicated by exact string equality, including names containing
  dots (a Git config subsection matches verbatim between the first and
  last dot). At most 128 distinct drivers, at most 256 bytes per driver
  name, and at most 65,536 bytes of total generated `-c` argv payload
  are permitted; malformed, NUL-invalid, or over-limit configuration is
  a structured `GitSafetyError`, never a partial or silently-degraded
  neutralization.
- **Attribute inspection is NUL-safe and framing-checked.**
  `parse_check_attr_output` parses `check-attr -z` output (flat
  `<path>\\0<attribute>\\0<value>\\0` triples) without any newline-based
  splitting, and requires a terminal NUL so truncated output is never
  mistaken for complete output even when its field count happens to
  divide by three. Empty paths and empty attribute names are refused.
- **Filter classification is driver-set dependent, never context-free.**
  `git check-attr` prints the literal string `unset` both for a
  genuinely unset attribute (`a.txt -filter`) and for an explicit
  assignment naming a driver called `unset` (`b.txt filter=unset`), and
  the same collision exists for `unspecified`. Positive controls
  against a real repository confirmed the named drivers really execute
  while the genuine cases do not — so the reported string alone cannot
  classify `filter` safety, and a context-free rule would let host code
  run during the very checkout ADR 0006 §1 relies on attribute
  inspection to protect. `is_safe_filter_state` therefore takes the
  configured driver-name set (from `enumerate_filter_neutralization`'s
  immutable `driver_names`) and treats `unset`/`unspecified` as safe
  only when that exact string is not also a configured driver name,
  refusing conservatively when it is. `is_safe_non_filter_state` keeps
  the plain state classification for `text`/`eol`/`ident`/
  `working-tree-encoding`/`crlf`, whose values Git does not resolve as
  driver names.
- **One public execution API.** `run_git` is the only public way to
  execute a Git command here; argv construction is the private
  `_build_git_argv` seam, so an integrating module is never handed a
  ready-made argv it could pass to a bare `subprocess` call, silently
  losing the sanitized environment, timeout, or error classification.
- **Sanitized errors.** Messages are fixed, categorical text. Raw Git
  stderr and filesystem paths never appear in a raised message.

No network and no Docker. Filter-neutralization tests exercise real
throwaway repositories with real hostile filter commands and markers;
only the version/option-recognition failure paths that cannot be
provoked with the single real installed Git monkeypatch the `_run`
seam, matching `checkpoint_ref`'s existing testing convention.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

# Bounded so a hung Git invocation cannot stall a caller indefinitely.
# Matches codeagent.checkpoint_ref.GIT_TIMEOUT_SECONDS.
GIT_TIMEOUT_SECONDS = 30.0

# Git's global --no-lazy-fetch option was introduced in Git 2.45; ADR
# 0006 establishes this as CodeAgent v1's minimum supported Git version
# (docs/adr/0006-...-content-fidelity.md, "Partial-clone / lazy-fetch
# safety").
MIN_GIT_VERSION = (2, 45)

# Fixed, verified absolute path — never shutil.which, never a
# PATH-resolved name. Only valid as a clean/smudge passthrough; never
# as a .process override (protocol-incompatible, ADR 0006 finding 10).
FIXED_CAT_PATH = "/bin/cat"

MAX_FILTER_DRIVERS = 128
MAX_FILTER_DRIVER_NAME_BYTES = 256
MAX_FILTER_ARGV_BYTES = 65_536

# Bounds for the shared tracked-path listing / filter-attribute
# inspection helpers (used by workspace.py's pre-materialization
# refusal check, ADR 0006 section 1's primary control). A repository
# with more tracked paths, or a single path longer, than these bounds
# is refused rather than processed — the same "fail closed rather than
# silently truncate" discipline as the filter-driver bounds above.
MAX_TRACKED_PATHS = 200_000
MAX_TRACKED_PATH_BYTES = 4_096
# Paths are inspected in fixed-size chunks so no single `check-attr
# --stdin` invocation's payload grows unboundedly with repository size.
ATTR_CHECK_CHUNK_SIZE = 512
# Defense in depth beyond chunk size × MAX_TRACKED_PATH_BYTES (which
# already bounds a chunk to a few MB): an explicit hard ceiling on any
# single check-attr stdin payload.
MAX_ATTR_CHECK_STDIN_BYTES = 4 * 1024 * 1024

# patch.py-hardening bounds (ADR 0006's patch.py slice). Parity with
# reader.py's existing MAX_READ_BYTES for patch-controlled file content;
# a commit header is at most a few hundred bytes even for a merge, so
# this is generous headroom without inviting a large-message DoS — the
# message body is never parsed or retained.
MAX_PATCH_BLOB_BYTES = 65_536
MAX_COMMIT_OBJECT_BYTES = 4_096

# The staged-index listing is repository-sized, not patch-sized. This
# is a real, generous safety ceiling (never unbounded capture_output)
# rather than a tight per-call bound.
MAX_STAGE_LISTING_BYTES = 64 * 1024 * 1024

# Objects are checked for availability in fixed-size chunks so no
# single `cat-file --batch-check` invocation's stdin/stdout payload
# grows unboundedly with repository size.
MAX_BATCH_CHECK_CHUNK = 512
MAX_BATCH_CHECK_OUTPUT_BYTES = 1 * 1024 * 1024

_GITLINK_MODE = "160000"

# unspecified: no rule at all. unset: an explicit "-attr" negation.
# "set" (bare boolean-true) and any named value are unsafe.
#
# For `filter` these two strings are NOT sufficient on their own: see
# `is_safe_filter_state`. `git check-attr` prints the literal string
# "unset" both for a genuinely unset attribute (`-filter`) and for an
# explicit assignment naming a driver called "unset" (`filter=unset`),
# and likewise for "unspecified" — and a driver so named really does
# execute. Classifying `filter` therefore requires the configured
# driver-name set as context.
SAFE_ATTRIBUTE_STATES = frozenset({"unspecified", "unset"})

# The one attribute whose value Git resolves as a configured driver
# name, which is what makes its classification context-dependent.
FILTER_ATTRIBUTE_NAME = "filter"

# ADR 0006 section 5's exact hardened baseline, applied to every
# governed invocation. Order is fixed for determinism; git accepts
# global options and -c overrides in any order before the subcommand.
BASELINE_ARGS: tuple[str, ...] = (
    "--no-pager",
    "--no-lazy-fetch",
    "--no-replace-objects",
    "--literal-pathspecs",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "submodule.recurse=false",
)

# Strict, fully anchored: the numeric core, then only suffix shapes
# real Git builds actually produce. Trailing junk that does not belong
# to a recognized platform suffix (e.g. "2.45evil") must not parse as a
# version at all, since a spoofed or corrupted version string is
# exactly what the second preflight check exists to catch.
_VERSION_RE = re.compile(
    r"^git version (\d+)\.(\d+)(?:\.\d+)*"  # 2.45 / 2.45.0 / 2.45.0.1
    r"(?:\.(?:rc\d+|windows\.\d+|msysgit\.\d+))?"  # .rc1 / .windows.1 / .msysgit.1
    r"(?:-rc\d+)?"  # -rc1
    r"(?: \([A-Za-z0-9 ._+-]+\))?"  # (Apple Git-157) / (Debian)
    r"$"
)


@unique
class GitSafetyFailure(str, Enum):
    """Categorical reason a Git-safety operation failed or refused.
    Callers branch on this; `GitSafetyError.message` is for humans and
    must never be parsed."""

    GIT_EXECUTABLE_UNAVAILABLE = "git_executable_unavailable"
    GIT_COMMAND_TIMEOUT = "git_command_timeout"
    GIT_VERSION_CHECK_FAILED = "git_version_check_failed"
    MALFORMED_VERSION_OUTPUT = "malformed_version_output"
    UNSUPPORTED_GIT_VERSION = "unsupported_git_version"
    UNRECOGNIZED_LAZY_FETCH_OPTION = "unrecognized_lazy_fetch_option"
    FILTER_ENUMERATION_UNAVAILABLE = "filter_enumeration_unavailable"
    FILTER_ENUMERATION_MALFORMED = "filter_enumeration_malformed"
    FILTER_ENUMERATION_LIMIT_EXCEEDED = "filter_enumeration_limit_exceeded"
    FILTER_PASSTHROUGH_UNAVAILABLE = "filter_passthrough_unavailable"
    ATTRIBUTE_OUTPUT_MALFORMED = "attribute_output_malformed"
    TRACKED_PATH_LISTING_UNAVAILABLE = "tracked_path_listing_unavailable"
    TRACKED_PATH_LISTING_MALFORMED = "tracked_path_listing_malformed"
    TRACKED_PATH_COUNT_EXCEEDED = "tracked_path_count_exceeded"
    TRACKED_PATH_TOO_LONG = "tracked_path_too_long"
    ATTRIBUTE_INSPECTION_UNAVAILABLE = "attribute_inspection_unavailable"
    ATTRIBUTE_INSPECTION_PAYLOAD_TOO_LARGE = "attribute_inspection_payload_too_large"
    ATTRIBUTE_RECORD_COUNT_MISMATCH = "attribute_record_count_mismatch"
    ATTRIBUTE_RECORD_PATH_MISMATCH = "attribute_record_path_mismatch"
    ATTRIBUTE_RECORD_DUPLICATE_PATH = "attribute_record_duplicate_path"
    ATTRIBUTE_RECORD_UNEXPECTED_ATTRIBUTE = "attribute_record_unexpected_attribute"
    OBJECT_FORMAT_UNAVAILABLE = "object_format_unavailable"
    OBJECT_FORMAT_UNSUPPORTED = "object_format_unsupported"
    MALFORMED_OID = "malformed_oid"
    PROCESS_CLEANUP_UNCONFIRMED = "process_cleanup_unconfirmed"
    PROCESS_SETUP_FAILED = "process_setup_failed"
    BOUNDED_COMMAND_FAILED = "bounded_command_failed"
    BINARY_OUTPUT_TOO_LARGE = "binary_output_too_large"
    INDEX_ENTRY_MISSING = "index_entry_missing"
    INDEX_ENTRY_AMBIGUOUS = "index_entry_ambiguous"
    INDEX_ENTRY_UNEXPECTED_MODE = "index_entry_unexpected_mode"
    INDEX_ENTRY_PATH_MISMATCH = "index_entry_path_mismatch"
    INDEX_ENTRY_MALFORMED = "index_entry_malformed"
    TREE_ENTRY_AMBIGUOUS = "tree_entry_ambiguous"
    TREE_ENTRY_UNEXPECTED_TYPE = "tree_entry_unexpected_type"
    TREE_ENTRY_UNEXPECTED_MODE = "tree_entry_unexpected_mode"
    TREE_ENTRY_PATH_MISMATCH = "tree_entry_path_mismatch"
    TREE_ENTRY_MALFORMED = "tree_entry_malformed"
    TREE_LISTING_UNAVAILABLE = "tree_listing_unavailable"
    BLOB_TOO_LARGE = "blob_too_large"
    BLOB_UNAVAILABLE = "blob_unavailable"
    BLOB_UNEXPECTED_TYPE = "blob_unexpected_type"
    COMMIT_OBJECT_TOO_LARGE = "commit_object_too_large"
    COMMIT_OBJECT_UNAVAILABLE = "commit_object_unavailable"
    COMMIT_OBJECT_UNEXPECTED_TYPE = "commit_object_unexpected_type"
    COMMIT_HEADER_MALFORMED = "commit_header_malformed"
    BATCH_CHECK_UNAVAILABLE = "batch_check_unavailable"
    BATCH_CHECK_MALFORMED_RECORD = "batch_check_malformed_record"
    BATCH_CHECK_COUNT_MISMATCH = "batch_check_count_mismatch"
    BATCH_CHECK_OID_MISMATCH = "batch_check_oid_mismatch"
    INDEX_STAGE_LISTING_UNAVAILABLE = "index_stage_listing_unavailable"
    INDEX_STAGE_LISTING_MALFORMED = "index_stage_listing_malformed"
    OBJECT_MISSING = "object_missing"


class GitSafetyError(Exception):
    """A Git-safety operation failed or was refused.

    `reason` is the stable, matchable identifier. `message` is already
    sanitized: fixed categorical text only. Callers that need an
    `OperationalError` translate this once, at the boundary that
    records the occurrence (same split as
    `codeagent.checkpoint_ref.CheckpointRefError`).
    """

    def __init__(self, reason: GitSafetyFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class AttributeRecord:
    """One `(path, attribute, value)` triple from `check-attr` output."""

    path: str
    attribute: str
    value: str

    def is_safe(self, *, configured_driver_names: Collection[str]) -> bool:
        """Classify this record.

        `configured_driver_names` is a required keyword argument even
        for non-filter attributes, which ignore it: a caller must not
        be able to classify a `filter` record without having enumerated
        the repository's configured drivers first (see
        `is_safe_filter_state` for why context-free classification of
        `filter` is unsound).
        """
        if self.attribute == FILTER_ATTRIBUTE_NAME:
            return is_safe_filter_state(
                self.value, configured_driver_names=configured_driver_names
            )
        return is_safe_non_filter_state(self.value)


@dataclass(frozen=True)
class FilterNeutralization:
    """The bounded set of `-c` overrides that neutralize every
    discovered filter driver's `clean`/`smudge`/`process` subkeys that
    are actually configured, plus the exact set of driver names that
    were discovered.

    `driver_names` is the context `is_safe_filter_state` needs, and is
    exposed as an immutable `frozenset` so a caller cannot mutate the
    set a classification decision was made against.
    """

    args: tuple[str, ...]
    driver_names: frozenset[str]

    @property
    def driver_count(self) -> int:
        return len(self.driver_names)


@unique
class ObjectFormat(str, Enum):
    """The repository's Git object format. Only these two exist today;
    anything else fails closed rather than being guessed at.

    Deliberately duplicated from `codeagent.checkpoint_ref.ObjectFormat`
    rather than imported: `_git_safety` is the shared foundation
    *under* `checkpoint_ref`, `workspace`, and `patch`, and importing a
    higher-level primitive module back into the foundation would invert
    that layering. The two enums are identical by construction (ADR
    0003/0004's accepted sha1/sha256 pair) and each module's tests
    cover its own copy independently.
    """

    SHA1 = "sha1"
    SHA256 = "sha256"

    @property
    def hex_length(self) -> int:
        return 40 if self is ObjectFormat.SHA1 else 64


def detect_object_format(repo_path: Path | str) -> ObjectFormat:
    """Discover `repo_path`'s Git object format via `git rev-parse
    --show-object-format`. Raises `GitSafetyError` if the command fails
    or reports anything other than `sha1`/`sha256` — never guessed at."""
    result = run_git(repo_path, "rev-parse", "--show-object-format")
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.OBJECT_FORMAT_UNAVAILABLE,
            "the repository's git object format could not be determined",
        )
    raw = result.stdout.strip()
    try:
        return ObjectFormat(raw)
    except ValueError as exc:
        raise GitSafetyError(
            GitSafetyFailure.OBJECT_FORMAT_UNSUPPORTED,
            "the repository's git object format is not a supported format "
            "(expected sha1 or sha256)",
        ) from exc


_HEX_RE = re.compile(r"^[0-9a-f]+$")


def validate_oid(object_format: ObjectFormat, value: str) -> str:
    """Validate `value` as a well-formed object id for `object_format`:
    exact expected hex length, lowercase hex characters only. Every
    OID this module receives from Git output and later reuses (in a
    revision expression, a lookup, or a comparison) is validated here
    before being trusted — never assumed well-formed merely because it
    came from a `git` invocation that exited 0."""
    if not isinstance(value, str) or len(value) != object_format.hex_length or not _HEX_RE.match(value):
        raise GitSafetyError(
            GitSafetyFailure.MALFORMED_OID,
            f"expected exactly {object_format.hex_length} lowercase hexadecimal characters "
            f"for this repository's {object_format.value} object format",
        )
    return value


def git_environment() -> dict[str, str]:
    """The environment every governed Git invocation runs with: the
    parent environment minus every `GIT_*` variable, then
    `GIT_NO_LAZY_FETCH=1` set deliberately (overriding any inherited
    value, hostile or not).

    See `codeagent.checkpoint_ref.git_environment()` for the stripping
    rationale, which applies identically here.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def _build_git_argv(repo_path: Path | str | None, *args: str) -> list[str]:
    """Build a full `git` argv with the hardened baseline applied, and
    `-C <repo_path>` inserted (after the baseline, before the
    subcommand) when a repository is given. `repo_path` is `None` for
    invocations that predate repository access, such as preflight.

    Private on purpose: `run_git` is the only public execution API, so
    an integrating production module is never handed a ready-made argv
    it could pass to a bare `subprocess` call, silently losing the
    sanitized environment, the timeout, and the error classification.
    """
    argv = ["git", *BASELINE_ARGS]
    if repo_path is not None:
        argv += ["-C", str(repo_path)]
    argv += list(args)
    return argv


def _run(
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The single subprocess seam. `args` excludes the leading `git`.
    Structured argv only: never a shell, never a string-built command.
    Raises `GitSafetyError` for a failure to launch or a hung
    invocation; a nonzero exit is returned for the caller to classify.

    Output is decoded with `errors="surrogateescape"`: repository
    content (a filter driver name, a file path) is untrusted and can
    legally contain bytes that are not valid UTF-8 (Git's own config
    grammar and most filesystems allow this) — strict decoding would
    raise an uncaught `UnicodeDecodeError` instead of a categorical
    `GitSafetyError`, defeating the fail-closed discipline this module
    exists to provide.
    """
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            env=git_environment(),
            input=input_text,
        )
    except subprocess.TimeoutExpired:
        # `TimeoutExpired.__str__` embeds the full argv, which can
        # include a real host repository path (`-C <repo>`). The
        # exception is deliberately not chained as __cause__, so a bare
        # traceback print can never surface it.
        raise GitSafetyError(
            GitSafetyFailure.GIT_COMMAND_TIMEOUT,
            "a git command did not finish within its time limit",
        ) from None
    except OSError as exc:
        raise GitSafetyError(
            GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE,
            "the git executable could not be launched",
        ) from exc


def run_git(
    repo_path: Path | str | None,
    *args: str,
    timeout: float = GIT_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The single safe entrypoint for a hardened Git invocation: builds
    the full baseline argv via `_build_git_argv` and executes it through
    the sanitized-environment, timeout-bounded, categorically-erroring
    `_run` seam, in one call.

    A later integrating slice (`workspace.py`/`patch.py`) calls this
    rather than composing an argv with its own bare `subprocess` call — doing so would silently drop the sanitized
    environment, the timeout, or the categorical error handling, and
    reintroduce exactly the `GIT_*`-redirection or hung-process risk
    this module exists to close.
    """
    argv = _build_git_argv(repo_path, *args)
    return _run(argv[1:], timeout=timeout, input_text=input_text)


_BOUNDED_READ_CHUNK = 65_536

# A minimum window allowed to *confirm* an already-issued kill signal
# was reaped, even once the nominal deadline has elapsed. This is not
# additional time to make progress — the process has already been
# killed by this point — it only accounts for the OS actually
# delivering SIGKILL and this process observing the exit. Mirrors the
# bounded-retry shape of codeagent.checkpoint_ref._RefTransaction's
# existing cleanup, without granting a fresh full-length timeout.
_KILL_CONFIRM_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class BoundedProcessResult:
    """The outcome of a bounded binary subprocess run: raw stdout bytes
    (never text-decoded, never subject to locale-dependent conversion)
    and the confirmed exit code. Never raised for a nonzero exit — the
    caller classifies that itself, exactly like `_run`'s text seam.
    This is `_read_bounded`'s own contract; the public `run_git_bounded`
    wrapper below has a stricter one — see its docstring."""

    stdout: bytes
    returncode: int


class _BoundedFailure(Exception):
    """Internal-only signal from the read/confirm helpers below to
    `_read_bounded`'s single cleanup-and-raise site. Never escapes
    `_read_bounded` itself."""

    def __init__(self, reason: GitSafetyFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


_BOUNDED_FAILURE_MESSAGES: dict[GitSafetyFailure, str] = {
    GitSafetyFailure.GIT_COMMAND_TIMEOUT: "a git command did not finish within its time limit",
    GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE: (
        "a git command produced more output than the fixed safety size bound allows"
    ),
    GitSafetyFailure.PROCESS_SETUP_FAILED: "a git subprocess could not be monitored",
    GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED: (
        "a git subprocess could not be confirmed terminated"
    ),
}


def _drain_stdout(process: subprocess.Popen, *, deadline: float, limit: int) -> bytes:
    """Read at most `limit + 1` bytes of `process.stdout`, non-blocking,
    under `deadline`. Raises `_BoundedFailure` for a monitoring setup
    failure, a timeout, or an oversize read — never partially trusts
    the buffer in any of those cases (the caller discards it)."""
    try:
        selector = selectors.DefaultSelector()
    except Exception as exc:  # noqa: BLE001 — any setup failure is categorical
        raise _BoundedFailure(GitSafetyFailure.PROCESS_SETUP_FAILED) from exc

    buf = bytearray()
    try:
        try:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
        except Exception as exc:  # noqa: BLE001 — any setup failure is categorical
            raise _BoundedFailure(GitSafetyFailure.PROCESS_SETUP_FAILED) from exc

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BoundedFailure(GitSafetyFailure.GIT_COMMAND_TIMEOUT)
            if not selector.select(timeout=remaining):
                continue
            try:
                chunk = os.read(process.stdout.fileno(), _BOUNDED_READ_CHUNK)
            except BlockingIOError:
                continue
            except OSError as exc:
                # An unreadable pipe is not distinguishable from a hung
                # child in any way that would change what happens next.
                raise _BoundedFailure(GitSafetyFailure.GIT_COMMAND_TIMEOUT) from exc
            if not chunk:
                return bytes(buf)  # EOF: the child closed stdout normally.
            buf += chunk
            if len(buf) > limit:
                raise _BoundedFailure(GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE)
    finally:
        try:
            selector.close()
        except Exception:  # noqa: BLE001 — cleanup must not depend on this succeeding
            pass
        try:
            process.stdout.close()
        except Exception:  # noqa: BLE001
            pass


def _confirm_exit(process: subprocess.Popen, *, deadline: float) -> None:
    """Confirm the child has already exited (EOF was observed) within
    `deadline`. A child that closes stdout but does not promptly exit
    is classified as a timeout, not silently accepted — the caller
    still performs the same kill/confirm cleanup either way."""
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise _BoundedFailure(GitSafetyFailure.GIT_COMMAND_TIMEOUT) from exc


def _confirm_writer(writer_thread: threading.Thread | None, *, deadline: float) -> None:
    """Confirm a concurrent stdin-writer thread (if any) has actually
    finished within `deadline`. A writer thread still alive at this
    point means its write never completed for a reason the read loop's
    success does not explain — treated as an unconfirmed cleanup, never
    silently ignored."""
    if writer_thread is None:
        return
    writer_thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if writer_thread.is_alive():
        raise _BoundedFailure(GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED)


def _terminate_and_confirm(
    process: subprocess.Popen,
    *,
    writer_thread: threading.Thread | None,
    deadline: float,
) -> None:
    """The single abort path, used for every failure after `Popen`
    succeeds: kill the child (which is what actually unblocks a writer
    thread stuck on a full stdin pipe — the child's end of the pipe
    disappearing delivers `BrokenPipeError` to the blocked `write()`),
    close stdout, confirm the kill was reaped within a bounded grace
    window, then confirm the writer thread (if any) actually stopped
    and closed stdin itself. Raises
    `GitSafetyError(PROCESS_CLEANUP_UNCONFIRMED)` if either
    confirmation cannot be completed — never returns having silently
    left an unconfirmed process or thread behind.

    Deliberately does **not** call `process.stdin.close()` from this
    (main) thread while a writer thread might still be inside a
    blocking `write()` on that same file object: Python's buffered-IO
    `close()` and an in-flight `write()` on the same object contend for
    the same internal lock, so closing it concurrently can itself
    deadlock this thread against the writer instead of unblocking it —
    a real hang reproduced while adding this cleanup path. Once the
    writer thread is confirmed stopped below, closing stdin is its own
    responsibility (see `_read_bounded`'s `_write_stdin`, which always
    closes it in its own `finally`), not this function's.
    """
    try:
        process.kill()
    except Exception:  # noqa: BLE001 — still attempt to confirm exit below
        pass
    try:
        if process.stdout is not None:
            process.stdout.close()
    except Exception:  # noqa: BLE001
        pass

    confirm_deadline = max(deadline, time.monotonic()) + _KILL_CONFIRM_GRACE_SECONDS
    try:
        process.wait(timeout=max(0.0, confirm_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise GitSafetyError(
            GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED,
            _BOUNDED_FAILURE_MESSAGES[GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED],
        ) from None

    if writer_thread is not None:
        writer_thread.join(timeout=max(0.0, confirm_deadline - time.monotonic()))
        if writer_thread.is_alive():
            raise GitSafetyError(
                GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED,
                _BOUNDED_FAILURE_MESSAGES[GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED],
            )


def _read_bounded(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: float,
    limit: int,
    input_bytes: bytes | None = None,
) -> BoundedProcessResult:
    """The bounded binary subprocess seam: at most `limit + 1` bytes of
    raw stdout, one monotonic deadline shared across the read loop, a
    kill-on-timeout-or-oversize path, and a confirmed exit before ever
    returning or raising.

    Exactly one cleanup path (`_terminate_and_confirm`) handles every
    failure after `Popen` succeeds — a read/oversize/timeout failure,
    an EOF followed by a `wait` timeout (classified as
    `GIT_COMMAND_TIMEOUT`, not silently accepted), an unconfirmed
    writer thread, a monitoring setup failure, or any other unexpected
    exception. Nothing after `Popen` can exit this function without
    going through it first.

    `input_bytes` defaults to `None` (no stdin — most of this module's
    object-read use cases need none of it, and stdin is explicitly
    closed via `DEVNULL` so a git process can never block waiting on
    this process's own inherited stdin). When a caller does supply
    `input_bytes` (only `check_index_blob_availability`'s batch-check
    needs this), it is written by a dedicated daemon thread running
    concurrently with the read loop below — never written in full
    before reading starts, which could deadlock once both this
    process's stdout pipe and the child's stdin pipe fill their OS
    buffers at once.

    Uses a non-blocking fd + `selectors` + a monotonic deadline (the
    same mechanism `codeagent.checkpoint_ref._RefTransaction._read_line`
    already uses), never a blocking `stdout.read(n)`: a blocking read
    waits for either `n` bytes or EOF and has no timeout of its own, so
    a child that writes partial output and then hangs would block
    forever before any `wait(timeout=...)` call downstream was ever
    reached.
    """
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            ["git", *args],
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError as exc:
        raise GitSafetyError(
            GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE,
            "the git executable could not be launched",
        ) from exc

    # Writer-thread creation and `.start()` are inside this same
    # protected region as the drain/confirm sequence: a thread-start
    # failure (e.g. `RuntimeError: can't start new thread`) is exactly
    # as much a post-Popen failure as a read timeout, and must go
    # through the same single cleanup path rather than leaking the
    # already-spawned `process`.
    writer_thread: threading.Thread | None = None
    try:
        if input_bytes is not None:

            def _write_stdin() -> None:
                try:
                    process.stdin.write(input_bytes)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except Exception:  # noqa: BLE001 — cleanup must not depend on this succeeding
                        pass

            _writer = threading.Thread(target=_write_stdin, daemon=True)
            _writer.start()
            # Only recorded once `.start()` succeeds: a thread that was
            # never started must never be `.join()`ed by the cleanup
            # path below (`Thread.join()` raises `RuntimeError` for a
            # thread that hasn't been started), and there is nothing to
            # confirm-stopped for a thread that never ran.
            writer_thread = _writer

        buf = _drain_stdout(process, deadline=deadline, limit=limit)
        _confirm_exit(process, deadline=deadline)
        _confirm_writer(writer_thread, deadline=deadline)
    except _BoundedFailure as failure:
        _terminate_and_confirm(process, writer_thread=writer_thread, deadline=deadline)
        raise GitSafetyError(failure.reason, _BOUNDED_FAILURE_MESSAGES[failure.reason]) from None
    except GitSafetyError:
        # Cleanup first, then preserve the original error's identity —
        # unless cleanup itself fails, in which case that failure (a
        # confirmed-unconfirmed process/thread) dominates and propagates
        # instead, via the bare `raise` never being reached below.
        _terminate_and_confirm(process, writer_thread=writer_thread, deadline=deadline)
        raise
    except BaseException as exc:  # noqa: BLE001 — one unified cleanup path for anything else
        _terminate_and_confirm(process, writer_thread=writer_thread, deadline=deadline)
        raise GitSafetyError(
            GitSafetyFailure.PROCESS_SETUP_FAILED,
            "a git subprocess failed unexpectedly",
        ) from exc

    return BoundedProcessResult(stdout=bytes(buf), returncode=process.returncode)


def run_git_bounded(
    repo_path: Path | str | None,
    *args: str,
    limit: int,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> BoundedProcessResult:
    """The bounded-binary counterpart to `run_git`: builds the same
    hardened baseline argv, then executes it through `_read_bounded`
    instead of `subprocess.run(capture_output=True, text=True)` — for
    call sites retrieving object content, where an unrestricted text
    capture would both risk unbounded memory growth and apply
    locale-dependent decoding to bytes that must be compared exactly.

    Unlike `_read_bounded`'s own neutral contract, this public wrapper
    fails categorically (`GitSafetyFailure.BOUNDED_COMMAND_FAILED`) on
    a nonzero exit rather than returning a `BoundedProcessResult` the
    caller must remember to check — every current call site treats a
    nonzero exit as a failure anyway, so silently returning it as data
    is a footgun this wrapper removes. A caller that needs a more
    specific reason (e.g. "blob unavailable" vs. "commit unavailable")
    catches this reason and re-raises its own.
    """
    argv = _build_git_argv(repo_path, *args)
    result = _read_bounded(argv[1:], env=git_environment(), timeout=timeout, limit=limit)
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.BOUNDED_COMMAND_FAILED,
            "a bounded git command exited with a nonzero status",
        )
    return result


def _parse_git_version(text: str) -> tuple[int, int] | None:
    """Parse `major.minor` out of `git --version` output (e.g. "git
    version 2.54.0", "git version 2.54.0 (Apple Git-157)", "git version
    2.45.0.windows.1", "git version 2.45.0.rc1").

    Returns `None` — never a best guess — for anything that is not
    exactly one line matching that grammar, including trailing junk
    ("2.45evil") and extra lines. The version is compared as a tuple of
    ints, so "2.9" correctly sorts below "2.45".
    """
    stripped = text.strip()
    if "\n" in stripped:
        return None
    match = _VERSION_RE.match(stripped)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def check_git_preflight() -> None:
    """Capability/platform preflight, run once before any repository
    access (ADR 0006 §6). Two independent checks, either failure is
    unsupported substrate:

    1. `git --version` must parse and report `>= 2.45`.
    2. `git --no-pager --no-lazy-fetch --version` must succeed,
       proving the option is actually recognized — a version number
       alone is not sufficient proof.
    """
    version_result = _run(["--version"])
    if version_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.GIT_VERSION_CHECK_FAILED,
            "git --version did not complete successfully",
        )
    version = _parse_git_version(version_result.stdout)
    if version is None:
        raise GitSafetyError(
            GitSafetyFailure.MALFORMED_VERSION_OUTPUT,
            "git --version output could not be parsed as a git version string",
        )
    if version < MIN_GIT_VERSION:
        raise GitSafetyError(
            GitSafetyFailure.UNSUPPORTED_GIT_VERSION,
            "the installed git version is older than the minimum supported version",
        )

    option_result = _run(["--no-pager", "--no-lazy-fetch", "--version"])
    if option_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.UNRECOGNIZED_LAZY_FETCH_OPTION,
            "the installed git did not recognize the --no-lazy-fetch option",
        )


def is_safe_non_filter_state(value: str) -> bool:
    """Classify a **non-filter** attribute state (`text`, `eol`,
    `ident`, `working-tree-encoding`, legacy `crlf`): `unspecified` and
    `unset` are safe; `set` and any explicit value are unsafe.

    Deliberately not usable for `filter` — that attribute's value is
    resolved by Git as a configured driver name, so its classification
    needs context (`is_safe_filter_state`).
    """
    return value in SAFE_ATTRIBUTE_STATES


def is_safe_filter_state(value: str, *, configured_driver_names: Collection[str]) -> bool:
    """Classify a `filter` attribute state against the repository's
    configured driver names.

    `git check-attr` reports the literal string `unset` both for a
    genuinely unset attribute (`a.txt -filter`) and for an explicit
    assignment naming a driver called `unset` (`b.txt filter=unset`);
    the same collision exists for `unspecified`. Positive controls
    against a real repository confirmed the named drivers really do
    execute while the genuine cases do not, so the reported string
    alone cannot distinguish them.

    The rule is therefore driver-set dependent, and conservative when
    ambiguous: `unset`/`unspecified` is safe only when that exact
    string is not also a configured driver name. Any other value —
    `set` or an ordinary named driver — is unsafe regardless.
    """
    if value not in SAFE_ATTRIBUTE_STATES:
        return False
    # Ambiguous: this path may be genuinely unset/unspecified, or may
    # be an explicit assignment to a live driver of that exact name.
    # Refuse rather than guess.
    return value not in configured_driver_names


def _split_nul_records(raw: str, *, reason: GitSafetyFailure, description: str) -> list[str]:
    """Split NUL-framed Git output into records, requiring a terminal
    NUL so truncated output is never mistaken for complete output.
    Returns `[]` for genuinely empty output."""
    if raw == "":
        return []
    if not raw.endswith("\0"):
        raise GitSafetyError(reason, f"{description} was truncated: it did not end with a NUL")
    return raw.split("\0")[:-1]


def parse_check_attr_output(raw: str) -> list[AttributeRecord]:
    """Parse `git check-attr -z [--cached] --stdin <attrs...>` output:
    a flat sequence of NUL-terminated `<path>\\0<attribute>\\0<value>\\0`
    triples, never newline-split.

    Raises `GitSafetyError` if the output is truncated (no terminal
    NUL, even when the field count happens to divide by three), does
    not decompose into complete triples, or contains an empty path or
    attribute name.
    """
    fields = _split_nul_records(
        raw,
        reason=GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
        description="git check-attr output",
    )
    if len(fields) % 3 != 0:
        raise GitSafetyError(
            GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
            "git check-attr output did not decompose into complete path/attribute/value triples",
        )
    records = []
    for i in range(0, len(fields), 3):
        path, attribute, value = fields[i], fields[i + 1], fields[i + 2]
        if path == "":
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
                "git check-attr output contained a record with an empty path",
            )
        if attribute == "":
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
                "git check-attr output contained a record with an empty attribute name",
            )
        records.append(AttributeRecord(path=path, attribute=attribute, value=value))
    return records


def _parse_filter_config(raw: str) -> dict[str, dict[str, str]]:
    """Parse `git config -z --get-regexp '^filter\\.'` output into
    `{driver_name: {subkey: value}}`, NUL-safe throughout. A driver name
    is everything between the fixed `filter.` prefix and the final
    `.<subkey>` component, so a name containing dots (a valid Git
    config subsection) is never mis-split."""
    records = _split_nul_records(
        raw,
        reason=GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
        description="git config output",
    )

    drivers: dict[str, dict[str, str]] = {}
    for record in records:
        if "\n" in record:
            key, value = record.split("\n", 1)
        else:
            # A bare-boolean config entry (e.g. `[filter "x"]\n\tclean`
            # with no `=value`) is emitted as `key` alone, with no
            # embedded newline at all — a real, valid Git config shape
            # (Git's own canonical boolean-true string), not malformed
            # input.
            key, value = record, "true"
        if not key.startswith("filter."):
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a key outside the filter.* namespace",
            )
        remainder = key[len("filter."):]
        if "." not in remainder:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a filter key with no driver name or subkey",
            )
        driver_name, subkey = remainder.rsplit(".", 1)
        if not driver_name:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a filter key with an empty driver name",
            )
        drivers.setdefault(driver_name, {})[subkey] = value
    return drivers


def enumerate_filter_neutralization(repo_path: Path | str) -> FilterNeutralization:
    """Enumerate `filter.*` configuration for `repo_path` and build the
    bounded set of `-c` overrides that neutralize every discovered
    driver's `clean`/`smudge`/`process` subkeys — only the subkeys
    actually configured, never a blind override, and never
    `required=false` (ADR 0006 §1/finding 10).

    Raises `GitSafetyError` for malformed output, or if the discovered
    configuration exceeds the fixed bounds: at most
    `MAX_FILTER_DRIVERS` distinct drivers, at most
    `MAX_FILTER_DRIVER_NAME_BYTES` bytes per driver name, and at most
    `MAX_FILTER_ARGV_BYTES` bytes of total generated argv payload.
    """
    result = run_git(repo_path, "config", "-z", "--get-regexp", r"^filter\.")
    if result.returncode == 1 and result.stdout == "":
        return FilterNeutralization(args=(), driver_names=frozenset())
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_UNAVAILABLE,
            "the repository's filter configuration could not be enumerated",
        )

    drivers = _parse_filter_config(result.stdout)

    if len(drivers) > MAX_FILTER_DRIVERS:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
            "the repository configures more filter drivers than the fixed safety bound allows",
        )
    for name in drivers:
        # surrogateescape mirrors _run's decoding, so a driver name
        # containing non-UTF-8 bytes is measured by its real byte
        # length instead of raising UnicodeEncodeError on the escaped
        # surrogate codepoints.
        if len(name.encode("utf-8", "surrogateescape")) > MAX_FILTER_DRIVER_NAME_BYTES:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
                "a configured filter driver name exceeds the fixed safety length bound",
            )

    needs_passthrough = any("clean" in subkeys or "smudge" in subkeys for subkeys in drivers.values())
    if needs_passthrough:
        _verify_fixed_cat_path()

    overrides: list[str] = []
    total_bytes = 0
    for name in sorted(drivers):
        subkeys = drivers[name]
        entries = []
        if "clean" in subkeys:
            entries.append(f"filter.{name}.clean={FIXED_CAT_PATH}")
        if "smudge" in subkeys:
            entries.append(f"filter.{name}.smudge={FIXED_CAT_PATH}")
        if "process" in subkeys:
            entries.append(f"filter.{name}.process=")
        for entry in entries:
            overrides.append("-c")
            overrides.append(entry)
            # Count the argument bytes plus a NUL terminator per
            # argument, matching ADR 0006 §1's "including encoded bytes
            # and argument terminators".
            total_bytes += len(b"-c\0") + len(entry.encode("utf-8", "surrogateescape")) + 1

    if total_bytes > MAX_FILTER_ARGV_BYTES:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
            "the generated filter-neutralization argv exceeds the fixed safety size bound",
        )

    return FilterNeutralization(args=tuple(overrides), driver_names=frozenset(drivers))


def _verify_fixed_cat_path() -> None:
    """Confirm `FIXED_CAT_PATH` exists, is a regular file (following
    symlinks — many systems symlink `/bin` to `/usr/bin`), and is
    executable, before it is embedded into any `-c filter.<name>.clean=`
    or `.smudge=` override. A missing or non-executable passthrough
    would otherwise only surface as a confusing failure from the later
    Git invocation that actually tries to run it."""
    if not (os.path.isfile(FIXED_CAT_PATH) and os.access(FIXED_CAT_PATH, os.X_OK)):
        raise GitSafetyError(
            GitSafetyFailure.FILTER_PASSTHROUGH_UNAVAILABLE,
            "the fixed filter passthrough executable is not available on this host",
        )


def list_tracked_paths(repo_path: Path | str) -> tuple[str, ...]:
    """List every path tracked in `repo_path`'s index (`git ls-files
    -z`), NUL-safe and genuinely bounded.

    Runs through the bounded binary seam (`run_git_bounded`/
    `_read_bounded`): a fixed total-output limit
    (`MAX_STAGE_LISTING_BYTES`, `limit + 1` oversize detection so an
    oversized listing is never fully materialized), one shared
    monotonic deadline, and the same kill/reap/cleanup-confirmation
    guarantees as every other bounded retrieval in this module — never
    an unbounded `capture_output` text call, which this function
    previously used despite its own docstring already claiming
    "bounded" (a real, uncorrected gap until this pass).

    Used as the input to `check_filter_attribute_for_paths` for ADR
    0006 section 1's primary control: before any materializing
    checkout, every tracked path's `filter` attribute must be
    inspected, not just the paths a particular operation happens to
    touch.

    Raises `GitSafetyError` if the listing command fails or times out,
    the output exceeds the fixed total-output bound, the decoded output
    is truncated or contains an empty or duplicate path, or the
    repository has more tracked paths, or a single path longer, than
    the fixed safety bounds (`MAX_TRACKED_PATHS`, `MAX_TRACKED_PATH_BYTES`)
    — never a partial or silently truncated listing.
    """
    try:
        result = run_git_bounded(repo_path, "ls-files", "-z", limit=MAX_STAGE_LISTING_BYTES)
    except GitSafetyError as exc:
        if exc.reason is GitSafetyFailure.BOUNDED_COMMAND_FAILED:
            raise GitSafetyError(
                GitSafetyFailure.TRACKED_PATH_LISTING_UNAVAILABLE,
                "the repository's tracked paths could not be listed",
            ) from exc
        raise
    text = result.stdout.decode("utf-8", "surrogateescape")
    paths = _split_nul_records(
        text,
        reason=GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED,
        description="git ls-files output",
    )
    if len(paths) > MAX_TRACKED_PATHS:
        raise GitSafetyError(
            GitSafetyFailure.TRACKED_PATH_COUNT_EXCEEDED,
            "the repository has more tracked paths than the fixed safety bound allows",
        )
    seen: set[str] = set()
    for path in paths:
        if path == "":
            raise GitSafetyError(
                GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED,
                "git ls-files output contained an empty path",
            )
        if len(path.encode("utf-8", "surrogateescape")) > MAX_TRACKED_PATH_BYTES:
            raise GitSafetyError(
                GitSafetyFailure.TRACKED_PATH_TOO_LONG,
                "a tracked path exceeds the fixed safety length bound",
            )
        if path in seen:
            raise GitSafetyError(
                GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED,
                "git ls-files output contained a duplicate path",
            )
        seen.add(path)
    return tuple(paths)


# ADR 0006's full attribute set: `filter` (driver-name-aware
# classification) plus the five content-fidelity attributes, each safe
# only when `unspecified`/`unset` (`AttributeRecord.is_safe` already
# encodes this split). Legacy `crlf` is included alongside `eol` since
# Git still honors it independently on older repository configurations.
TEXT_ATTRIBUTE_NAME = "text"
EOL_ATTRIBUTE_NAME = "eol"
IDENT_ATTRIBUTE_NAME = "ident"
WORKING_TREE_ENCODING_ATTRIBUTE_NAME = "working-tree-encoding"
CRLF_ATTRIBUTE_NAME = "crlf"

ALL_SAFETY_ATTRIBUTE_NAMES: tuple[str, ...] = (
    FILTER_ATTRIBUTE_NAME,
    TEXT_ATTRIBUTE_NAME,
    EOL_ATTRIBUTE_NAME,
    IDENT_ATTRIBUTE_NAME,
    WORKING_TREE_ENCODING_ATTRIBUTE_NAME,
    CRLF_ATTRIBUTE_NAME,
)


def check_attributes_for_paths(
    repo_path: Path | str,
    paths: Collection[str],
    *,
    cached: bool = True,
    attribute_names: Collection[str] = ALL_SAFETY_ATTRIBUTE_NAMES,
) -> tuple[AttributeRecord, ...]:
    """Inspect every attribute in `attribute_names` (default: ADR
    0006's full set — `filter`, `text`, `eol`, `ident`,
    `working-tree-encoding`, `crlf`) for every path in `paths`, in
    fixed-size chunks (`ATTR_CHECK_CHUNK_SIZE`) so no single
    `check-attr --stdin` invocation's payload grows unboundedly with
    the number of paths.

    `cached=True` (the default) inspects attributes as they would apply
    to the staged index's own `.gitattributes` content — matching `git
    check-attr --cached`. `cached=False` inspects them against the
    working tree's current `.gitattributes` content instead, which can
    differ from the staged view when a `.gitattributes` edit is present
    but not yet staged; patch.py's hardening requires both views be
    checked before either a write or an `add`, since an unstaged
    `.gitattributes` edit still governs what `git add` itself does.

    One `check-attr` invocation per chunk requests every attribute
    name at once; Git reports records path-major, one record per
    attribute per path, in the exact order requested. Nothing here is
    trusted merely because the exit code was 0: the record count must
    match `len(chunk) * len(attribute_names)` exactly, each record's
    path and attribute must match the expected path/attribute at that
    exact position, and no *requested* path may repeat (a duplicate in
    the caller's own `paths` list is refused, since two different
    requested slots could then be satisfied by records that are each
    individually well-formed but ambiguous as a set). Any mismatch — a
    Git version reordering output, a truncated response, an injected or
    dropped record — is a categorical `GitSafetyError`, never silently
    tolerated or partially applied.
    """
    paths = list(paths)
    attribute_names = list(attribute_names)
    if not attribute_names:
        raise ValueError("attribute_names must be nonempty")
    records: list[AttributeRecord] = []
    for start in range(0, len(paths), ATTR_CHECK_CHUNK_SIZE):
        chunk = paths[start : start + ATTR_CHECK_CHUNK_SIZE]
        stdin_payload = "".join(path + "\0" for path in chunk)
        payload_bytes = len(stdin_payload.encode("utf-8", "surrogateescape"))
        if payload_bytes > MAX_ATTR_CHECK_STDIN_BYTES:
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_INSPECTION_PAYLOAD_TOO_LARGE,
                "a chunk of paths to inspect exceeds the fixed safety payload size bound",
            )
        cache_args = ("--cached",) if cached else ()
        result = run_git(
            repo_path,
            "check-attr",
            *cache_args,
            "-z",
            "--stdin",
            *attribute_names,
            input_text=stdin_payload,
        )
        if result.returncode != 0:
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_INSPECTION_UNAVAILABLE,
                "an attribute could not be inspected for a chunk of tracked paths",
            )
        chunk_records = parse_check_attr_output(result.stdout)
        expected_count = len(chunk) * len(attribute_names)
        if len(chunk_records) != expected_count:
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_RECORD_COUNT_MISMATCH,
                "git check-attr reported a different number of records than requested",
            )
        seen: set[str] = set()
        idx = 0
        for path in chunk:
            if path in seen:
                raise GitSafetyError(
                    GitSafetyFailure.ATTRIBUTE_RECORD_DUPLICATE_PATH,
                    "git check-attr reported more than one record for the same path",
                )
            seen.add(path)
            for attribute_name in attribute_names:
                record = chunk_records[idx]
                idx += 1
                if record.path != path:
                    raise GitSafetyError(
                        GitSafetyFailure.ATTRIBUTE_RECORD_PATH_MISMATCH,
                        "git check-attr reported a record for a path that was not requested "
                        "in that position",
                    )
                if record.attribute != attribute_name:
                    raise GitSafetyError(
                        GitSafetyFailure.ATTRIBUTE_RECORD_UNEXPECTED_ATTRIBUTE,
                        "git check-attr reported a record for an attribute that was not "
                        "requested in that position",
                    )
                records.append(record)
    return tuple(records)


def check_filter_attribute_for_paths(
    repo_path: Path | str, paths: Collection[str], *, cached: bool = True
) -> tuple[AttributeRecord, ...]:
    """Backward-compatible single-attribute (`filter`) specialization
    of `check_attributes_for_paths`, used by `workspace.py`."""
    return check_attributes_for_paths(
        repo_path, paths, cached=cached, attribute_names=(FILTER_ATTRIBUTE_NAME,)
    )


_REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class IndexEntry:
    """One stage-0 `git ls-files --stage` record for a single path."""

    mode: str
    oid: str
    path: str


@dataclass(frozen=True)
class TreeEntry:
    """One `git ls-tree` record for a single path within a commit."""

    mode: str
    type: str
    oid: str
    path: str


def lookup_staged_entry(
    repo_path: Path | str, relative_path: str, *, object_format: ObjectFormat
) -> IndexEntry:
    """Look up exactly one staged index entry for `relative_path`,
    never via `:<path>` revision syntax — always `-- <path>` pathspec
    argv, with the hardened baseline's `--literal-pathspecs` already in
    effect so the path cannot be reinterpreted as pathspec magic.

    Requires exactly one stage-0 record: zero records (missing), an
    unmerged path (records only at stages 1/2/3, never 0), or more than
    one record (a magic-pathspec bypass, or a genuine duplicate) are
    all refused categorically — never silently disambiguated. The
    returned path must equal `relative_path` exactly, and the mode must
    be a regular file (`100644`/`100755`); patch.py has never supported
    a symlink destination, and this lookup does not start now.
    """
    result = run_git(repo_path, "ls-files", "--stage", "-z", "--", relative_path)
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_MALFORMED,
            "the staged index entry could not be listed",
        )
    records = _split_nul_records(
        result.stdout,
        reason=GitSafetyFailure.INDEX_ENTRY_MALFORMED,
        description="git ls-files --stage output",
    )
    if len(records) == 0:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_MISSING,
            "no staged index entry exists for the requested path",
        )
    if len(records) > 1:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_AMBIGUOUS,
            "more than one staged index record matched the requested path",
        )
    record = records[0]
    # "<mode> <oid> <stage>\t<path>"
    try:
        meta, path = record.split("\t", 1)
        mode, oid, stage = meta.split(" ")
    except ValueError as exc:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_MALFORMED,
            "git ls-files --stage produced an unparseable record",
        ) from exc
    if stage != "0":
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_AMBIGUOUS,
            "the only staged record for the requested path is not at stage 0 (unmerged)",
        )
    if path != relative_path:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_PATH_MISMATCH,
            "git ls-files --stage reported a path that was not requested",
        )
    if mode not in _REGULAR_FILE_MODES:
        raise GitSafetyError(
            GitSafetyFailure.INDEX_ENTRY_UNEXPECTED_MODE,
            "the staged index entry is not a regular file",
        )
    validate_oid(object_format, oid)
    return IndexEntry(mode=mode, oid=oid, path=path)


def lookup_tree_entry(
    repo_path: Path | str, commit_oid: str, relative_path: str, *, object_format: ObjectFormat
) -> TreeEntry | None:
    """Look up exactly one tree entry for `relative_path` within
    `commit_oid`, never via `<commit>:<path>` revision syntax — always
    `-- <path>` pathspec argv against `git ls-tree`.

    Returns `None` if the path does not exist in that commit (a
    legitimate outcome the caller must decide how to treat — e.g. a
    commit-acceptance check treating "missing" as contaminated).
    Raises `GitSafetyError` for more than one match, a path mismatch,
    an unexpected type (only `blob` is valid — a gitlink or a nested
    tree is refused), or an unexpected mode (only a regular file).
    """
    validate_oid(object_format, commit_oid)
    result = run_git(repo_path, "ls-tree", "-z", commit_oid, "--", relative_path)
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.TREE_LISTING_UNAVAILABLE,
            "the commit's tree entry could not be listed",
        )
    records = _split_nul_records(
        result.stdout,
        reason=GitSafetyFailure.TREE_ENTRY_MALFORMED,
        description="git ls-tree output",
    )
    if len(records) == 0:
        return None
    if len(records) > 1:
        raise GitSafetyError(
            GitSafetyFailure.TREE_ENTRY_AMBIGUOUS,
            "more than one tree record matched the requested path",
        )
    record = records[0]
    # "<mode> <type> <oid>\t<path>"
    try:
        meta, path = record.split("\t", 1)
        mode, entry_type, oid = meta.split(" ")
    except ValueError as exc:
        raise GitSafetyError(
            GitSafetyFailure.TREE_ENTRY_MALFORMED,
            "git ls-tree produced an unparseable record",
        ) from exc
    if path != relative_path:
        raise GitSafetyError(
            GitSafetyFailure.TREE_ENTRY_PATH_MISMATCH,
            "git ls-tree reported a path that was not requested",
        )
    if mode not in _REGULAR_FILE_MODES:
        raise GitSafetyError(
            GitSafetyFailure.TREE_ENTRY_UNEXPECTED_MODE,
            "the tree entry is not a regular file",
        )
    if entry_type != "blob":
        raise GitSafetyError(
            GitSafetyFailure.TREE_ENTRY_UNEXPECTED_TYPE,
            "the tree entry is not a blob",
        )
    validate_oid(object_format, oid)
    return TreeEntry(mode=mode, type=entry_type, oid=oid, path=path)


def _check_objects_present(
    repo_path: Path | str, oids: list[str], *, object_format: ObjectFormat
) -> None:
    """Check that every OID in `oids` (already validated by the caller)
    is present as a real object in `repo_path`, via one `git cat-file
    --batch-check` invocation reading OIDs from stdin — the only
    structured, non-stderr-parsing way to distinguish "missing" from
    every other failure. Never more than `MAX_BATCH_CHECK_CHUNK` OIDs
    per invocation, and the result order/count/echoed-OID are all
    verified before any record is trusted.
    """
    payload = ("\n".join(oids) + "\n").encode("ascii")
    argv = _build_git_argv(
        repo_path, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"
    )
    result = _read_bounded(
        argv[1:],
        env=git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
        limit=MAX_BATCH_CHECK_OUTPUT_BYTES,
        input_bytes=payload,
    )
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.BATCH_CHECK_UNAVAILABLE,
            "object availability could not be checked",
        )
    text = result.stdout.decode("utf-8", "surrogateescape")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) != len(oids):
        raise GitSafetyError(
            GitSafetyFailure.BATCH_CHECK_COUNT_MISMATCH,
            "the object availability check returned an unexpected number of records",
        )
    for expected_oid, line in zip(oids, lines):
        parts = line.split(" ")
        if not parts or parts[0] != expected_oid:
            raise GitSafetyError(
                GitSafetyFailure.BATCH_CHECK_OID_MISMATCH,
                "the object availability check returned results out of order",
            )
        if len(parts) == 2 and parts[1] == "missing":
            raise GitSafetyError(
                GitSafetyFailure.OBJECT_MISSING,
                "a referenced object is not available in the local repository",
            )
        if len(parts) != 3:
            raise GitSafetyError(
                GitSafetyFailure.BATCH_CHECK_MALFORMED_RECORD,
                "the object availability check produced an unparseable record",
            )


def check_index_blob_availability(repo_path: Path | str, *, object_format: ObjectFormat) -> None:
    """Check that every non-gitlink object referenced by the current
    staged index is actually present in `repo_path`'s object store.

    This is a pre-mutation safety net for a partial clone
    (`GIT_NO_LAZY_FETCH=1` already refuses lazy fetching, but does not
    by itself prove every staged blob was already fetched) — it does
    **not** prove complete repository object closure (it checks only
    what is currently staged, not history, not unreferenced objects,
    not tags). Gitlinks (mode `160000`) are deliberately excluded: a
    real submodule commit was proven, by direct probe, not to need its
    target commit to exist locally for `write-tree` to succeed.

    Runs `git ls-files --stage -z` through the bounded binary seam
    (never unbounded `capture_output` for repository-sized output) —
    this is a bounded whole-output capture up to `MAX_STAGE_LISTING_BYTES`
    (`limit + 1` oversize detection), not incremental record-by-record
    parsing; the bound is what prevents unbounded memory growth, not
    early termination mid-stream. Deduplicates OIDs and checks them via
    `cat-file --batch-check` in fixed-size chunks.
    """
    try:
        listing = run_git_bounded(
            repo_path, "ls-files", "--stage", "-z", limit=MAX_STAGE_LISTING_BYTES
        )
    except GitSafetyError as exc:
        if exc.reason is GitSafetyFailure.BOUNDED_COMMAND_FAILED:
            raise GitSafetyError(
                GitSafetyFailure.INDEX_STAGE_LISTING_UNAVAILABLE,
                "the staged index could not be listed",
            ) from exc
        raise
    text = listing.stdout.decode("utf-8", "surrogateescape")
    records = _split_nul_records(
        text,
        reason=GitSafetyFailure.INDEX_STAGE_LISTING_MALFORMED,
        description="git ls-files --stage output",
    )
    if len(records) > MAX_TRACKED_PATHS:
        raise GitSafetyError(
            GitSafetyFailure.TRACKED_PATH_COUNT_EXCEEDED,
            "the repository has more staged entries than the fixed safety bound allows",
        )
    oids: set[str] = set()
    for record in records:
        try:
            meta, path = record.split("\t", 1)
            mode, oid, _stage = meta.split(" ")
        except ValueError as exc:
            raise GitSafetyError(
                GitSafetyFailure.INDEX_STAGE_LISTING_MALFORMED,
                "git ls-files --stage produced an unparseable record",
            ) from exc
        if len(path.encode("utf-8", "surrogateescape")) > MAX_TRACKED_PATH_BYTES:
            raise GitSafetyError(
                GitSafetyFailure.TRACKED_PATH_TOO_LONG,
                "a staged path exceeds the fixed safety length bound",
            )
        if mode == _GITLINK_MODE:
            continue
        validate_oid(object_format, oid)
        oids.add(oid)
    sorted_oids = sorted(oids)
    for start in range(0, len(sorted_oids), MAX_BATCH_CHECK_CHUNK):
        chunk = sorted_oids[start : start + MAX_BATCH_CHECK_CHUNK]
        _check_objects_present(repo_path, chunk, object_format=object_format)


def read_blob_bytes(repo_path: Path | str, oid: str, *, object_format: ObjectFormat) -> bytes:
    """Read a blob's raw bytes, bounded by `MAX_PATCH_BLOB_BYTES`.

    A structured pre-check (`cat-file -t`/`-s`) confirms the object is
    actually a blob and within the size bound before any content
    retrieval is attempted — a Git exit code of 0 alone never proves
    the object is what the caller expects.
    """
    validate_oid(object_format, oid)
    type_result = run_git(repo_path, "cat-file", "-t", oid)
    if type_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.BLOB_UNAVAILABLE, "the requested blob object is not available"
        )
    if type_result.stdout.strip() != "blob":
        raise GitSafetyError(
            GitSafetyFailure.BLOB_UNEXPECTED_TYPE, "the requested object is not a blob"
        )
    size_result = run_git(repo_path, "cat-file", "-s", oid)
    if size_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.BLOB_UNAVAILABLE,
            "the requested blob object's size could not be determined",
        )
    try:
        size = int(size_result.stdout.strip())
    except ValueError as exc:
        raise GitSafetyError(
            GitSafetyFailure.BLOB_UNAVAILABLE,
            "the requested blob object's size could not be parsed",
        ) from exc
    if size > MAX_PATCH_BLOB_BYTES:
        raise GitSafetyError(
            GitSafetyFailure.BLOB_TOO_LARGE, "the requested blob exceeds the fixed safety size bound"
        )
    try:
        content = run_git_bounded(repo_path, "cat-file", "-p", oid, limit=MAX_PATCH_BLOB_BYTES)
    except GitSafetyError as exc:
        if exc.reason is GitSafetyFailure.BOUNDED_COMMAND_FAILED:
            raise GitSafetyError(
                GitSafetyFailure.BLOB_UNAVAILABLE,
                "the requested blob object could not be retrieved",
            ) from exc
        raise
    return content.stdout


@dataclass(frozen=True)
class CommitHeader:
    """The structural fields of a commit object that commit-acceptance
    verification needs — never the message body, which is neither
    parsed nor retained."""

    tree: str
    parents: tuple[str, ...]


def read_commit_header(repo_path: Path | str, oid: str, *, object_format: ObjectFormat) -> CommitHeader:
    """Read `oid`'s `tree` and `parent` header lines, bounded by
    `MAX_COMMIT_OBJECT_BYTES` and structurally pre-checked (type=commit,
    size within bound) before retrieval. Parses only header lines up to
    the first blank line (the header/message separator) — the message
    body is never inspected."""
    validate_oid(object_format, oid)
    type_result = run_git(repo_path, "cat-file", "-t", oid)
    if type_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_OBJECT_UNAVAILABLE,
            "the requested commit object is not available",
        )
    if type_result.stdout.strip() != "commit":
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_OBJECT_UNEXPECTED_TYPE,
            "the requested object is not a commit",
        )
    size_result = run_git(repo_path, "cat-file", "-s", oid)
    if size_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_OBJECT_UNAVAILABLE,
            "the requested commit object's size could not be determined",
        )
    try:
        size = int(size_result.stdout.strip())
    except ValueError as exc:
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_OBJECT_UNAVAILABLE,
            "the requested commit object's size could not be parsed",
        ) from exc
    if size > MAX_COMMIT_OBJECT_BYTES:
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_OBJECT_TOO_LARGE,
            "the requested commit object exceeds the fixed safety size bound",
        )
    try:
        content = run_git_bounded(repo_path, "cat-file", "-p", oid, limit=MAX_COMMIT_OBJECT_BYTES)
    except GitSafetyError as exc:
        if exc.reason is GitSafetyFailure.BOUNDED_COMMAND_FAILED:
            raise GitSafetyError(
                GitSafetyFailure.COMMIT_OBJECT_UNAVAILABLE,
                "the requested commit object could not be retrieved",
            ) from exc
        raise
    text = content.stdout.decode("utf-8", "surrogateescape")
    tree: str | None = None
    parents: list[str] = []
    for line in text.split("\n"):
        if line == "":
            break  # header/message separator — the message body is never parsed.
        if line.startswith("tree "):
            if tree is not None:
                raise GitSafetyError(
                    GitSafetyFailure.COMMIT_HEADER_MALFORMED,
                    "the commit header contains more than one tree line",
                )
            tree = line[len("tree ") :]
        elif line.startswith("parent "):
            parents.append(line[len("parent ") :])
    if tree is None:
        raise GitSafetyError(
            GitSafetyFailure.COMMIT_HEADER_MALFORMED, "the commit header is missing a tree line"
        )
    validate_oid(object_format, tree)
    validated_parents = tuple(validate_oid(object_format, parent) for parent in parents)
    return CommitHeader(tree=tree, parents=validated_parents)


def evaluate_tracked_filter_safety(repo_path: Path | str) -> bool:
    """`True` iff every path currently tracked in `repo_path`'s index
    has a safe `filter` attribute, given the repository's actually
    configured driver names (ADR 0006 finding 16's driver-set-dependent
    rule — a bare `unspecified`/`unset` string is not, by itself,
    sufficient).

    This is the composed primary control for ADR 0006 section 1:
    `worktree add`'s real checkout must never proceed while this
    returns anything other than `True`. Any `GitSafetyError` raised by
    the underlying enumeration/listing/inspection calls (a timeout,
    malformed output, an exceeded bound, a missing object) propagates
    to the caller rather than being treated as `True` — the caller must
    treat an inability to complete this evaluation as unsafe, not as a
    pass.
    """
    neutralization = enumerate_filter_neutralization(repo_path)
    paths = list_tracked_paths(repo_path)
    if not paths:
        return True
    records = check_filter_attribute_for_paths(repo_path, paths)
    return all(
        record.is_safe(configured_driver_names=neutralization.driver_names)
        for record in records
    )
