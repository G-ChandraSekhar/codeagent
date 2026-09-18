"""Milestone 2 slice 2B-2: durable evidence-artifact capture.

Implements the evidence-capture design accepted in ADR 0003 Amendment 2
("Durable evidence") and ADR 0006 Amendment 4 (`git diff` as a governed
filter/external-diff/textconv execution point). Attempted exactly once
per run, at terminal teardown, by `RunController._terminate` — always,
regardless of the run's underlying domain outcome, and never blocking
any subsequent cleanup step (see `controller.py`).

Scope boundary:

- **Strictly observational.** Never runs `git add`, `git add
  --intent-to-add`, `update-index`, `checkout`, or any other command
  that mutates the index or working tree — including to work around
  the untracked-path blindness below. The only two governed commands
  are a bounded `git status --porcelain=v1 -z --untracked-files=all`
  (to detect untracked paths) and a bounded `git diff --binary
  --full-index --find-renames --no-ext-diff --no-textconv
  <initial_commit> --` (the actual evidence), both run under
  `_git_safety`'s hardened baseline plus one freshly enumerated
  `enumerate_filter_neutralization()` result, reused unchanged for
  both calls.
- **Current single-file engine only.** `--find-renames`/`--binary` are
  correctly formed for a future multi-file/add/delete/rename/binary
  patch engine, but nothing here or in `GitPatchApplier` exercises
  those operation kinds — this module does not claim forward
  completeness for them. The 1 MiB bound is sized for the current
  engine only and must be re-derived before any larger patch
  capability exists.
- **Any untracked path makes evidence incomplete.** Because today's
  patch engine cannot create files, an untracked path found at capture
  time is either pre-existing foreign content or an anomaly — either
  way its absence from `git diff`'s output (which is blind to
  untracked files under every flag combination) would be a silent,
  undetected completeness gap. Detected via the read-only `status`
  call above, never worked around by staging it.
- **No lifecycle storage, reconciliation, or CLI work.** This module
  performs one capture-and-publish operation and returns a receipt; it
  keeps no state between calls and knows nothing about ADR 0004's
  durable lifecycle store.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codeagent import _git_safety
from codeagent.errors import ErrorCode, OperationalError

# ADR 0003 Amendment 2: a hard 1 MiB bound for the current single-file
# text-replacement patch engine. Must be re-derived, not assumed, before
# any multi-file/add-file patch capability exists.
MAX_DIFF_BYTES = 1 * 1024 * 1024
# A separate, smaller bound for the untracked-path status inventory —
# distinct from the diff payload bound, since this is a precondition
# check, never persisted as the artifact's own payload.
MAX_STATUS_BYTES = 1 * 1024 * 1024

ARTIFACT_MAGIC = b"CAEV"
ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_SUFFIX = ".evidence"


@dataclass(frozen=True)
class EvidenceReceipt:
    """The outcome of one capture attempt — maps directly onto
    `events.EvidenceCaptured`'s fields (minus the event envelope), so a
    caller constructs that event straight from this receipt without any
    translation. See `events.EvidenceCaptured`'s docstring for the four
    legal shapes this must produce."""

    success: bool
    complete: bool
    artifact_id: str | None
    sha256_payload: str | None
    bytes_written: int | None
    error: OperationalError | None


class EvidenceSink(Protocol):
    """Suitable for controller injection — a fake in tests never touches
    a filesystem or Git at all."""

    def capture(
        self,
        *,
        worktree_path: Path,
        source_repo_path: Path,
        initial_commit: str,
        lifecycle_id: str,
    ) -> EvidenceReceipt: ...


class EvidenceSinkError(Exception):
    """Internal-only signal between this module's own helpers and
    `FilesystemEvidenceSink.capture`'s single translation point. Never
    escapes `capture()` itself."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _CollisionError(EvidenceSinkError):
    """An artifact already exists under this lifecycle's exact name."""


class _DurabilityAmbiguousError(EvidenceSinkError):
    """The artifact was genuinely published (the no-replace hard link
    succeeded), but a step after that could not be confirmed. Carries
    the already-known receipt fields, since the artifact really is
    durable at its final path."""

    def __init__(
        self, message: str, *, artifact_id: str, sha256_payload: str, bytes_written: int
    ) -> None:
        super().__init__(message)
        self.artifact_id = artifact_id
        self.sha256_payload = sha256_payload
        self.bytes_written = bytes_written


