"""Abandonment markers (ADR 0004 section 11, Amendment 18).

`runs/<lifecycle-id>/abandonment.json` is an exclusive, durable,
immutable administrative record. It is never rewritten and never
touched again automatically; the lifecycle projection beside it is
never rewritten by abandonment.

Publication (Amendment 18, refining section 11's `O_EXCL` wording): a
fully written and `fsync`ed private temporary file
`.abandonment.json.tmp-<16 hex>` is hard-linked to the final name -- a
no-replace operation -- then the directory is `fsync`ed, the exact
temporary twin is removed, and the directory is `fsync`ed again. The
marker is logically installed the moment the link succeeds.

Reading rules:
- only a final `abandonment.json` decides the marker path; a recognized
  temporary file alone never proves abandonment, never refuses, and
  never blocks (it is validated, counted, reported, and left in place);
- a final marker must be a private regular file with `st_nlink == 1`,
  or `st_nlink == 2` with exactly one recognized temporary twin of the
  same inode (a crash between link and unlink);
- at most `ABANDONMENT_TEMP_MAX` stale temporary files are tolerated.

The free-form operator reason is persisted only in the marker. It is
excluded from `repr`, and no other module stores, logs, or prints it.

Leaf module: imports only `_lifecycle_fs`. It performs no Docker, Git,
container, worktree, ref, or projection operation.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, unique

from ._lifecycle_fs import (
    ExclusivePublication,
    LifecycleFsError,
    LifecycleFsFailure,
    _cloexec_flag,
    _dominant_cleanup,
    _nofollow_flag,
    canonical_json_dumps,
    canonical_json_loads_strict,
    close_confirmed,
    publish_private_file_exclusively_at,
    read_all_eintr_safe,
)

MARKER_FILENAME = "abandonment.json"
MARKER_TEMP_RE = re.compile(r"^\.abandonment\.json\.tmp-[0-9a-f]{16}$")
MARKER_MAX_BYTES = 4096
REASON_MAX_BYTES = 256
# 0-15 recognized stale temporary files never refuse or block; 16 refuses
# a new abandonment attempt (so CodeAgent itself can never create a
# 17th); more than 16 makes reconciliation fail closed.
ABANDONMENT_TEMP_MAX = 16

RESOURCE_FIELDS = (
    "baseline_container",
    "verification_container",
    "worktree_registration",
    "worktree_admin_entry",
    "worktree_directory",
    "checkpoint_ref",
)
RESOURCE_VALUES = frozenset({"absent", "present", "unknown"})

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_BASE_KEYS = frozenset(
    {"schema_version", "lifecycle_id", "repo_key", "state_root_id", "disposition", "maintenance_id", "timestamp"}
)
_UNRESOLVED_KEYS = _BASE_KEYS | {"reason", "remaining"}
_REJECTED_REASON_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
_REASON_INVALID = (
    "the reason must be nonempty, at most 256 UTF-8 bytes, and contain no control, format, "
    "line/paragraph-separator, or surrogate characters"
)


@unique
class AbandonmentDisposition(str, Enum):
    ABANDONED = "ABANDONED"
    ABANDONED_UNRESOLVED = "ABANDONED_UNRESOLVED"


@dataclass(frozen=True)
class AbandonmentMarker:
    lifecycle_id: str
    repo_key: str
    state_root_id: str
    disposition: AbandonmentDisposition
    maintenance_id: str
    timestamp: str
    # Never in repr: operator-controlled text may contain a path (ADR 0004
    # section 12 forbids absolute host paths outside the marker).
    reason: str | None = field(default=None, repr=False)
    remaining: Mapping[str, str] | None = None


@unique
class AbandonmentMarkerFailure(str, Enum):
    REFUSED = "refused"
    SUBSTRATE_UNAVAILABLE = "substrate_unavailable"
    ALREADY_EXISTS = "already_exists"
    PUBLICATION_FAILED = "publication_failed"
    TEMP_CLEANUP_UNCONFIRMED = "temp_cleanup_unconfirmed"


class AbandonmentMarkerError(Exception):
    """`reason` is the stable identifier; `message` is fixed categorical
    text only -- never a path, raw OS text, or marker content."""

    def __init__(self, reason: AbandonmentMarkerFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def validate_reason(text: object) -> str:
    """Reject, never truncate. Returns `text` unchanged when valid. The
    error message is fixed and never echoes the input."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(_REASON_INVALID)
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(_REASON_INVALID) from None
    if len(encoded) > REASON_MAX_BYTES:
        raise ValueError(_REASON_INVALID)
    if any(unicodedata.category(ch) in _REJECTED_REASON_CATEGORIES for ch in text):
        raise ValueError(_REASON_INVALID)
    return text


def _refused(message: str) -> AbandonmentMarkerError:
    return AbandonmentMarkerError(AbandonmentMarkerFailure.REFUSED, message)


