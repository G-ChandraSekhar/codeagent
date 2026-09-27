"""Milestone 3 Slice 3B-6: shared, strict Docker container-ownership
observation.

Generalizes the identical strict `docker ps -a`/`docker inspect` output
parsing that previously existed as independent, hand-written copies in
`codeagent.executor` (`_parse_cleanup_listing`) and
`codeagent.reconciliation` (`_docker_ps_all_id_name_pairs`/
`_docker_inspect_ownership`, Slice 3B-5). Depends only on
`codeagent._bounded_subprocess` — never on `codeagent.lifecycle_store`
or anything else in the persistence stack, so `executor.py` can use this
module without pulling that stack in.

Exposes both the pure parsers (`parse_ps_all_output`/
`parse_inspect_output`, operating on already-captured stdout bytes) and
full run-and-parse convenience functions
(`docker_ps_all_id_name_pairs`/`docker_inspect_ownership`) that also
launch the subprocess via `codeagent._bounded_subprocess.
run_bounded_stdout`. `codeagent.reconciliation` deliberately keeps
issuing its own `run_bounded_stdout` call (so its existing tests, which
monkeypatch `reconciliation.run_bounded_stdout` directly, keep working
unchanged) and delegates only the parsing step to this module's pure
parsers; `codeagent.executor`'s new lifecycle-aware code uses the full
convenience functions directly.

Every failure here is a categorical exception (`DockerListingError`/
`DockerInspectError`) — never a silent partial result, and a listing or
inspection that is ambiguous in any way (a duplicate id/name, a
malformed row, non-UTF-8 output, a missing trailing terminator) is
never partially trusted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ._bounded_subprocess import BoundedProcessError, run_bounded_stdout

# A Docker container ID is exactly 64 lowercase hexadecimal ASCII
# characters — never uppercase, never abbreviated.
CONTAINER_ID_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Docker's own container-name grammar: an ASCII alphanumeric first
# character, then any run of ASCII alphanumeric, underscore, period, or
# hyphen.
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_DEFAULT_TIMEOUT_SECONDS = 30.0
# A full, unfiltered `docker ps -a --no-trunc` listing on a busy host
# can be large; matches the bound both prior independent copies of this
# call already used.
_DEFAULT_LISTING_MAX_BYTES = 1024 * 1024
# One ownership-proof `docker inspect` result (id, name, and a JSON
# labels object) is small but not itself size-pinned by Docker.
_DEFAULT_INSPECT_MAX_BYTES = 16 * 1024

_PS_ALL_ARGV: tuple[str, ...] = ("docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}")


def _inspect_argv(candidate_id: str) -> list[str]:
    return [
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        '{{.Id}}{{"\t"}}{{.Name}}{{"\t"}}{{json .Config.Labels}}',
        candidate_id,
    ]


class DockerListingError(Exception):
    """A `docker ps -a` listing failed, timed out, exceeded its output
    bound, or was not strictly well-formed (a malformed row, a
    duplicate id/name, invalid UTF-8, or a missing trailing line
    terminator)."""


class DockerInspectError(Exception):
    """An ownership-proof `docker inspect` of one candidate id failed,
    timed out, exceeded its output bound, or was not strictly
    well-formed. Never treated as confirmed absence — a genuine
    inspection failure, including the candidate having vanished in a
    race, must be classified as a substrate-unavailable retry
    condition by the caller, never guessed at."""


@dataclass(frozen=True)
class InspectOwnership:
    id: str
    name: str
    labels: dict[str, str]


def parse_ps_all_output(raw: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Strictly parse `docker ps -a --no-trunc --format
    '{{.ID}}\\t{{.Names}}'`'s stdout: one ID/name record per nonempty
    line, every ID exactly 64 lowercase hexadecimal characters, every
    name matching Docker's own container-name grammar. A duplicate id
    or duplicate name anywhere in the listing makes the whole listing
    untrusted. Once output is nonempty, every row must be a genuine
    record — a blank row (leading, internal, or an extra trailing one
    beyond the single required final line terminator) is fail-closed
    rejected, never silently skipped. Returns both directions
    (`name -> id`, `id -> name`)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DockerListingError("docker listing produced invalid UTF-8 output") from exc

    if text and not text.endswith("\n"):
        raise DockerListingError("docker listing output was missing its final line terminator")

    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    # Split strictly on `\n` only -- never `str.splitlines()`, which also
    # treats `\r` and several Unicode line separators as row boundaries
    # and would silently absorb a CRLF-terminated row as if it were the
    # expected bare-LF shape.
    for line in text.split("\n")[:-1] if text else []:
        if not line:
            raise DockerListingError("a docker listing contained a blank row")
        fields = line.split("\t")
        if len(fields) != 2:
            raise DockerListingError("a docker listing row was not the expected two-field shape")
        raw_id, raw_name = fields
        if not CONTAINER_ID_HEX_RE.fullmatch(raw_id):
            raise DockerListingError("a docker listing row's id was not the expected 64-lowercase-hex shape")
        if not CONTAINER_NAME_RE.fullmatch(raw_name):
            raise DockerListingError("a docker listing row's name was not the expected shape")
        if raw_id in id_to_name or raw_name in name_to_id:
            raise DockerListingError("a docker listing contained a duplicate id or name")
        name_to_id[raw_name] = raw_id
        id_to_name[raw_id] = raw_name
    return name_to_id, id_to_name


def parse_inspect_output(raw: bytes) -> InspectOwnership:
    """Strictly parse one ownership-proof `docker inspect --format
    '{{.Id}}\\t{{.Name}}\\t{{json .Config.Labels}}'` row for exactly one
    candidate."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DockerInspectError("docker inspect produced invalid UTF-8 output") from exc
    if text.count("\n") != 1 or not text.endswith("\n"):
        raise DockerInspectError("docker inspect output was not the expected single-line shape")
    if "\r" in text:
        # A CRLF row's `\r` would otherwise survive as trailing
        # whitespace the JSON decoder silently tolerates after a
        # complete value -- rejected explicitly rather than relying on
        # that decoder's own leniency to ever catch it.
        raise DockerInspectError("docker inspect output contained a carriage return")
    fields = text[:-1].split("\t")
    if len(fields) != 3:
        raise DockerInspectError("docker inspect output was not the expected three-field shape")
    raw_id, raw_name, raw_labels_json = fields
    if not CONTAINER_ID_HEX_RE.fullmatch(raw_id):
        raise DockerInspectError("docker inspect id was not the expected 64-lowercase-hex shape")
    # `docker inspect`'s `.Name` always carries exactly one leading `/`
    # for a container's primary name.
    if not raw_name.startswith("/") or raw_name.startswith("//"):
        raise DockerInspectError("docker inspect name did not have exactly one leading '/'")
    name = raw_name[1:]
    if not CONTAINER_NAME_RE.fullmatch(name):
        raise DockerInspectError("docker inspect name was not the expected shape")
    try:
        labels = json.loads(raw_labels_json)
    except json.JSONDecodeError as exc:
        raise DockerInspectError("docker inspect labels were not valid JSON") from exc
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise DockerInspectError("docker inspect labels were not a flat string-keyed object")
    return InspectOwnership(id=raw_id, name=name, labels=labels)