# The fixed, finite set of top-level paths this module *may* trust as
# OS-ambient symlinks rather than caller- or attacker-controlled
# structure, mapped to the exact resolved target each must have. macOS
# ships /tmp, /var, and /etc as standard symlinks to /private/tmp,
# /private/var, /private/etc (verified on this session's real macOS
# host via os.readlink). This is a narrow, verified exception, not a
# blanket name-based allowlist:
# - it is consulted only when the current platform is actually macOS/
#   Darwin (`_current_platform_is_darwin`) — Linux does not make these
#   three paths symlinks at all, so trusting the names unconditionally
#   on every platform would accept a hostile symlink an attacker placed
#   at one of these exact names on a platform where it has no ambient
#   meaning;
# - even on Darwin, a component is only exempted if it resolves to
#   *exactly* the expected /private/... target — a symlink merely named
#   /tmp/etc/var, but redirected elsewhere, is refused like any other
#   hostile symlink, never trusted by name alone.
# Every other component of the caller-specified path is checked
# unconditionally, existing or not, so a hostile symlink planted at an
# already-materialized intermediate component (e.g. an output
# directory reused across runs) is still caught.
_MACOS_AMBIENT_SYMLINK_TARGETS: dict[str, str] = {
    "/tmp": "/private/tmp",
    "/var": "/private/var",
    "/etc": "/private/etc",
}


def _current_platform_is_darwin() -> bool:
    """A thin, monkeypatchable seam over `sys.platform` so tests can
    exercise the Darwin and non-Darwin branches of the ambient-symlink
    exception regardless of the OS actually running the test suite."""
    return sys.platform == "darwin"


def _is_verified_ambient_symlink(path_str: str) -> bool:
    """True only when: the current platform is verified Darwin, the
    exact path string is one of the fixed named roots, that component
    really is a symlink, and its fully resolved target matches the
    expected `/private/...` path exactly. Never trusted by name alone,
    and never consulted at all off Darwin."""
    if not _current_platform_is_darwin():
        return False
    expected_target = _MACOS_AMBIENT_SYMLINK_TARGETS.get(path_str)
    if expected_target is None:
        return False
    if not os.path.islink(path_str):
        return False
    try:
        actual_target = os.path.realpath(path_str)
    except OSError:
        return False
    return actual_target == expected_target


def _validate_no_symlink_ancestors(path: Path) -> None:
    """Walk every component of `path` (absolute, deliberately **not**
    resolved first — resolving would silently follow and hide exactly
    the symlink this exists to detect), refusing if any component is a
    symlink — checked unconditionally, whether or not that component
    (or the full path) already exists on disk, so a hostile symlink
    planted at an intermediate component of an already-materialized
    output directory (e.g. reused across runs) is still caught.

    The only exception is a verified macOS/Darwin ambient symlink (see
    `_is_verified_ambient_symlink`): a small, fixed, explicit set of
    well-known OS-level symlinks, permitted only on the platform that
    actually creates them and only once their real resolved target is
    confirmed to match exactly. Every other component — including ones
    that already exist, and including any of the three names on a
    non-Darwin platform or with an unexpected target — is refused.
    `_validate_containment`'s containment comparison separately
    operates on the fully resolved path regardless of this check.
    """
    absolute = path.absolute()
    accumulated = Path(absolute.anchor)
    for part in absolute.relative_to(absolute.anchor).parts:
        accumulated = accumulated / part
        accumulated_str = str(accumulated)
        if os.path.islink(accumulated):
            if _is_verified_ambient_symlink(accumulated_str):
                continue
            raise EvidenceSinkError(
                "the evidence output directory's path contains a symlink component"
            )


def _validate_containment(output_dir: Path, other: Path) -> None:
    """Component-aware containment check (never string-prefix
    comparison): refuses if `output_dir` is `other`, is an ancestor of
    `other`, or is inside `other` — checked via `os.path.commonpath` on
    already-resolved absolute paths, in both directions."""
    if output_dir == other:
        raise EvidenceSinkError(
            "the evidence output directory must not be the worktree or source repository"
        )
    common = os.path.commonpath([str(output_dir), str(other)])
    if common == str(output_dir) or common == str(other):
        raise EvidenceSinkError(
            "the evidence output directory must be outside both the disposable worktree "
            "and the trusted source repository"
        )