def _validate_remaining(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(RESOURCE_FIELDS):
        raise _refused("abandonment.json has an invalid remaining-resource summary")
    if any(not isinstance(v, str) or v not in RESOURCE_VALUES for v in value.values()):
        raise _refused("abandonment.json has an invalid remaining-resource summary")
    if all(v == "absent" for v in value.values()):
        raise _refused("abandonment.json records an unresolved disposition with nothing remaining")
    return dict(value)


def _validate_payload(
    payload: object, *, expected_lifecycle_id: str, expected_repo_key: str, expected_state_root_id: str
) -> AbandonmentMarker:
    if not isinstance(payload, dict):
        raise _refused("abandonment.json is not a JSON object")
    disposition_value = payload.get("disposition")
    try:
        disposition = AbandonmentDisposition(disposition_value)
    except ValueError:
        raise _refused("abandonment.json has an invalid disposition") from None
    expected_keys = _BASE_KEYS if disposition is AbandonmentDisposition.ABANDONED else _UNRESOLVED_KEYS
    if set(payload) != expected_keys:
        raise _refused("abandonment.json has an unexpected key set")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _refused("abandonment.json has an unsupported schema_version")
    for key in ("lifecycle_id", "repo_key", "state_root_id", "maintenance_id"):
        if not isinstance(payload[key], str) or not _HEX32_RE.fullmatch(payload[key]):
            raise _refused("abandonment.json has a malformed identifier")
    if not isinstance(payload["timestamp"], str) or not _TIMESTAMP_RE.fullmatch(payload["timestamp"]):
        raise _refused("abandonment.json has a malformed timestamp")
    if (
        payload["lifecycle_id"] != expected_lifecycle_id
        or payload["repo_key"] != expected_repo_key
        or payload["state_root_id"] != expected_state_root_id
    ):
        raise _refused("abandonment.json's identity does not match its location")
    reason = None
    remaining = None
    if disposition is AbandonmentDisposition.ABANDONED_UNRESOLVED:
        try:
            reason = validate_reason(payload["reason"])
        except ValueError:
            raise _refused("abandonment.json has an invalid reason") from None
        remaining = _validate_remaining(payload["remaining"])
    return AbandonmentMarker(
        lifecycle_id=payload["lifecycle_id"],
        repo_key=payload["repo_key"],
        state_root_id=payload["state_root_id"],
        disposition=disposition,
        maintenance_id=payload["maintenance_id"],
        timestamp=payload["timestamp"],
        reason=reason,
        remaining=remaining,
    )


def marker_to_bytes(marker: AbandonmentMarker) -> bytes:
    """Canonical JSON for `marker`, round-trip validated against the same
    rules the reader enforces, and bounded."""
    payload: dict = {
        "schema_version": 1,
        "lifecycle_id": marker.lifecycle_id,
        "repo_key": marker.repo_key,
        "state_root_id": marker.state_root_id,
        "disposition": AbandonmentDisposition(marker.disposition).value,
        "maintenance_id": marker.maintenance_id,
        "timestamp": marker.timestamp,
    }
    if marker.disposition is AbandonmentDisposition.ABANDONED_UNRESOLVED:
        payload["reason"] = marker.reason
        payload["remaining"] = dict(marker.remaining or {})
    _validate_payload(
        payload,
        expected_lifecycle_id=marker.lifecycle_id,
        expected_repo_key=marker.repo_key,
        expected_state_root_id=marker.state_root_id,
    )
    data = canonical_json_dumps(payload)
    if len(data) > MARKER_MAX_BYTES:
        raise _refused("abandonment.json would exceed its size bound")
    return data


def _lstat_or_unavailable(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise AbandonmentMarkerError(
            AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "an abandonment file could not be inspected"
        ) from None


def _require_private_regular(st: os.stat_result, what: str) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise _refused(f"{what} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise _refused(f"{what} is not a regular file")
    if st.st_uid != os.getuid():
        raise _refused(f"{what} is not owned by the current user")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise _refused(f"{what} does not have mode 0600")


def _same_inode_temp_twins(run_dir_fd: int, names: Iterable[str], final_stat: os.stat_result) -> int:
    twins = 0
    for name in names:
        if not MARKER_TEMP_RE.fullmatch(name):
            continue
        st = _lstat_or_unavailable(name, run_dir_fd)
        if st is not None and os.path.samestat(st, final_stat):
            twins += 1
    return twins


def load_abandonment_marker(
    run_dir_fd: int,
    *,
    names: Iterable[str],
    expected_lifecycle_id: str,
    expected_repo_key: str,
    expected_state_root_id: str,
) -> AbandonmentMarker | None:
    """Load and validate the final marker beneath `run_dir_fd`. `names` is
    the caller's single listing of the run directory. Returns `None` iff
    there is no final `abandonment.json` (temporary files alone always
    yield `None`). Raises `AbandonmentMarkerError(REFUSED)` for any
    positively observed invalid marker and `SUBSTRATE_UNAVAILABLE` for an
    inspection failure. Never writes or deletes anything."""
    names = list(names)
    final_stat = _lstat_or_unavailable(MARKER_FILENAME, run_dir_fd)
    if final_stat is None:
        return None
    _require_private_regular(final_stat, "abandonment.json")
    try:
        fd = os.open(MARKER_FILENAME, os.O_RDONLY | _nofollow_flag() | _cloexec_flag(), dir_fd=run_dir_fd)
    except OSError:
        raise AbandonmentMarkerError(
            AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "abandonment.json could not be opened"
        ) from None
    try:
        try:
            held = os.fstat(fd)
        except OSError:
            raise AbandonmentMarkerError(
                AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "abandonment.json could not be inspected"
            ) from None
        if not os.path.samestat(held, final_stat):
            raise _refused("abandonment.json changed while it was being opened")
        _require_private_regular(held, "abandonment.json")
        if held.st_nlink == 2:
            if _same_inode_temp_twins(run_dir_fd, names, held) != 1:
                raise _refused("abandonment.json is hard-linked without exactly one recognized temporary twin")
        elif held.st_nlink != 1:
            raise _refused("abandonment.json has an unexpected link count")
        if held.st_size > MARKER_MAX_BYTES:
            raise _refused("abandonment.json exceeds its size bound")
        try:
            data = read_all_eintr_safe(fd, MARKER_MAX_BYTES)
            payload = canonical_json_loads_strict(data, max_bytes=MARKER_MAX_BYTES)
        except LifecycleFsError as exc:
            if exc.reason is LifecycleFsFailure.IO_FAILED:
                raise AbandonmentMarkerError(
                    AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "abandonment.json could not be read"
                ) from None
            raise _refused("abandonment.json is not valid canonical JSON") from None
        marker = _validate_payload(
            payload,
            expected_lifecycle_id=expected_lifecycle_id,
            expected_repo_key=expected_repo_key,
            expected_state_root_id=expected_state_root_id,
        )
    except BaseException as exc:
        try:
            _dominant_cleanup([fd], exc)
        except LifecycleFsError:
            raise AbandonmentMarkerError(
                AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "abandonment.json could not be confirmed closed"
            ) from exc
        raise
    try:
        close_confirmed([fd])
    except LifecycleFsError:
        raise AbandonmentMarkerError(
            AbandonmentMarkerFailure.SUBSTRATE_UNAVAILABLE, "abandonment.json could not be confirmed closed"
        ) from None
    return marker


def count_stale_marker_temps(run_dir_fd: int, names: Iterable[str]) -> int:
    """Validate and count recognized temporary marker files when no final
    marker exists. Each must be a private regular file with
    `st_nlink == 1` (otherwise `REFUSED`; an inspection failure is
    `SUBSTRATE_UNAVAILABLE`). Counting stops at `ABANDONMENT_TEMP_MAX + 1`,
    so the work is bounded. Never opens, trusts, or deletes a temp."""
    count = 0
    for name in names:
        if not MARKER_TEMP_RE.fullmatch(name):
            continue
        st = _lstat_or_unavailable(name, run_dir_fd)
        if st is None:
            continue
        _require_private_regular(st, "an abandonment temporary file")
        if st.st_nlink != 1:
            raise _refused("an abandonment temporary file has an unexpected link count")
        count += 1
        if count > ABANDONMENT_TEMP_MAX:
            break
    return count


def publish_abandonment_marker(run_dir_fd: int, marker: AbandonmentMarker) -> ExclusivePublication:
    """Publish `marker` exclusively. Raises `AbandonmentMarkerError` only
    before the final link succeeds (nothing installed): `ALREADY_EXISTS`,
    `PUBLICATION_FAILED`, or `TEMP_CLEANUP_UNCONFIRMED` (one stale temp
    left). Once linked, it returns which later stage was confirmed."""
    data = marker_to_bytes(marker)
    try:
        return publish_private_file_exclusively_at(run_dir_fd, MARKER_FILENAME, data, mode=0o600)
    except FileExistsError:
        raise AbandonmentMarkerError(
            AbandonmentMarkerFailure.ALREADY_EXISTS, "abandonment.json already exists"
        ) from None
    except LifecycleFsError as exc:
        if exc.reason is LifecycleFsFailure.CLEANUP_UNCONFIRMED:
            raise AbandonmentMarkerError(
                AbandonmentMarkerFailure.TEMP_CLEANUP_UNCONFIRMED,
                "abandonment.json was not installed, and its temporary file could not be confirmed removed",
            ) from None
        raise AbandonmentMarkerError(
            AbandonmentMarkerFailure.PUBLICATION_FAILED, "abandonment.json could not be installed"
        ) from None