def docker_ps_all_id_name_pairs(
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = _DEFAULT_LISTING_MAX_BYTES,
) -> tuple[dict[str, str], dict[str, str]]:
    """One bounded, unfiltered, timeout-controlled, no-shell
    `docker ps -a` listing of every container (running or stopped) —
    never a name-filtered or label-filtered query, so ownership must
    always be proven afterward by inspection, never assumed from the
    listing alone. Runs the command itself and then strictly parses via
    `parse_ps_all_output`."""
    try:
        result = run_bounded_stdout(list(_PS_ALL_ARGV), timeout_seconds=timeout_seconds, stdout_limit=output_limit)
    except BoundedProcessError as exc:
        raise DockerListingError("docker listing failed, timed out, or exceeded its output bound") from exc
    if result.returncode != 0:
        raise DockerListingError("docker listing exited with a nonzero status")
    return parse_ps_all_output(result.stdout)


def docker_inspect_ownership(
    candidate_id: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = _DEFAULT_INSPECT_MAX_BYTES,
) -> InspectOwnership:
    """One bounded, timeout-controlled, no-shell ownership-proof
    `docker inspect` of exactly one candidate, by its immutable id —
    never by name (a name can be reused). A timeout, overflow, or
    nonzero exit (including the candidate having vanished in a race) is
    never treated as confirmed absence. Runs the command itself and then
    strictly parses via `parse_inspect_output`."""
    try:
        result = run_bounded_stdout(_inspect_argv(candidate_id), timeout_seconds=timeout_seconds, stdout_limit=output_limit)
    except BoundedProcessError as exc:
        raise DockerInspectError("docker inspect failed, timed out, or exceeded its output bound") from exc
    if result.returncode != 0:
        raise DockerInspectError("docker inspect could not confirm the candidate container")
    return parse_inspect_output(result.stdout)