def _has_untracked_path(status_output: bytes) -> bool:
    """`git status --porcelain=v1 -z` NUL-delimited records: each
    record's first two bytes are the XY status code, `??` for an
    untracked path. Never newline-split."""
    for record in status_output.split(b"\x00"):
        if len(record) >= 2 and record[0:2] == b"??":
            return True
    return False


def _build_header(
    *, lifecycle_id: str, complete: bool, bytes_written: int, sha256_payload: str
) -> bytes:
    """Canonical JSON header, embedded directly in the artifact's own
    bytes — never relying on the in-memory `EvidenceCaptured` event
    surviving process exit. `bytes_total`/`sha256_full_stream` are
    `None` for an incomplete capture, never fabricated: an incomplete
    payload IS the full available preview, but is not claimed to
    represent the complete diff."""
    header = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "lifecycle_id": lifecycle_id,
        "status": "complete" if complete else "incomplete",
        "payload_encoding": "binary",
        "payload_type": "unified-diff",
        "bytes_written": bytes_written,
        "bytes_total": bytes_written if complete else None,
        "complete": complete,
        "sha256_payload": sha256_payload,
        "sha256_full_stream": sha256_payload if complete else None,
    }
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def parse_artifact(raw: bytes) -> tuple[dict, bytes]:
    """Parse a published evidence artifact's bytes into `(header,
    payload)`. Used by tests (and any future reader) to round-trip the
    exact framed format `_publish` writes: magic, big-endian header
    length, canonical JSON header, then raw payload bytes."""
    if raw[:4] != ARTIFACT_MAGIC:
        raise ValueError("not an evidence artifact: bad magic")
    if len(raw) < 8:
        raise ValueError("not an evidence artifact: truncated before header length")
    header_len = int.from_bytes(raw[4:8], "big")
    header_start = 8
    header_end = header_start + header_len
    if header_end > len(raw):
        raise ValueError("not an evidence artifact: truncated header")
    header = json.loads(raw[header_start:header_end].decode("utf-8"))
    payload = raw[header_end:]
    return header, payload


def _open_private_temp(path: Path) -> int:
    """Open a new, private (owner-only, no-replace) temp file: `O_EXCL`
    refuses an existing name, `O_NOFOLLOW` refuses a symlink at the
    final component, and mode `0o600` is enforced by an explicit
    `fchmod` plus a verified stat afterward — independent of umask."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != 0o600:
            raise EvidenceSinkError("a temp file's permissions could not be verified as 0600")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of `data` to `fd`, robust against a partial
    write, `EINTR`, and a write that makes zero progress.

    `os.write` is not guaranteed to write the entire buffer in one call
    — POSIX permits a short write for a regular file (large buffers,
    an interrupted or resource-constrained write), and a single
    `os.write(fd, data)` call silently accepting whatever byte count it
    returns is exactly how a truncated artifact segment could be
    written and then reported as if the full segment were persisted.
    `InterruptedError` (`EINTR`) is retried with the same remaining
    buffer, never treated as a failure. A `write()` that returns `0`
    for a nonempty remaining buffer can never make progress and is
    raised as a categorical `EvidenceSinkError` rather than spun on
    forever.
    """
    view = memoryview(data)
    total = len(view)
    written = 0
    while written < total:
        try:
            n = os.write(fd, view[written:])
        except InterruptedError:
            continue
        if n <= 0:
            raise EvidenceSinkError(
                "a write to the evidence artifact made zero progress"
            )
        written += n


def _best_effort_unlink(path: Path) -> bool:
    """Returns True on confirmed removal (or the path never existed),
    False if removal could not be confirmed — never raises."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


class FilesystemEvidenceSink:
    """The real `EvidenceSink`: a single, self-describing framed binary
    artifact published atomically (same-directory no-replace hard link)
    to a directory confirmed outside both the disposable worktree and
    the trusted source repository."""

    def __init__(self, output_root: Path | str) -> None:
        self._output_root = Path(output_root)

    def capture(
        self,
        *,
        worktree_path: Path,
        source_repo_path: Path,
        initial_commit: str,
        lifecycle_id: str,
    ) -> EvidenceReceipt:
        try:
            output_dir = self._prepare_output_dir(Path(worktree_path), Path(source_repo_path))
            neutralization = _git_safety.enumerate_filter_neutralization(worktree_path)
            untracked = self._check_untracked(worktree_path, neutralization)
            payload, diff_complete = self._capture_diff(
                worktree_path, neutralization, initial_commit
            )
        except (EvidenceSinkError, _git_safety.GitSafetyError) as exc:
            return self._capture_failed(str(exc))

        complete = diff_complete and not untracked
        sha256_payload = hashlib.sha256(payload).hexdigest()
        bytes_written = len(payload)

        try:
            artifact_id = self._publish(
                output_dir,
                lifecycle_id,
                payload=payload,
                complete=complete,
                sha256_payload=sha256_payload,
                bytes_written=bytes_written,
            )
        except _CollisionError as exc:
            return EvidenceReceipt(
                success=False,
                complete=False,
                artifact_id=None,
                sha256_payload=None,
                bytes_written=None,
                error=self._error(ErrorCode.EVIDENCE_ARTIFACT_COLLISION, str(exc), lifecycle_id),
            )
        except _DurabilityAmbiguousError as exc:
            # ADR 0003 Amendment 2's terminal precedence: EVIDENCE_INCOMPLETE
            # outranks EVIDENCE_DURABILITY_UNCONFIRMED. If the capture was
            # already incomplete (truncated, or an untracked path was
            # found) before this ambiguous-housekeeping publish even ran,
            # that takes precedence over the housekeeping ambiguity —
            # `complete` is never true here as a side effect of that
            # choice (see EvidenceCaptured's own validation, which now
            # enforces this pairing structurally).
            if not complete:
                code = ErrorCode.EVIDENCE_INCOMPLETE
                message = "capture was incomplete"
            else:
                code = ErrorCode.EVIDENCE_DURABILITY_UNCONFIRMED
                message = str(exc)
            return EvidenceReceipt(
                success=False,
                complete=complete,
                artifact_id=exc.artifact_id,
                sha256_payload=exc.sha256_payload,
                bytes_written=exc.bytes_written,
                error=self._error(code, message, lifecycle_id),
            )
        except EvidenceSinkError as exc:
            return self._capture_failed(str(exc))

        if not complete:
            return EvidenceReceipt(
                success=False,
                complete=False,
                artifact_id=artifact_id,
                sha256_payload=sha256_payload,
                bytes_written=bytes_written,
                error=self._error(ErrorCode.EVIDENCE_INCOMPLETE, "capture was incomplete", lifecycle_id),
            )

        return EvidenceReceipt(
            success=True,
            complete=True,
            artifact_id=artifact_id,
            sha256_payload=sha256_payload,
            bytes_written=bytes_written,
            error=None,
        )

    def _capture_failed(self, message: str) -> EvidenceReceipt:
        return EvidenceReceipt(
            success=False,
            complete=False,
            artifact_id=None,
            sha256_payload=None,
            bytes_written=None,
            error=self._error(ErrorCode.EVIDENCE_CAPTURE_FAILED, message, "capture"),
        )

    @staticmethod
    def _error(code: ErrorCode, message: str, discriminator: str) -> OperationalError:
        return OperationalError(
            code=code, error_id=f"evidence-{discriminator}-{code.value}", message=message
        )

    def _prepare_output_dir(self, worktree_path: Path, source_repo_path: Path) -> Path:
        _validate_no_symlink_ancestors(self._output_root)
        output_dir = self._output_root.resolve()
        resolved_worktree = worktree_path.resolve()
        resolved_source = source_repo_path.resolve()
        _validate_containment(output_dir, resolved_worktree)
        _validate_containment(output_dir, resolved_source)

        try:
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(output_dir, 0o700)
            st = output_dir.stat()
        except OSError as exc:
            raise EvidenceSinkError(
                "the evidence output directory could not be created or verified"
            ) from exc
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise EvidenceSinkError("the evidence output directory is not exactly mode 0700")
        if st.st_uid != os.getuid():
            raise EvidenceSinkError("the evidence output directory is not owned by this process")
        return output_dir

    def _check_untracked(
        self, worktree_path: Path, neutralization: _git_safety.FilterNeutralization
    ) -> bool:
        try:
            result = _git_safety.run_git_bounded_preview(
                worktree_path,
                *neutralization.args,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                limit=MAX_STATUS_BYTES,
            )
        except _git_safety.GitSafetyError as exc:
            raise EvidenceSinkError(
                "the worktree's untracked-path state could not be determined"
            ) from exc
        if not result.complete:
            # Cannot safely rule out untracked paths — never silently
            # treated as "none found."
            raise EvidenceSinkError(
                "the worktree's untracked-path listing exceeded the fixed safety bound"
            )
        return _has_untracked_path(result.stdout)

    def _capture_diff(
        self,
        worktree_path: Path,
        neutralization: _git_safety.FilterNeutralization,
        initial_commit: str,
    ) -> tuple[bytes, bool]:
        result = _git_safety.run_git_bounded_preview(
            worktree_path,
            *neutralization.args,
            "diff",
            "--binary",
            "--full-index",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            initial_commit,
            "--",
            limit=MAX_DIFF_BYTES,
        )
        return result.stdout, result.complete

    def _publish(
        self,
        output_dir: Path,
        lifecycle_id: str,
        *,
        payload: bytes,
        complete: bool,
        sha256_payload: str,
        bytes_written: int,
    ) -> str:
        artifact_id = lifecycle_id
        final_path = output_dir / f"{artifact_id}{ARTIFACT_SUFFIX}"
        if final_path.exists():
            raise _CollisionError(
                f"an evidence artifact already exists for lifecycle {lifecycle_id!r}"
            )

        payload_tmp = output_dir / f".{lifecycle_id}.payload.tmp"
        artifact_tmp = output_dir / f".{lifecycle_id}.artifact.tmp"

        try:
            payload_fd = _open_private_temp(payload_tmp)
            try:
                _write_all(payload_fd, payload)
                os.fsync(payload_fd)
            finally:
                os.close(payload_fd)

            header_bytes = _build_header(
                lifecycle_id=lifecycle_id,
                complete=complete,
                bytes_written=bytes_written,
                sha256_payload=sha256_payload,
            )
            artifact_fd = _open_private_temp(artifact_tmp)
            try:
                _write_all(artifact_fd, ARTIFACT_MAGIC)
                _write_all(artifact_fd, len(header_bytes).to_bytes(4, "big"))
                _write_all(artifact_fd, header_bytes)
                _write_all(artifact_fd, payload)
                os.fsync(artifact_fd)
            finally:
                os.close(artifact_fd)
        except (OSError, EvidenceSinkError) as exc:
            _best_effort_unlink(payload_tmp)
            _best_effort_unlink(artifact_tmp)
            raise EvidenceSinkError("the evidence artifact could not be written") from exc

        try:
            os.link(artifact_tmp, final_path)
        except FileExistsError as exc:
            _best_effort_unlink(payload_tmp)
            _best_effort_unlink(artifact_tmp)
            raise _CollisionError(
                f"an evidence artifact already exists for lifecycle {lifecycle_id!r}"
            ) from exc
        except OSError as exc:
            _best_effort_unlink(payload_tmp)
            _best_effort_unlink(artifact_tmp)
            raise EvidenceSinkError("the evidence artifact could not be published") from exc

        # Published: final_path is now real and durable. Everything
        # from here on is housekeeping only — never delete or overwrite
        # final_path in response to any failure below.
        ambiguous = not _best_effort_unlink(artifact_tmp)
        ambiguous = not _best_effort_unlink(payload_tmp) or ambiguous
        try:
            dir_fd = os.open(str(output_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            ambiguous = True

        if ambiguous:
            raise _DurabilityAmbiguousError(
                "the evidence artifact was published but its housekeeping could not be "
                "confirmed",
                artifact_id=artifact_id,
                sha256_payload=sha256_payload,
                bytes_written=bytes_written,
            )
        return artifact_id
