"""
CodeAgent Stage 2 -- S5 spike: interruption, cancellation, and
orphan-resource reconciliation.

Implements ONLY Planning Proposal Revision 4 (this session's history).
Every mechanism here -- labels, the durable manifest, advisory locks,
signal handling, reconciliation -- is spike-only scaffolding. Current
production `codeagent.executor.DockerVerifier` and
`codeagent.workspace.GitWorktree` have NONE of this: no labels, no
cancellation wiring, no durable manifest, no signal handling, no
startup reconciler. Nothing here is imported from or written into
src/codeagent; `codeagent.executor.DEFAULT_IMAGE` is imported
READ-ONLY, purely so this spike exercises the same pinned digest
already used elsewhere, never an independently retyped tag.

Ownership model: the outer harness (this file's `main()` with no
arguments) creates only the throwaway source Git repository, the
session scratch root, and the four canaries. Every dedicated child
(`--child` mode) creates and owns ITS OWN worktree and container, and
is normally responsible for tearing both down itself -- this is what
makes SIGKILL a genuine test: the code that would have cleaned up
simply never runs.

Note on GitWorktree: `codeagent.workspace.GitWorktree.__enter__` always
allocates its own `tempfile.TemporaryDirectory` and has no parameter
for a caller-supplied destination -- it structurally cannot implement
this experiment's child-owned, precomputed-path worktree. This spike
therefore reimplements a minimal, spike-local worktree lifecycle using
the same structured-argv, no-shell, exact-registration-check
discipline `GitWorktree` already follows, reused as a PRINCIPLE only,
never as a shared code path, and `src/codeagent/workspace.py` is not
modified.

Manifest state machine (child-writable, intent-first -- every state
bracketing a real side effect is written BEFORE that side effect):

    PREPARING -> WORKTREE_CREATING -> WORKTREE_CREATED
              -> CONTAINER_CREATING -> CONTAINER_CREATED
              -> READY -> CLEANING -> COMPLETE

Reconciler-writable states:

    ORPHANED -> RECONCILING -> RECONCILED
                             -> RECONCILIATION_FAILED  (retryable, NOT terminal)

COMPLETE and RECONCILED are the only permanent terminals: a reconciler
encountering either skips immediately, with zero lock, inspection, or
mutation. RECONCILIATION_FAILED is retried on every later pass with
fresh inspection of both resources -- the manifest's own
`worktree_created`/`container_created` flags are never authoritative;
they are historical narrative only. Live inspection controls action.
"""
from __future__ import annotations

import copy
import fcntl
import functools
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
SPIKE_DIR = THIS_FILE.parent

sys.path.insert(0, str(SPIKE_DIR.parents[1] / "src"))
from codeagent.executor import DEFAULT_IMAGE  # noqa: E402 -- read-only import, never modified

RUN_TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")

TERMINAL_STATES = ("COMPLETE", "RECONCILED")
POST_ACTION_ACK_FIELDS = (
    "cleaning_started_at",
    "complete_at",
    "cancellation_observed_at",
    "sigint_received_at",
    "sigterm_received_at",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def platform_key() -> str:
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        return f"macos-docker-desktop-{machine}"
    if system == "Linux":
        return f"linux-{machine}"
    return f"{system.lower()}-{machine}"


def is_supported_platform() -> tuple[bool, str]:
    """Darwin (any machine, matching every macOS run gathered so far)
    or Linux/x86_64 specifically -- never Linux on any other
    architecture, and never any other OS. Kept as a pure, independently
    testable predicate rather than inlined into run_full_experiment so
    a future platform addition/removal is a one-function change."""
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        return True, f"Darwin/{machine} supported"
    if system == "Linux" and machine == "x86_64":
        return True, "Linux/x86_64 supported"
    if system == "Linux":
        return False, f"Linux/{machine} is not supported (only Linux/x86_64)"
    return False, f"{system}/{machine} is not supported"


class ProvenanceIncomplete(Exception):
    """Raised when a required piece of host/workflow/git provenance
    cannot be collected or fails strict validation. Always converted to
    overall_verdict TECHNICAL_FAILURE (never silently degraded to an
    'unknown' value that would still permit a PASS) wherever it is
    caught -- see run_full_experiment's except/finally handling."""


S5_BASELINE_COMMIT = "d2a6f639826bb1368cc88a6233394b0c6b0ca7da"
_DIGITS_ONLY_RE = re.compile(r"^[0-9]+$")


def check_ancestor(candidate_commit: str, repo: str) -> tuple[str, str]:
    """Independent `git merge-base --is-ancestor` check with exact,
    non-collapsed exit-code interpretation: 0 means confirmed ancestor,
    1 means CONFIRMED NOT an ancestor (a real, meaningful negative
    answer -- not a technical failure), and any other exit code means
    the check itself is broken (a technical failure, not an answer at
    all). Returns (status, detail) with status in
    {"ancestor", "not_ancestor", "technical_failure"}."""
    result = run(["git", "-C", repo, "merge-base", "--is-ancestor", candidate_commit, "HEAD"])
    if result.returncode == 0:
        return "ancestor", ""
    if result.returncode == 1:
        return "not_ancestor", f"git merge-base --is-ancestor confirmed {candidate_commit} is NOT an ancestor of HEAD"
    return (
        "technical_failure",
        f"git merge-base --is-ancestor exited {result.returncode} (neither 0 nor 1) -- the check itself is broken",
    )


_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_server_url(url: str) -> str:
    """Requires an absolute http(s) URL with a nonempty host, no
    embedded credentials, and no query string or fragment (rejected
    outright -- never silently discarded while still accepting the
    rest of the URL as valid). Returns the URL normalized with any
    trailing slash removed (so `workflow_url` construction never
    produces a doubled `//`). Raises ValueError with a fixed, non-
    echoing message on any problem -- GITHUB_SERVER_URL is CI-
    controlled, not attacker-secret, but there is no reason to echo
    malformed input back into evidence either."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("GITHUB_SERVER_URL must be an absolute http(s) URL")
    if not parsed.hostname:
        raise ValueError("GITHUB_SERVER_URL must have a nonempty host")
    if parsed.username or parsed.password:
        raise ValueError("GITHUB_SERVER_URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("GITHUB_SERVER_URL must not contain a query string or fragment")
    normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return normalized.rstrip("/")


def _validate_repository(repository: str) -> str:
    """Requires exactly one owner/name pair: both components nonempty,
    no whitespace, no query string or fragment, no traversal-like
    ('.'/'..') components, no extra path segments, and restricted to
    GitHub's own allowed repository-name character set."""
    if not repository or repository != repository.strip() or any(c.isspace() for c in repository):
        raise ValueError("GITHUB_REPOSITORY must be nonempty and contain no whitespace")
    if "?" in repository or "#" in repository:
        raise ValueError("GITHUB_REPOSITORY must not contain a query string or fragment")
    parts = repository.split("/")
    if len(parts) != 2:
        raise ValueError("GITHUB_REPOSITORY must be exactly one owner/name pair")
    owner, name = parts
    if not owner or not name:
        raise ValueError("GITHUB_REPOSITORY owner and name must both be nonempty")
    if owner in (".", "..") or name in (".", ".."):
        raise ValueError("GITHUB_REPOSITORY must not contain traversal-like components")
    if not _REPO_COMPONENT_RE.fullmatch(owner) or not _REPO_COMPONENT_RE.fullmatch(name):
        raise ValueError("GITHUB_REPOSITORY components contain disallowed characters")
    return repository


def validate_actions_context(env) -> dict:
    """Fail-closed validation of the four GitHub Actions provenance
    variables. Returns {"present": False} ONLY when none of the four
    are set at all (a genuinely local, non-Actions invocation, which
    may legitimately fall back to local UUID evidence-directory
    naming). Any other combination -- one or more present but not all
    four well-formed -- raises ProvenanceIncomplete: a partial or
    malformed Actions context is refused outright, never silently
    downgraded to local-UUID naming that could be mistaken for a
    genuinely local run.

    IMPORTANT: a ProvenanceIncomplete raised BY THIS FUNCTION, when
    called from run_full_experiment's own precondition check (before
    any evidence directory exists), is never converted into a harness
    summary.json/overall_verdict -- there is no evidence-directory or
    finalization scope for it to be recorded into yet. It propagates
    as an uncaught exception (nonzero process exit), and in the
    workflow it is caught by the shell-level `--validate-actions-
    context` preflight before the harness's real experiment ever
    starts. Only a ProvenanceIncomplete raised AFTER that scope is
    established (e.g. host-provenance collection, or scenario-5
    filesystem observation, both deep inside the try/except/finally)
    produces overall_verdict TECHNICAL_FAILURE."""
    run_id = env.get("GITHUB_RUN_ID")
    run_attempt = env.get("GITHUB_RUN_ATTEMPT")
    server_url = env.get("GITHUB_SERVER_URL")
    repository = env.get("GITHUB_REPOSITORY")

    if not any((run_id, run_attempt, server_url, repository)):
        return {"present": False}

    problems = []
    if not run_id or not _DIGITS_ONLY_RE.fullmatch(run_id):
        problems.append(f"GITHUB_RUN_ID missing or non-numeric: {run_id!r}")
    if not run_attempt or not _DIGITS_ONLY_RE.fullmatch(run_attempt):
        problems.append(f"GITHUB_RUN_ATTEMPT missing or non-numeric: {run_attempt!r}")

    normalized_server_url = None
    if not server_url:
        problems.append("GITHUB_SERVER_URL missing")
    else:
        try:
            normalized_server_url = _validate_server_url(server_url)
        except ValueError as exc:
            problems.append(str(exc))

    if not repository:
        problems.append("GITHUB_REPOSITORY missing")
    else:
        try:
            _validate_repository(repository)
        except ValueError as exc:
            problems.append(str(exc))

    if problems:
        raise ProvenanceIncomplete(f"partial/malformed GitHub Actions context: {'; '.join(problems)}")

    return {
        "present": True,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_url": f"{normalized_server_url}/{repository}/actions/runs/{run_id}",
    }


def evidence_run_dir_name(actions_context: dict) -> str:
    if actions_context.get("present"):
        return f"run-{actions_context['run_id']}-attempt-{actions_context['run_attempt']}"
    return f"run-{uuid.uuid4().hex[:12]}"


def docker_client_version() -> str:
    result = run(["docker", "version", "--format", "{{.Client.Version}}"])
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ProvenanceIncomplete(f"docker client version collection failed: exit={result.returncode}")
    return value


def docker_server_version() -> str:
    result = run(["docker", "version", "--format", "{{.Server.Version}}"])
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ProvenanceIncomplete(f"docker server version collection failed: exit={result.returncode}")
    return value


def docker_daemon_cgroup_version() -> str:
    """Docker's OWN reported cgroup version (the daemon's view), not an
    inference from the host's /sys/fs/cgroup -- this is what actually
    governs container resource accounting, and on Docker Desktop for
    macOS it correctly reflects the VM's kernel, not the macOS host's
    (which has no cgroups at all)."""
    result = run(["docker", "info", "--format", "{{json .CgroupVersion}}"])
    if result.returncode != 0:
        raise ProvenanceIncomplete(f"docker info cgroup-version collection failed: exit={result.returncode}")
    try:
        value = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise ProvenanceIncomplete(f"unparseable docker cgroup-version output: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise ProvenanceIncomplete(f"empty/invalid docker cgroup-version value: {value!r}")
    return value


def collect_kernel_and_architecture() -> tuple[str, str]:
    kernel_release = platform.release()
    machine = platform.machine()
    if not kernel_release or not machine:
        raise ProvenanceIncomplete(
            f"platform.release()/platform.machine() returned an empty value: {kernel_release!r}/{machine!r}"
        )
    return kernel_release, machine


@dataclass
class FilesystemObservation:
    ok: bool
    resolved_path: str | None = None
    mountpoint: str | None = None
    fs_type: str | None = None
    error: str | None = None


def observe_filesystem_linux(path: Path) -> FilesystemObservation:
    """Linux-only: resolves an EXISTING path with `Path.resolve(strict=
    True)` and queries `findmnt --json` for the containing mountpoint
    and filesystem type. Deliberately requires the path to already
    exist -- this must never be called for a scenario-5 lock file or
    worktree before the dedicated child has actually created it and
    reached the READY barrier; calling it earlier would either raise on
    a nonexistent path or (worse) silently observe the WRONG, enclosing
    directory's filesystem instead of the real target once it exists.
    `findmnt --target <path>` itself already resolves to the innermost
    (longest-prefix) containing mount, so no separate manual longest-
    match logic is needed here.

    Strictly requires, in order: the `findmnt` command to succeed;
    its stdout to be valid JSON; a `filesystems` array with EXACTLY one
    record (zero means findmnt matched nothing, more than one is an
    unexpected/ambiguous shape neither of which this harness trusts);
    that record to be an object with nonempty STRING `target` and
    `fstype` fields. Every failure mode returns a fixed, categorical,
    sanitized `error` string only -- never raw findmnt stdout/stderr
    and never a host path -- so a caller that folds this into
    `technical_failure_reason` (itself potentially retained in evidence
    and workflow logs) never leaks unvalidated command output or
    filesystem layout detail."""
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError:
        return FilesystemObservation(ok=False, error="path_does_not_exist")
    result = run(["findmnt", "--json", "--output", "TARGET,FSTYPE", "--target", str(resolved)])
    if result.returncode != 0:
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_command_failed")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_output_not_valid_json")
    if not isinstance(data, dict) or not isinstance(data.get("filesystems"), list):
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_json_missing_filesystems_array")
    filesystems = data["filesystems"]
    if len(filesystems) != 1:
        return FilesystemObservation(
            ok=False, resolved_path=str(resolved), error="findmnt_json_did_not_contain_exactly_one_record"
        )
    record = filesystems[0]
    if not isinstance(record, dict):
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_json_record_not_an_object")
    mountpoint = record.get("target")
    fs_type = record.get("fstype")
    if not isinstance(mountpoint, str) or not mountpoint:
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_json_target_missing_or_empty")
    if not isinstance(fs_type, str) or not fs_type:
        return FilesystemObservation(ok=False, resolved_path=str(resolved), error="findmnt_json_fstype_missing_or_empty")
    return FilesystemObservation(ok=True, resolved_path=str(resolved), mountpoint=mountpoint, fs_type=fs_type)


_DOCKER_ID_RE = re.compile(r"^[0-9a-f]+$")


def normalize_container_listing(container_ids: list[str]) -> list[dict]:
    """Takes exact container IDs (as produced by `docker ps -aq
    --no-trunc`, one per line) and independently `docker inspect`s
    EACH ONE via structured argv, returning a normalized,
    deterministically sorted list keyed on STABLE identity fields
    only -- exact container ID, exact name, the immutable image ID,
    and CodeAgent-relevant labels.

    This deliberately does NOT use `docker ps --format '{{json .}}'`'s
    comma-joined `.Labels` string: a comma embedded in one label's
    value does not just fail to round-trip cleanly -- it can produce
    ANOTHER syntactically valid `key=value` pair from the remainder,
    which passes a naive round-trip check while silently misattributing
    label data. Full structured `docker inspect` JSON has no such
    ambiguity, since Config.Labels is a real JSON object.

    Every inspect result is parsed ONLY in memory -- the full inspect
    object, `Config.Env`, mounts, and any other field beyond the four
    listed above are never retained or printed, since they can contain
    container environment values.

    Fails closed (raises ValueError) on: a duplicate or malformed
    (non-hex) input ID; a failed `docker inspect` invocation; malformed
    JSON; a result count other than exactly one; an inspected ID that
    does not match the requested ID; or a missing/wrong-typed identity
    field (Id/Name/Image) or Labels object. An empty `container_ids`
    list is a valid, normalized empty inventory -- not an error."""
    if len(set(container_ids)) != len(container_ids):
        raise ValueError("duplicate container ID in input list")

    entries = []
    for container_id in container_ids:
        if not isinstance(container_id, str) or not container_id or not _DOCKER_ID_RE.fullmatch(container_id):
            raise ValueError("malformed (non-hex) container ID in input list")

        result = run(["docker", "inspect", container_id])
        if result.returncode != 0:
            raise ValueError("docker inspect failed for a requested container ID")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("docker inspect produced malformed JSON") from exc
        if not isinstance(data, list) or len(data) != 1:
            raise ValueError("docker inspect did not return exactly one result")
        record = data[0]
        if not isinstance(record, dict):
            raise ValueError("docker inspect result is not an object")

        actual_id = record.get("Id")
        name = record.get("Name")
        image_id = record.get("Image")
        if not isinstance(actual_id, str) or not actual_id:
            raise ValueError("docker inspect result has a missing/invalid Id field")
        if actual_id != container_id:
            raise ValueError("docker inspect result Id does not match the requested container ID")
        if not isinstance(name, str) or not name:
            raise ValueError("docker inspect result has a missing/invalid Name field")
        if not isinstance(image_id, str) or not image_id:
            raise ValueError("docker inspect result has a missing/invalid Image field")

        config = record.get("Config")
        if not isinstance(config, dict):
            raise ValueError("docker inspect result has a missing/invalid Config object")
        labels = config.get("Labels")
        if labels is None:
            labels = {}
        if not isinstance(labels, dict):
            raise ValueError("docker inspect result has a non-object Config.Labels field")

        codeagent_labels: dict[str, str] = {}
        for k, v in labels.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("a Config.Labels key/value is not a string")
            if k.startswith("codeagent."):
                codeagent_labels[k] = v

        entries.append({"id": actual_id, "name": name.lstrip("/"), "image": image_id, "codeagent_labels": codeagent_labels})
    entries.sort(key=lambda e: e["id"])
    return entries


# --------------------------------------------------------------------
# Identity: recomputed from trusted inputs, never trusted from a file.
# --------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    run_token: str
    session_id: str
    scratch_root: Path
    source_repo_path: str
    lock_path: Path
    manifest_path: Path
    control_path: Path
    worktree_path: Path
    container_name: str
    labels: dict[str, str]


def compute_identity(
    trusted_scratch_root: Path, trusted_source_repo: Path, trusted_session_id: str, run_token: str
) -> Identity:
    if not RUN_TOKEN_RE.fullmatch(run_token):
        raise ValueError(f"invalid run_token shape: {run_token!r}")
    scratch_root = Path(trusted_scratch_root).resolve()
    source_repo = Path(trusted_source_repo).resolve()
    return Identity(
        run_token=run_token,
        session_id=trusted_session_id,
        scratch_root=scratch_root,
        source_repo_path=str(source_repo),
        lock_path=scratch_root / "locks" / f"{run_token}.lock",
        manifest_path=scratch_root / "manifest" / f"{run_token}.json",
        control_path=scratch_root / "control" / f"{run_token}.json",
        worktree_path=scratch_root / "worktrees" / run_token,
        container_name=f"codeagent-spike-s5-{run_token}",
        labels={
            "codeagent.spike": "s5",
            "codeagent.s5_session": trusted_session_id,
            "codeagent.s5_run": run_token,
        },
    )


def identity_mismatches(expected: Identity, manifest: dict) -> list[str]:
    """Every safety-relevant field the manifest claims, compared
    against what was independently recomputed from trusted inputs.
    A mismatch on any of these means the manifest is not to be
    trusted for this entry."""
    checks = {
        "lock_path": str(expected.lock_path),
        "worktree_path": str(expected.worktree_path),
        "container_name": expected.container_name,
        "labels": expected.labels,
        "source_repo_path": expected.source_repo_path,
    }
    return [name for name, value in checks.items() if manifest.get(name) != value]


def path_is_contained(path: Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _codeagent_labels(labels: dict | None) -> dict:
    return {k: v for k, v in (labels or {}).items() if k.startswith("codeagent.")}


def labels_match_subset(observed: dict | None, expected: dict) -> bool:
    """True if every expected codeagent.* label is present with the
    expected value -- tolerant of unrelated extra labels."""
    observed = _codeagent_labels(observed)
    return all(observed.get(k) == v for k, v in expected.items())


def labels_match_exact(observed: dict | None, expected: dict) -> bool:
    """Exact equality restricted to the codeagent.* namespace -- used
    for canary drift detection, where the recorded expected state may
    legitimately be 'no CodeAgent labels at all'."""
    return _codeagent_labels(observed) == _codeagent_labels(expected)


# --------------------------------------------------------------------
# Atomic manifest/control documents: same-directory temp file, fsync,
# os.replace -- a reader never observes a torn write.
# --------------------------------------------------------------------


class ManifestCorruptError(Exception):
    pass


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_manifest(path: Path) -> dict:
    try:
        content = path.read_text()
    except OSError as exc:
        raise ManifestCorruptError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ManifestCorruptError(f"malformed manifest json {path}: {exc}") from exc


def safe_read_manifest(path: Path) -> dict | None:
    try:
        return read_manifest(path)
    except ManifestCorruptError:
        return None


def write_manifest_state(
    identity: Identity, manifest: dict, *, state: str, snapshot_dir: Path | None = None, **updates
) -> dict:
    manifest = dict(manifest)
    manifest["state"] = state
    manifest["updated_at"] = now_iso()
    manifest.update(updates)
    atomic_write_json(identity.manifest_path, manifest)
    if snapshot_dir is not None:
        # Best-effort, evidence-only: a copy of each write-ahead
        # transition, so the sequence of real side effects (a worktree
        # add only ever happening after WORKTREE_CREATING is durably
        # written, etc.) is directly demonstrable after the fact, not
        # just asserted. Never authoritative for any decision.
        try:
            atomic_write_json(snapshot_dir / f"{identity.run_token}-{state}.json", manifest)
        except OSError:
            pass
    return manifest


# --------------------------------------------------------------------
# Advisory lock (fcntl.flock -- not POSIX-specified; a BSD/Linux
# flock(2) facility, available identically via Python's fcntl module
# on macOS and Linux, released automatically by the kernel when a
# process's file descriptors close, including on SIGKILL). Behavior is
# empirically checked by the real experiment, never merely assumed.
# --------------------------------------------------------------------


def open_lock_file(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return open(lock_path, "a+")


def try_lock_nonblocking(fd) -> bool:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def release_lock(fd) -> None:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# --------------------------------------------------------------------
# Live resource observation -- unfiltered listings + exact matching,
# never Docker's own --filter, never a prefix/substring check. A
# failed listing/inspect is reported as not-ok, never as "absent".
# --------------------------------------------------------------------


@dataclass
class ContainerObservation:
    ok: bool
    present: bool
    id: str | None = None
    labels: dict | None = None
    error: str | None = None


def docker_all_container_names() -> tuple[bool, set[str]]:
    result = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.Names}}"])
    if result.returncode != 0:
        return False, set()
    return True, {line.strip() for line in result.stdout.splitlines() if line.strip()}


def raw_docker_ps_a() -> tuple[bool, str]:
    """Exact, unfiltered `docker ps -a --no-trunc` output, retained
    verbatim as evidence (never just a derived boolean)."""
    result = run(["docker", "ps", "-a", "--no-trunc"])
    return result.returncode == 0, result.stdout


def raw_git_worktree_list(repo: str) -> tuple[bool, str]:
    """Exact, unfiltered `git worktree list --porcelain` output for a
    given repo, retained verbatim."""
    result = run(["git", "-C", repo, "worktree", "list", "--porcelain"])
    return result.returncode == 0, result.stdout


def observe_container(name: str) -> ContainerObservation:
    ok, names = docker_all_container_names()
    if not ok:
        return ContainerObservation(ok=False, present=False, error="docker ps -a listing failed")
    if name not in names:
        return ContainerObservation(ok=True, present=False)
    result = run(["docker", "inspect", name])
    if result.returncode != 0:
        return ContainerObservation(ok=False, present=True, error="docker inspect failed after confirmed presence")
    try:
        data = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        return ContainerObservation(ok=False, present=True, error=f"unparseable inspect output: {exc}")
    labels = (data.get("Config") or {}).get("Labels") or {}
    return ContainerObservation(ok=True, present=True, id=data.get("Id"), labels=labels)


@dataclass
class WorktreeObservation:
    ok: bool
    registered: bool = False
    directory_exists: bool = False
    error: str | None = None


def observe_worktree(source_repo: str, worktree_path: Path) -> WorktreeObservation:
    result = run(["git", "-C", source_repo, "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return WorktreeObservation(ok=False, error="git worktree list failed")
    target = str(Path(worktree_path).resolve())
    registered = any(
        line[len("worktree ") :] == target
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    )
    directory_exists = Path(worktree_path).is_dir()
    return WorktreeObservation(ok=True, registered=registered, directory_exists=directory_exists)


def remove_container_and_confirm(name: str) -> bool:
    run(["docker", "rm", "--force", name])
    ok, names = docker_all_container_names()
    return ok and name not in names


def remove_worktree_and_confirm(source_repo: str, worktree_path: Path) -> bool:
    """Structured `git worktree remove --force` only -- no shutil.rmtree
    fallback. A leftover directory or registration after this is a
    genuine RECONCILIATION_FAILED, never patched over."""
    result = run(["git", "-C", source_repo, "worktree", "remove", "--force", str(worktree_path)])
    if result.returncode != 0:
        return False
    obs = observe_worktree(source_repo, worktree_path)
    return obs.ok and not obs.registered and not obs.directory_exists


# --------------------------------------------------------------------
# Preflight: independently inspect BOTH resources, unconditionally,
# regardless of the manifest's own flags. Any inspection failure or
# identity mismatch on EITHER resource aborts the whole entry with
# zero mutation.
# --------------------------------------------------------------------


@dataclass
class PreflightResult:
    ok: bool
    worktree_action: str  # "none" | "remove" | "mismatch"
    container_action: str  # "none" | "remove" | "mismatch"
    detail: list[str]
    worktree_obs: WorktreeObservation | None = None
    container_obs: ContainerObservation | None = None


def preflight(expected: Identity, manifest: dict) -> PreflightResult:
    detail: list[str] = []
    contained = path_is_contained(expected.worktree_path, expected.scratch_root)
    if not contained:
        detail.append("worktree_path escapes scratch root")

    wt_obs = observe_worktree(expected.source_repo_path, expected.worktree_path)
    c_obs = observe_container(expected.container_name)

    worktree_action = "mismatch"
    if not contained:
        pass
    elif not wt_obs.ok:
        detail.append(f"worktree inspection failed: {wt_obs.error}")
    elif wt_obs.registered and wt_obs.directory_exists:
        worktree_action = "remove"
    elif not wt_obs.registered and not wt_obs.directory_exists:
        worktree_action = "none"
    else:
        detail.append("worktree registration/directory-existence mismatch")

    container_action = "mismatch"
    if not c_obs.ok:
        detail.append(f"container inspection failed: {c_obs.error}")
    elif not c_obs.present:
        container_action = "none"
    else:
        expected_id = manifest.get("container_id")
        id_ok = (not expected_id) or (expected_id == c_obs.id)
        label_ok = labels_match_subset(c_obs.labels, expected.labels)
        if not id_ok:
            detail.append("container_id mismatch")
        if not label_ok:
            detail.append("container label mismatch")
        if id_ok and label_ok:
            container_action = "remove"

    ok = (
        contained
        and wt_obs.ok
        and c_obs.ok
        and worktree_action != "mismatch"
        and container_action != "mismatch"
    )
    return PreflightResult(ok, worktree_action, container_action, detail, wt_obs, c_obs)


# --------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------


def process_entry(expected: Identity, manifest: dict) -> dict:
    lock_fd = open_lock_file(expected.lock_path)
    if not try_lock_nonblocking(lock_fd):
        lock_fd.close()
        return {"run_token": expected.run_token, "action": "skipped_active"}

    try:
        original_snapshot = copy.deepcopy(manifest)  # taken from the AS-READ document, before any state write

        # Incremented exactly once per ACQUIRED reconciliation attempt
        # -- including a first-attempt success -- and carried unchanged
        # through every manifest write this attempt makes below, so a
        # single attempt is never double-counted across its own
        # ORPHANED -> RECONCILING -> terminal sequence.
        attempts = manifest.get("reconciliation_attempts", 0) + 1

        pf = preflight(expected, manifest)

        if not pf.ok:
            new_manifest = write_manifest_state(
                expected,
                manifest,
                state="RECONCILIATION_FAILED",
                failure_phase="pre_mutation_validation",
                reconciliation_attempts=attempts,
                failure_history=manifest.get("failure_history", [])
                + [{"at": now_iso(), "phase": "pre_mutation_validation", "detail": pf.detail}],
            )
            return {
                "run_token": expected.run_token,
                "action": "reconciliation_failed",
                "phase": "pre_mutation_validation",
                "snapshot": original_snapshot,
                "manifest": new_manifest,
            }

        manifest = write_manifest_state(expected, manifest, state="ORPHANED", reconciliation_attempts=attempts)
        manifest = write_manifest_state(expected, manifest, state="RECONCILING", reconciliation_attempts=attempts)

        if pf.container_action == "remove":
            if not remove_container_and_confirm(expected.container_name):
                manifest = write_manifest_state(
                    expected,
                    manifest,
                    state="RECONCILIATION_FAILED",
                    failure_phase="post_mutation_cleanup",
                    worktree_removal_skipped=True,
                    reconciliation_attempts=attempts,
                    failure_history=manifest.get("failure_history", [])
                    + [{"at": now_iso(), "phase": "post_mutation_cleanup", "detail": "container removal not confirmed"}],
                )
                return {
                    "run_token": expected.run_token,
                    "action": "reconciliation_failed",
                    "phase": "post_mutation_cleanup",
                    "snapshot": original_snapshot,
                    "manifest": manifest,
                }

        if pf.worktree_action == "remove":
            if not remove_worktree_and_confirm(expected.source_repo_path, expected.worktree_path):
                manifest = write_manifest_state(
                    expected,
                    manifest,
                    state="RECONCILIATION_FAILED",
                    failure_phase="post_mutation_cleanup",
                    reconciliation_attempts=attempts,
                    failure_history=manifest.get("failure_history", [])
                    + [{"at": now_iso(), "phase": "post_mutation_cleanup", "detail": "worktree removal not confirmed"}],
                )
                return {
                    "run_token": expected.run_token,
                    "action": "reconciliation_failed",
                    "phase": "post_mutation_cleanup",
                    "snapshot": original_snapshot,
                    "manifest": manifest,
                }

        manifest = write_manifest_state(expected, manifest, state="RECONCILED", reconciliation_attempts=attempts)
        return {
            "run_token": expected.run_token,
            "action": "reconciled",
            "snapshot": original_snapshot,
            "manifest": manifest,
        }
    finally:
        release_lock(lock_fd)


def reconcile(trusted_scratch_root: Path, trusted_source_repo: str, trusted_session_id: str) -> dict:
    scratch_root = Path(trusted_scratch_root).resolve()
    manifest_dir = scratch_root / "manifest"
    results: list[dict] = []

    if not manifest_dir.exists():
        return {"ok": True, "results": []}

    try:
        entries = sorted(manifest_dir.iterdir())
    except OSError as exc:
        return {"ok": False, "error": f"cannot list manifest directory: {exc}", "results": []}

    for path in entries:
        if path.name.endswith(".tmp"):
            results.append({"run_token": path.name[: -len(".json.tmp")], "action": "informational_leftover_tmp"})
            continue
        if not path.name.endswith(".json"):
            continue
        run_token = path.name[: -len(".json")]

        if not RUN_TOKEN_RE.fullmatch(run_token):
            results.append({"run_token": run_token, "action": "technical_failure", "detail": "malformed run_token"})
            continue

        try:
            manifest = read_manifest(path)
        except ManifestCorruptError as exc:
            results.append({"run_token": run_token, "action": "technical_failure", "detail": str(exc)})
            continue

        if manifest.get("state") in TERMINAL_STATES:
            results.append({"run_token": run_token, "action": "skipped_terminal", "state": manifest.get("state")})
            continue

        try:
            expected = compute_identity(scratch_root, trusted_source_repo, trusted_session_id, run_token)
        except ValueError as exc:
            results.append({"run_token": run_token, "action": "technical_failure", "detail": str(exc)})
            continue

        mismatches = identity_mismatches(expected, manifest)
        if mismatches:
            results.append(
                {"run_token": run_token, "action": "technical_failure", "detail": f"identity mismatch: {mismatches}"}
            )
            continue

        results.append(process_entry(expected, manifest))

    return {"ok": True, "results": results}


_SUCCESSFUL_RECONCILE_ACTIONS = ("skipped_terminal", "skipped_active", "reconciled", "informational_leftover_tmp")


def reconcile_run_has_problem(result: dict) -> bool:
    """True if the standalone --reconcile command should exit nonzero:
    the run-level listing itself failed, or any entry is
    technical_failure/reconciliation_failed. skipped_terminal,
    skipped_active, reconciled, and an informational leftover .tmp
    note are all successful outcomes for the command's own exit
    status -- only genuine problems fail it."""
    if not result.get("ok", False):
        return True
    return any(r.get("action") not in _SUCCESSFUL_RECONCILE_ACTIONS for r in result.get("results", []))


# --------------------------------------------------------------------
# Cooperative control-file protocol (spike-only mechanism -- never
# presented as a production cancellation API).
# --------------------------------------------------------------------


def evaluate_control_document(
    raw: dict | None, expected_run_token: str, already_consumed: bool
) -> tuple[str, str | None, str | None]:
    """Returns (outcome, action, reject_reason). outcome is one of
    "absent", "accept", "reject". Fail-closed: anything not an exact,
    well-formed, first-time match for THIS run_token is rejected, never
    defaulted to any action."""
    if raw is None:
        return "absent", None, None
    if already_consumed:
        return "reject", None, "duplicate"
    if not isinstance(raw, dict) or "run_token" not in raw or "action" not in raw:
        return "reject", None, "malformed"
    if raw["run_token"] != expected_run_token:
        return "reject", None, "foreign_run_token"
    if raw["action"] not in ("continue", "cancel"):
        return "reject", None, "unsupported_action"
    return "accept", raw["action"], None


def read_control_document(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}  # malformed but present -- evaluate_control_document rejects this shape too


# --------------------------------------------------------------------
# Dedicated child driver
# --------------------------------------------------------------------


class _CooperativeFlags:
    def __init__(self) -> None:
        self.sigint = False
        self.sigterm = False


def child_main(
    scenario: str,
    session_id: str,
    run_token: str,
    scratch_root: Path,
    source_repo: str,
    snapshot_dir: Path | None = None,
) -> int:
    identity = compute_identity(scratch_root, source_repo, session_id, run_token)
    _write = functools.partial(write_manifest_state, snapshot_dir=snapshot_dir)
    lock_fd = open_lock_file(identity.lock_path)
    if not try_lock_nonblocking(lock_fd):
        print("FATAL: could not acquire own lock -- run_token collision", file=sys.stderr)
        return 2

    manifest = {
        "run_token": identity.run_token,
        "session_id": identity.session_id,
        "source_repo_path": identity.source_repo_path,
        "lock_path": str(identity.lock_path),
        "worktree_path": str(identity.worktree_path),
        "container_name": identity.container_name,
        "container_id": None,
        "labels": identity.labels,
        "pid": os.getpid(),
        "scenario": scenario,
        "worktree_created": False,
        "container_created": False,
        "reconciliation_attempts": 0,
        "failure_history": [],
        "created_at": now_iso(),
    }
    manifest = _write(identity, manifest, state="PREPARING")

    flags = _CooperativeFlags()

    def _on_sigint(signum, frame) -> None:
        flags.sigint = True

    def _on_sigterm(signum, frame) -> None:
        flags.sigterm = True

    # Handlers only ever set a flag -- all cleanup runs from the main
    # loop's normal control flow below, never from inside a handler.
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        manifest = _write(identity, manifest, state="WORKTREE_CREATING")
        source_head = run(["git", "-C", identity.source_repo_path, "rev-parse", "HEAD"]).stdout.strip()
        identity.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        wt_result = run(
            ["git", "-C", identity.source_repo_path, "worktree", "add", "--detach", str(identity.worktree_path), source_head]
        )
        if wt_result.returncode != 0:
            print(f"FATAL: worktree add failed: {wt_result.stderr}", file=sys.stderr)
            return 3
        manifest = _write(identity, manifest, state="WORKTREE_CREATED", worktree_created=True)

        manifest = _write(identity, manifest, state="CONTAINER_CREATING")
        create = run(
            [
                "docker",
                "create",
                "--name",
                identity.container_name,
                "--label",
                f"codeagent.spike={identity.labels['codeagent.spike']}",
                "--label",
                f"codeagent.s5_session={identity.labels['codeagent.s5_session']}",
                "--label",
                f"codeagent.s5_run={identity.labels['codeagent.s5_run']}",
                "--network",
                "none",
                "--memory",
                "64m",
                "--pids-limit",
                "16",
                DEFAULT_IMAGE,
                "sleep",
                "infinity",
            ]
        )
        if create.returncode != 0:
            print(f"FATAL: docker create failed: {create.stderr}", file=sys.stderr)
            return 4
        container_id = create.stdout.strip()
        # Persist the created container's identity IMMEDIATELY, before
        # attempting to start it -- if `docker start` (or anything
        # after it) fails or the process dies, reconciliation must
        # still be able to discover this exact container by name/ID,
        # never only by a flag that might not have been written yet.
        manifest = _write(
            identity, manifest, state="CONTAINER_CREATED", container_created=True, container_id=container_id
        )

        start = run(["docker", "start", identity.container_name])
        if start.returncode != 0:
            # Fail closed: stay at the last successfully-written
            # NONTERMINAL state (CONTAINER_CREATED is not in
            # TERMINAL_STATES) so reconciliation still independently
            # inspects and reconciles this container/worktree -- never
            # write READY or COMPLETE for a container that isn't
            # actually running. Only a fixed, sanitized phase/detail is
            # persisted, never raw daemon stderr.
            _write(
                identity,
                manifest,
                state="CONTAINER_CREATED",
                failure_phase="start_failed",
                failure_detail="docker start did not succeed",
            )
            print("FATAL: docker start failed", file=sys.stderr)
            return 6

        manifest = _write(identity, manifest, state="READY", ready_barrier_ack_at=now_iso())

        action = None
        consumed = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not consumed:
                raw = read_control_document(identity.control_path)
                outcome, candidate_action, reason = evaluate_control_document(raw, identity.run_token, consumed)
                if outcome == "reject":
                    manifest = _write(
                        identity, manifest, state="READY", control_rejected_at=now_iso(), control_reject_reason=reason
                    )
                elif outcome == "accept":
                    consumed = True
                    action = candidate_action
                    ack_field = "cancellation_observed_at" if action == "cancel" else "continue_received_at"
                    manifest = _write(
                        identity, manifest, state="READY", control_action_consumed_at=now_iso(), **{ack_field: now_iso()}
                    )
                    break
            if flags.sigint:
                action = "sigint"
                manifest = _write(identity, manifest, state="READY", sigint_received_at=now_iso())
                break
            if flags.sigterm:
                action = "sigterm"
                manifest = _write(identity, manifest, state="READY", sigterm_received_at=now_iso())
                break
            time.sleep(0.02)

        if action is None:
            print("FATAL: no action received within deadline", file=sys.stderr)
            return 5

        return perform_child_cleanup(identity, manifest, action, snapshot_dir=snapshot_dir)
    finally:
        release_lock(lock_fd)


def perform_child_cleanup(
    identity: Identity, manifest: dict, action: str, *, snapshot_dir: Path | None = None
) -> int:
    """The child's own normal-path teardown: container first, confirmed,
    before the worktree is ever touched -- mirrors the reconciler's own
    ordering discipline, and for the same reason: never remove a host
    path that might still be bind-mounted into a running container.
    Fails closed: COMPLETE is written only if BOTH removals are
    independently confirmed. Any failure leaves a NONTERMINAL manifest
    state (CLEANING is never in TERMINAL_STATES) with a fixed,
    sanitized failure_phase/failure_detail, so a later reconciliation
    pass still picks this entry up -- it can never become permanently
    skipped. Pulled out of child_main so it is directly unit-testable
    without driving the full control-file/signal wait loop.
    """
    _write = functools.partial(write_manifest_state, snapshot_dir=snapshot_dir)
    manifest = _write(identity, manifest, state="CLEANING", cleaning_started_at=now_iso())

    if not remove_container_and_confirm(identity.container_name):
        _write(
            identity,
            manifest,
            state="CLEANING",
            failure_phase="cleanup_container_failed",
            failure_detail="container removal not confirmed",
        )
        print("FATAL: container removal not confirmed -- worktree cleanup skipped", file=sys.stderr)
        return 7

    if not remove_worktree_and_confirm(identity.source_repo_path, identity.worktree_path):
        _write(
            identity,
            manifest,
            state="CLEANING",
            failure_phase="cleanup_worktree_failed",
            failure_detail="worktree removal not confirmed",
        )
        print("FATAL: worktree removal not confirmed", file=sys.stderr)
        return 8

    _write(identity, manifest, state="COMPLETE", complete_at=now_iso(), outcome=action)
    return 0


# --------------------------------------------------------------------
# Outer harness: canaries, six scenarios, reconciliation, idempotency,
# independent emergency cleanup.
# --------------------------------------------------------------------


def wait_for_ready(identity: Identity, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manifest = safe_read_manifest(identity.manifest_path)
        if manifest and manifest.get("state") == "READY" and manifest.get("ready_barrier_ack_at"):
            wt = observe_worktree(identity.source_repo_path, identity.worktree_path)
            c = observe_container(identity.container_name)
            if wt.ok and wt.registered and wt.directory_exists and c.ok and c.present:
                return True
        time.sleep(0.05)
    return False


def snapshot_canaries(inventory: list[dict]) -> dict:
    snap = {}
    for entry in inventory:
        if entry["kind"] == "container":
            obs = observe_container(entry["name"])
            snap[entry["name"]] = {"present": obs.present, "id": obs.id, "labels": _codeagent_labels(obs.labels)}
        elif entry["kind"] == "child":
            identity = entry["identity"]
            wt = observe_worktree(identity.source_repo_path, identity.worktree_path)
            c = observe_container(identity.container_name)
            snap[identity.run_token] = {
                "worktree_registered": wt.registered,
                "worktree_dir": wt.directory_exists,
                "container_present": c.present,
                "container_labels": _codeagent_labels(c.labels),
            }
    return snap


def redact_inventory(inventory: list[dict]) -> list[dict]:
    out = []
    for entry in inventory:
        e = {"kind": entry["kind"]}
        if entry["kind"] == "container":
            e.update({"name": entry["name"], "id": entry["id"], "expected_labels": entry["expected_labels"]})
        else:
            identity = entry["identity"]
            e.update(
                {
                    "run_token": identity.run_token,
                    "container_name": identity.container_name,
                    "worktree_path": str(identity.worktree_path),
                    "pid": entry["popen"].pid,
                }
            )
        out.append(e)
    return out


def _emergency_cleanup_container_entry(entry: dict) -> dict:
    obs = observe_container(entry["name"])
    if not obs.ok:
        return {"kind": "container", "name": entry["name"], "action": "technical_failure", "detail": obs.error}
    if not obs.present:
        return {"kind": "container", "name": entry["name"], "action": "already_absent"}
    matches = obs.id == entry["id"] and labels_match_exact(obs.labels, entry["expected_labels"])
    if not matches:
        return {"kind": "container", "name": entry["name"], "action": "refused_identity_drift"}
    removed = remove_container_and_confirm(entry["name"])
    return {"kind": "container", "name": entry["name"], "action": "removed" if removed else "removal_unconfirmed"}


def _emergency_cleanup_child_entry(entry: dict, source_repo: str) -> dict:
    identity = entry["identity"]
    proc = entry["popen"]
    atomic_write_json(identity.control_path, {"run_token": identity.run_token, "action": "cancel"})
    mode = "cooperative"
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        mode = "forced"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

    manifest = safe_read_manifest(identity.manifest_path)
    if mode == "cooperative" and manifest and manifest.get("state") == "COMPLETE":
        return {"kind": "child", "run_token": identity.run_token, "mode": mode, "action": "child_self_cleaned"}

    c_obs = observe_container(identity.container_name)
    container_result = "not_present"
    if c_obs.ok and c_obs.present and labels_match_subset(c_obs.labels, identity.labels):
        container_result = "removed" if remove_container_and_confirm(identity.container_name) else "removal_unconfirmed"
    wt_obs = observe_worktree(source_repo, identity.worktree_path)
    worktree_result = "not_present"
    if wt_obs.ok and wt_obs.registered:
        worktree_result = (
            "removed" if remove_worktree_and_confirm(source_repo, identity.worktree_path) else "removal_unconfirmed"
        )
    return {
        "kind": "child",
        "run_token": identity.run_token,
        "mode": mode,
        "action": "harness_direct_cleanup",
        "container_result": container_result,
        "worktree_result": worktree_result,
    }


def emergency_cleanup(inventory: list[dict], source_repo: str) -> dict:
    """Independent from reconciliation-under-test: uses only the
    harness's own in-memory creation records, never a generic
    label-based sweep. Every entry is isolated in its own try/except --
    an unexpected exception cleaning up one entry (a failed control-file
    write, a process wait/signal error, an unexpected Docker/Git
    observation or removal failure) is recorded as a sanitized
    technical_failure for THAT entry only, and every remaining entry is
    still attempted."""
    results = []
    for entry in inventory:
        try:
            if entry["kind"] == "container":
                results.append(_emergency_cleanup_container_entry(entry))
            elif entry["kind"] == "child":
                results.append(_emergency_cleanup_child_entry(entry, source_repo))
        except BaseException as exc:  # noqa: BLE001 -- isolate this entry only; every other entry must still run
            identifier = entry.get("name") or (entry["identity"].run_token if entry.get("identity") else None)
            results.append(
                {
                    "kind": entry.get("kind"),
                    "name": identifier if entry.get("kind") == "container" else None,
                    "run_token": identifier if entry.get("kind") == "child" else None,
                    "action": "technical_failure",
                    "detail": f"unexpected exception during cleanup: {type(exc).__name__}",
                }
            )

    def _ok(r: dict) -> bool:
        if r.get("action") in ("removed", "already_absent", "child_self_cleaned"):
            return True
        if r.get("action") == "harness_direct_cleanup":
            return r["container_result"] in ("removed", "not_present") and r["worktree_result"] in ("removed", "not_present")
        return False

    return {"results": results, "all_clean": all(_ok(r) for r in results)}


def run_full_experiment() -> dict:
    supported, platform_reason = is_supported_platform()
    if not supported:
        raise RuntimeError(f"unsupported platform for S5 spike: {platform_reason}")

    codeagent_repo_root = SPIKE_DIR.parents[1]

    # ---- Fail-closed preconditions, checked BEFORE any evidence
    # directory or resource is created. Both are pure environment/git
    # facts, independent of anything this run is about to create, so
    # there is nothing to clean up if either fails here -- exactly like
    # the platform gate above. A partial/malformed Actions context is
    # refused outright, never silently downgraded to a local-UUID-named
    # run that could be mistaken for a genuinely local invocation.
    #
    # IMPORTANT: a ProvenanceIncomplete (or RuntimeError, for the
    # platform gate) raised by either check below propagates as an
    # uncaught exception -- there is no evidence directory, no
    # try/except/finally scope, and no summary.json for it to be
    # recorded into. It is NEVER reported as a harness
    # overall_verdict=TECHNICAL_FAILURE; it is only a nonzero process
    # exit. In the workflow, both facts are additionally (and
    # independently) checked by dedicated shell-level preflight steps
    # before this Python process's real experiment ever starts, so a
    # failure here is caught there too. Only a ProvenanceIncomplete
    # raised LATER -- after the evidence directory and the
    # try/except/finally finalization scope below are established
    # (host-provenance collection, or scenario-5 filesystem
    # observation) -- is caught by that scope and DOES produce
    # overall_verdict=TECHNICAL_FAILURE in a real summary.json. ----
    actions_context = validate_actions_context(os.environ)

    ancestor_status, ancestor_detail = check_ancestor(S5_BASELINE_COMMIT, str(codeagent_repo_root))
    if ancestor_status != "ancestor":
        raise ProvenanceIncomplete(
            f"S5 baseline commit {S5_BASELINE_COMMIT} provenance check did not confirm ancestry: "
            f"status={ancestor_status} detail={ancestor_detail}"
        )

    log_lines: list[str] = []

    def elog(text: str) -> None:
        print(text)
        log_lines.append(text)

    session_id = uuid.uuid4().hex[:12]
    evidence_dir = SPIKE_DIR / "evidence" / platform_key() / evidence_run_dir_name(actions_context)
    evidence_dir.mkdir(parents=True, exist_ok=False)
    snapshot_dir = evidence_dir / "manifest_snapshots"
    snapshot_dir.mkdir()

    def write_evidence(name: str, data) -> None:
        (evidence_dir / name).write_text(json.dumps(data, indent=2, default=str))

    # ---- Baseline, captured before this run creates anything at all ----
    baseline_ps_ok, baseline_ps_raw = raw_docker_ps_a()
    baseline_wt_ok, baseline_wt_raw = raw_git_worktree_list(str(codeagent_repo_root))

    # ---- Provenance: what code actually produced this evidence ----
    harness_bytes = THIS_FILE.read_bytes()
    harness_sha256 = hashlib.sha256(harness_bytes).hexdigest()
    repo_head = run(["git", "-C", str(codeagent_repo_root), "rev-parse", "HEAD"]).stdout.strip()
    repo_status_porcelain = run(["git", "-C", str(codeagent_repo_root), "status", "--porcelain"]).stdout
    run_info = {
        "session_id": session_id,
        "repository_head": repo_head,
        "repository_dirty": bool(repo_status_porcelain.strip()),
        "harness_file": str(THIS_FILE.relative_to(codeagent_repo_root)),
        "harness_sha256": harness_sha256,
        "s5_baseline_commit": S5_BASELINE_COMMIT,
        "s5_baseline_commit_is_ancestor": True,  # otherwise we already raised above
        "workflow_run_id": actions_context.get("run_id"),
        "workflow_run_attempt": actions_context.get("run_attempt"),
        "workflow_run_url": actions_context.get("workflow_url"),
        "actions_context_present": actions_context.get("present", False),
        "note": (
            "repository_head does NOT represent the harness that produced this "
            "evidence unless repository_dirty is false and this exact "
            "harness_sha256 matches the tracked file at that commit -- the "
            "harness may be uncommitted/modified relative to HEAD; harness_sha256 "
            "is the authoritative pointer to the exact code that ran."
        ),
        "triggered_at": now_iso(),
    }
    write_evidence("RUN_INFO.json", run_info)
    elog(f"S5 real experiment starting. session_id={session_id} evidence_dir={evidence_dir}")

    # Outer protection is established IMMEDIATELY after the scratch
    # root itself is created -- everything from here on (fixture repo
    # creation, the pull check, canary/child creation, all six
    # scenarios) runs inside the try block below, so an exception
    # anywhere in setup or execution still reaches the unconditional
    # cleanup/evidence section that follows it.
    session_scratch_root = Path(tempfile.mkdtemp(prefix="codeagent-spike-s5-session-"))
    source_repo = session_scratch_root / "fixture-repo"

    inventory: list[dict] = []
    scenario_results: dict[str, dict] = {}
    scenario5: dict = {}
    scenario6: dict = {}
    idempotency: dict = {}
    canary_snapshot_before: dict = {}
    canary_snapshot_after_6: dict = {}
    sigkill_identity: Identity | None = None
    sigkill_token: str | None = None
    host_info: dict = {}
    experiment_exception: BaseException | None = None

    def docker_run_canary(name: str, labels: dict) -> str:
        argv = ["docker", "run", "-d", "--name", name]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        argv += [DEFAULT_IMAGE, "sleep", "infinity"]
        result = run(argv)
        if result.returncode != 0:
            raise RuntimeError(f"failed to create canary {name}: {result.stderr}")
        return result.stdout.strip()

    try:
        source_repo.mkdir()
        run(["git", "init", "-q", str(source_repo)])
        run(["git", "-C", str(source_repo), "config", "user.email", "s5-spike@example.invalid"])
        run(["git", "-C", str(source_repo), "config", "user.name", "s5-spike"])
        (source_repo / "README.md").write_text("s5 fixture\n")
        run(["git", "-C", str(source_repo), "add", "."])
        run(["git", "-C", str(source_repo), "commit", "-q", "-m", "initial"])

        security_flags = ["--network", "none", "--memory", "64m", "--pids-limit", "16"]
        write_evidence("tested_config.json", {"image": DEFAULT_IMAGE, "container_flags": security_flags})

        pull = run(["docker", "pull", DEFAULT_IMAGE])
        if pull.returncode != 0:
            raise RuntimeError(f"docker pull failed: {pull.stderr}")

        # Fail-closed: each of these raises ProvenanceIncomplete on a
        # missing/failing command or unparseable output rather than
        # recording "unknown" while still permitting a PASS. Required
        # on both platforms -- by this point the fixture repo and
        # session scratch root already exist, so a failure here is
        # handled exactly like any other mid-run exception: caught
        # below, recorded as overall_verdict TECHNICAL_FAILURE, and
        # still fully finalized/cleaned up.
        docker_client = docker_client_version()
        docker_server = docker_server_version()
        cgroup_version = docker_daemon_cgroup_version()
        kernel_release, machine = collect_kernel_and_architecture()
        host_info.update(
            {
                "system": platform.system(),
                "release": kernel_release,
                "machine": machine,
                "python_version": platform.python_version(),
                "docker_client_version": docker_client,
                "docker_server_version": docker_server,
                "docker_daemon_cgroup_version": cgroup_version,
            }
        )
        write_evidence("host.json", host_info)

        canary_a_name = f"codeagent-canary-a-unlabeled-{uuid.uuid4().hex[:8]}"
        inventory.append(
            {"kind": "container", "name": canary_a_name, "id": docker_run_canary(canary_a_name, {}), "expected_labels": {}}
        )

        canary_b_name = f"codeagent-verify-baseline-{uuid.uuid4().hex[:12]}"
        inventory.append(
            {"kind": "container", "name": canary_b_name, "id": docker_run_canary(canary_b_name, {}), "expected_labels": {}}
        )

        foreign_session = uuid.uuid4().hex[:12]
        foreign_run = uuid.uuid4().hex[:12]
        canary_c_labels = {"codeagent.spike": "s5", "codeagent.s5_session": foreign_session, "codeagent.s5_run": foreign_run}
        canary_c_name = f"codeagent-spike-s5-{foreign_run}"
        inventory.append(
            {
                "kind": "container",
                "name": canary_c_name,
                "id": docker_run_canary(canary_c_name, canary_c_labels),
                "expected_labels": canary_c_labels,
            }
        )

        canary_d_token = uuid.uuid4().hex[:12]
        canary_d_identity = compute_identity(session_scratch_root, source_repo, session_id, canary_d_token)
        canary_d_proc = subprocess.Popen(
            [sys.executable, str(THIS_FILE), "--child", "live_canary", session_id, canary_d_token, str(session_scratch_root), str(source_repo), str(snapshot_dir)]
        )
        # Recorded in inventory BEFORE the readiness check: even if D
        # never reaches READY, the process itself (and whatever it
        # already created) is still a harness-owned resource that
        # unconditional cleanup below must attempt.
        inventory.append({"kind": "child", "identity": canary_d_identity, "popen": canary_d_proc})
        if not wait_for_ready(canary_d_identity, timeout=30):
            raise RuntimeError("canary D never reached READY -- aborting run")

        # `canary_inventory` is a SEPARATE snapshot of `inventory` taken
        # right here, deliberately frozen to exactly the four canaries
        # (A, B, C, D) -- `inventory` itself keeps growing below as each
        # scenario/sigkill child is recorded for emergency cleanup, and
        # those are never canaries. Comparing canary state against the
        # wrong (growing) list would make `canaries_unchanged` spuriously
        # False the moment any scenario child is appended.
        canary_inventory = list(inventory)
        write_evidence("canary_inventory.json", redact_inventory(canary_inventory))
        canary_snapshot_before = snapshot_canaries(canary_inventory)

        for scenario_name, action_kind, ack_field in [
            ("normal_completion", "continue", "continue_received_at"),
            ("cooperative_cancellation", "cancel", "cancellation_observed_at"),
            ("sigint", "sigint", "sigint_received_at"),
            ("sigterm", "sigterm", "sigterm_received_at"),
        ]:
            run_token = uuid.uuid4().hex[:12]
            identity = compute_identity(session_scratch_root, source_repo, session_id, run_token)
            proc = subprocess.Popen(
                [sys.executable, str(THIS_FILE), "--child", scenario_name, session_id, run_token, str(session_scratch_root), str(source_repo), str(snapshot_dir)]
            )
            # Recorded immediately, before any readiness wait/signal/
            # control write that could raise -- so an exception in any
            # of that still leaves this exact child in the inventory
            # emergency cleanup below will act on.
            inventory.append({"kind": "child", "identity": identity, "popen": proc})
            result: dict = {"run_token": run_token, "pid": proc.pid}
            if not wait_for_ready(identity, timeout=30):
                proc.kill()
                proc.wait(timeout=10)
                result["classification"] = "TECHNICAL_FAILURE"
                result["reason"] = "READY barrier not reached"
                scenario_results[scenario_name] = result
                write_evidence(f"check_scenario_{scenario_name}.json", result)
                continue

            result["action_sent_at"] = now_iso()
            if action_kind in ("continue", "cancel"):
                atomic_write_json(identity.control_path, {"run_token": run_token, "action": action_kind})
            else:
                os.kill(proc.pid, signal.SIGINT if action_kind == "sigint" else signal.SIGTERM)

            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

            final_manifest = safe_read_manifest(identity.manifest_path)
            wt_obs = observe_worktree(str(source_repo), identity.worktree_path)
            c_obs = observe_container(identity.container_name)
            worktree_absent = wt_obs.ok and not wt_obs.registered and not wt_obs.directory_exists
            container_absent = c_obs.ok and not c_obs.present
            ack_present = bool(final_manifest and final_manifest.get(ack_field))

            result.update(
                {
                    "final_manifest": final_manifest,
                    "worktree_absent": worktree_absent,
                    "container_absent": container_absent,
                    "ack_present": ack_present,
                }
            )
            if not (wt_obs.ok and c_obs.ok):
                result["classification"] = "TECHNICAL_FAILURE"
            elif final_manifest and final_manifest.get("state") == "COMPLETE" and worktree_absent and container_absent and ack_present:
                result["classification"] = "PASS"
            else:
                result["classification"] = "FAIL"
            scenario_results[scenario_name] = result
            write_evidence(f"check_scenario_{scenario_name}.json", result)

        # ---- Scenario 5: SIGKILL ----
        sigkill_token = uuid.uuid4().hex[:12]
        sigkill_identity = compute_identity(session_scratch_root, source_repo, session_id, sigkill_token)
        sigkill_proc = subprocess.Popen(
            [sys.executable, str(THIS_FILE), "--child", "sigkill", session_id, sigkill_token, str(session_scratch_root), str(source_repo), str(snapshot_dir)]
        )
        # Recorded immediately, before the readiness wait/signal below
        # that could raise -- same discipline as the scenario 1-4
        # children. By the time emergency cleanup runs, this entry is
        # normally already resolved (self-cleaned, or reconciled by
        # scenario 6) -- emergency_cleanup safely recognizes either
        # already-resolved state and is a no-op for it; it only truly
        # matters if an exception prevents scenario 6 from ever running.
        inventory.append({"kind": "child", "identity": sigkill_identity, "popen": sigkill_proc})
        scenario5 = {"run_token": sigkill_token, "pid": sigkill_proc.pid}
        if not wait_for_ready(sigkill_identity, timeout=30):
            sigkill_proc.kill()
            sigkill_proc.wait(timeout=10)
            scenario5["classification"] = "TECHNICAL_FAILURE"
            scenario5["reason"] = "READY barrier not reached"
        else:
            pre_manifest = safe_read_manifest(sigkill_identity.manifest_path)

            # Linux-only, and deliberately placed HERE -- only after
            # wait_for_ready has already independently confirmed the
            # child reached READY with its worktree registered/present
            # and its container present, i.e. only once the exact
            # scenario-5 lock file and worktree path are confirmed to
            # exist. Observing them any earlier would either raise on a
            # nonexistent path or silently describe the wrong (parent)
            # directory's filesystem instead. A collection failure here
            # is a required-provenance failure like any other: it
            # raises, is caught below, and still runs full finalization
            # (the still-alive sigkill child is a real resource that
            # emergency cleanup must still tear down).
            if platform.system() == "Linux":
                lock_fs_obs = observe_filesystem_linux(sigkill_identity.lock_path)
                worktree_fs_obs = observe_filesystem_linux(sigkill_identity.worktree_path)
                if not (lock_fs_obs.ok and worktree_fs_obs.ok):
                    raise ProvenanceIncomplete(
                        "scenario-5 Linux filesystem observation failed: "
                        f"lock_ok={lock_fs_obs.ok} lock_error={lock_fs_obs.error} "
                        f"worktree_ok={worktree_fs_obs.ok} worktree_error={worktree_fs_obs.error}"
                    )
                host_info["lock_resolved_path"] = lock_fs_obs.resolved_path
                host_info["lock_mountpoint"] = lock_fs_obs.mountpoint
                host_info["lock_filesystem_type"] = lock_fs_obs.fs_type
                host_info["worktree_resolved_path"] = worktree_fs_obs.resolved_path
                host_info["worktree_mountpoint"] = worktree_fs_obs.mountpoint
                host_info["worktree_filesystem_type"] = worktree_fs_obs.fs_type
                write_evidence("host.json", host_info)

            probe_fd = open_lock_file(sigkill_identity.lock_path)
            busy_before = not try_lock_nonblocking(probe_fd)
            if busy_before:
                probe_fd.close()
            else:
                release_lock(probe_fd)
            wt_before = observe_worktree(str(source_repo), sigkill_identity.worktree_path)
            c_before = observe_container(sigkill_identity.container_name)

            os.kill(sigkill_proc.pid, signal.SIGKILL)
            sigkill_proc.wait(timeout=10)
            died_by_sigkill = sigkill_proc.returncode == -signal.SIGKILL.value

            probe_fd2 = open_lock_file(sigkill_identity.lock_path)
            acquirable_after = try_lock_nonblocking(probe_fd2)
            release_lock(probe_fd2)

            post_manifest = safe_read_manifest(sigkill_identity.manifest_path)
            no_post_ack = bool(post_manifest) and not any(post_manifest.get(f) for f in POST_ACTION_ACK_FIELDS)

            wt_after = observe_worktree(str(source_repo), sigkill_identity.worktree_path)
            c_after = observe_container(sigkill_identity.container_name)
            orphan_confirmed = (
                wt_after.ok
                and wt_after.registered
                and wt_after.directory_exists
                and c_after.ok
                and c_after.present
                and labels_match_subset(c_after.labels, sigkill_identity.labels)
            )

            scenario5.update(
                {
                    "pre": {
                        "manifest_state": pre_manifest.get("state") if pre_manifest else None,
                        "ready_barrier_ack_at": pre_manifest.get("ready_barrier_ack_at") if pre_manifest else None,
                        "lock_busy_before_signal": busy_before,
                        "worktree_registered_before": wt_before.registered if wt_before.ok else None,
                        "container_present_before": c_before.present if c_before.ok else None,
                    },
                    "died_by_sigkill": died_by_sigkill,
                    "lock_acquirable_after": acquirable_after,
                    "no_post_action_ack": no_post_ack,
                    "post": {
                        "manifest": post_manifest,
                        "worktree_registered_after": wt_after.registered if wt_after.ok else None,
                        "worktree_directory_exists_after": wt_after.directory_exists if wt_after.ok else None,
                        "container_present_after": c_after.present if c_after.ok else None,
                        "container_labels_after": c_after.labels if c_after.ok else None,
                    },
                }
            )
            if not (wt_before.ok and c_before.ok and wt_after.ok and c_after.ok):
                scenario5["classification"] = "TECHNICAL_FAILURE"
            elif died_by_sigkill and busy_before and acquirable_after and no_post_ack and orphan_confirmed:
                scenario5["classification"] = "PASS"
            else:
                scenario5["classification"] = "FAIL"
        write_evidence("check_scenario_5_sigkill.json", scenario5)

        # ---- Scenario 6: fresh reconciliation ----
        reconcile_proc = run([sys.executable, str(THIS_FILE), "--reconcile", str(session_scratch_root), str(source_repo), session_id])
        try:
            reconcile_result = json.loads(reconcile_proc.stdout)
        except json.JSONDecodeError:
            reconcile_result = {"ok": False, "raw_stdout": reconcile_proc.stdout, "stderr": reconcile_proc.stderr}
        write_evidence("reconciler_under_test_results.json", reconcile_result)

        post_wt = observe_worktree(str(source_repo), sigkill_identity.worktree_path)
        post_c = observe_container(sigkill_identity.container_name)
        sigkill_entry = next((r for r in reconcile_result.get("results", []) if r.get("run_token") == sigkill_token), None)
        canary_snapshot_after_6 = snapshot_canaries(canary_inventory)
        scenario6 = {
            "reconcile_result": reconcile_result,
            "reconcile_returncode": reconcile_proc.returncode,
            "target_entry": sigkill_entry,
            "worktree_removed": post_wt.ok and not post_wt.registered and not post_wt.directory_exists,
            "container_removed": post_c.ok and not post_c.present,
            "canaries_unchanged": canary_snapshot_before == canary_snapshot_after_6,
            "canary_snapshot_after": canary_snapshot_after_6,
        }
        if (
            sigkill_entry
            and sigkill_entry.get("action") == "reconciled"
            and scenario6["worktree_removed"]
            and scenario6["container_removed"]
            and scenario6["canaries_unchanged"]
        ):
            scenario6["classification"] = "PASS"
        else:
            scenario6["classification"] = "FAIL"
        write_evidence("check_scenario_6_reconciliation.json", scenario6)

        # ---- Idempotency: second fresh reconciliation pass ----
        reconcile_proc2 = run([sys.executable, str(THIS_FILE), "--reconcile", str(session_scratch_root), str(source_repo), session_id])
        try:
            reconcile_result2 = json.loads(reconcile_proc2.stdout)
        except json.JSONDecodeError:
            reconcile_result2 = {"ok": False, "raw_stdout": reconcile_proc2.stdout}
        canary_snapshot_after_idem = snapshot_canaries(canary_inventory)
        sigkill_entry2 = next((r for r in reconcile_result2.get("results", []) if r.get("run_token") == sigkill_token), None)
        idempotency = {
            "reconcile_result": reconcile_result2,
            "reconcile_returncode": reconcile_proc2.returncode,
            "target_entry_action": sigkill_entry2.get("action") if sigkill_entry2 else None,
            "canaries_unchanged": canary_snapshot_after_6 == canary_snapshot_after_idem,
        }
        idempotency["classification"] = (
            "PASS"
            if (sigkill_entry2 and sigkill_entry2.get("action") == "skipped_terminal" and idempotency["canaries_unchanged"])
            else "FAIL"
        )
        write_evidence("check_scenario_6b_idempotency.json", idempotency)
    except BaseException as exc:  # noqa: BLE001 -- stashed; re-raised with its
        # original traceback AFTER the finally block below completes.
        # Never handled/swallowed here -- only recorded. This block
        # performs EXACTLY this one operation and nothing else: even
        # logging is deferred to the already-guarded phase-9 run-log
        # block in `finally`, so nothing fallible can run here and
        # risk replacing `experiment_exception` before it's even
        # assigned.
        experiment_exception = exc
    finally:
        # ---- GENUINE unconditional finalization: a real `finally`
        # attached to the try/except above, so every phase below runs
        # whether or not the try block raised. Each of the nine phases
        # (cleanup, each evidence write, each inspection) is guarded in
        # its OWN try/except -- a failure in any single phase, INCLUDING
        # a write_evidence/elog call itself, is recorded as a sanitized
        # entry in `secondary_failures` and can never prevent a later
        # phase from running, and can never be confused with -- or
        # silently replace -- `experiment_exception` above.
        # `emergency_cleanup` always targets only the exact resources
        # recorded in `inventory`, never a generic name/label sweep, and
        # isolates exceptions per-entry internally (see its docstring).
        secondary_failures: list[str] = []
        emergency_results = {"results": [], "all_clean": False}
        fixture_wt_ok, fixture_wt_raw, fixture_worktree_entries = False, "", []
        fixture_repo_has_only_main_worktree = False
        scratch_root_removed_confirmed = False
        final_ps_ok = final_wt_ok = baseline_final_listings_ok = baseline_final_equal = False
        final_ps_raw = final_wt_raw = ""
        summary: dict | None = None

        # Phase 1: emergency cleanup.
        try:
            emergency_results = emergency_cleanup(inventory, str(source_repo))
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"emergency_cleanup raised: {type(exc).__name__}")

        # Phase 2: emergency-cleanup evidence write.
        try:
            write_evidence("emergency_cleanup_results.json", emergency_results)
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"writing emergency_cleanup_results.json raised: {type(exc).__name__}")

        # Phase 3: fixture-repository worktree inspection -- captured
        # BEFORE the scratch root (which contains the fixture repo) is
        # deleted. This is the check that actually demonstrates every
        # scenario/canary worktree was removed -- distinct from the
        # CodeAgent-checkout comparison in phase 6, which only proves
        # this run never touched THIS repository's own worktree
        # registration (a weaker, host-integrity fact that would hold
        # trivially even if fixture-repo cleanup were broken, since
        # nothing here ever targets the real checkout).
        try:
            fixture_wt_ok, fixture_wt_raw = raw_git_worktree_list(str(source_repo))
            fixture_worktree_entries = [line for line in fixture_wt_raw.splitlines() if line.startswith("worktree ")]
            fixture_repo_has_only_main_worktree = (
                fixture_wt_ok
                and len(fixture_worktree_entries) == 1
                and fixture_worktree_entries[0][len("worktree ") :] == str(source_repo.resolve())
            )
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"fixture worktree check raised: {type(exc).__name__}")

        # Phase 4: fixture evidence write.
        try:
            write_evidence(
                "fixture_worktree_check.json",
                {
                    "purpose": (
                        "Proves every scenario/canary worktree registered against "
                        "the temporary FIXTURE repository was actually removed -- "
                        "captured before the scratch root (which contains this "
                        "repo) is deleted. This is distinct from "
                        "baseline_final_comparison.json's CodeAgent-checkout "
                        "comparison, which only proves this run never registered/"
                        "left a worktree in the real project checkout -- a "
                        "host-integrity fact, not evidence that fixture-repo "
                        "worktrees were cleaned up."
                    ),
                    "fixture_repo_path": str(source_repo),
                    "listing_ok": fixture_wt_ok,
                    "listing_raw": fixture_wt_raw,
                    "worktree_entry_count": len(fixture_worktree_entries),
                    "has_only_main_worktree": fixture_repo_has_only_main_worktree,
                },
            )
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"writing fixture_worktree_check.json raised: {type(exc).__name__}")

        # Phase 5: scratch-root removal and absence check.
        # `ignore_errors=True` alone is never treated as confirmed
        # cleanup -- the scratch root's actual absence is independently
        # checked and recorded as its own fact.
        try:
            shutil.rmtree(session_scratch_root, ignore_errors=True)
            scratch_root_removed_confirmed = not session_scratch_root.exists()
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"scratch root removal raised: {type(exc).__name__}")

        # Phase 6: final Docker/host-checkout inspection -- exact
        # equality, not "looks empty". Proves this run never left this
        # CodeAgent repository itself with an extra registered worktree
        # -- NOT proof that the fixture repo's own worktrees were
        # cleaned up (see fixture_worktree_check.json for that).
        try:
            final_ps_ok, final_ps_raw = raw_docker_ps_a()
            final_wt_ok, final_wt_raw = raw_git_worktree_list(str(codeagent_repo_root))
            baseline_final_listings_ok = baseline_ps_ok and baseline_wt_ok and final_ps_ok and final_wt_ok
            baseline_final_equal = baseline_final_listings_ok and (baseline_ps_raw == final_ps_raw) and (
                baseline_wt_raw == final_wt_raw
            )
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"final host/Docker inspection raised: {type(exc).__name__}")

        # Phase 7: baseline/final evidence write.
        try:
            write_evidence(
                "baseline_final_comparison.json",
                {
                    "purpose": (
                        "Host-integrity check on the real CodeAgent checkout "
                        "(codeagent_repo_root) -- proves this run never "
                        "registered a worktree there or left a stray Docker "
                        "container globally visible. It does NOT prove the "
                        "fixture repository's own per-scenario worktrees were "
                        "removed; see fixture_worktree_check.json for that."
                    ),
                    "baseline_ps_ok": baseline_ps_ok,
                    "baseline_ps_raw": baseline_ps_raw,
                    "baseline_worktree_ok": baseline_wt_ok,
                    "baseline_worktree_raw": baseline_wt_raw,
                    "final_ps_ok": final_ps_ok,
                    "final_ps_raw": final_ps_raw,
                    "final_worktree_ok": final_wt_ok,
                    "final_worktree_raw": final_wt_raw,
                    "listings_ok": baseline_final_listings_ok,
                    "exactly_equal": baseline_final_equal,
                    "scratch_root_removed_confirmed": scratch_root_removed_confirmed,
                },
            )
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"writing baseline_final_comparison.json raised: {type(exc).__name__}")

        # Phase 8: summary construction/write. Deliberately built from
        # `secondary_failures` as accumulated through phase 7 only --
        # a failure in this phase or the next is still recorded in the
        # LIST used for the final raise-or-return decision below, even
        # though it cannot retroactively appear inside summary.json
        # itself once that file has already been written (or failed to
        # write).
        try:
            scenario_classifications = {k: v["classification"] for k, v in scenario_results.items()}
            if "classification" in scenario5:
                scenario_classifications["sigkill"] = scenario5["classification"]
            if "classification" in scenario6:
                scenario_classifications["reconciliation"] = scenario6["classification"]
            if "classification" in idempotency:
                scenario_classifications["idempotency"] = idempotency["classification"]

            if experiment_exception is not None:
                # A required-provenance collection/validation failure
                # (ProvenanceIncomplete) is a TECHNICAL_FAILURE, not a
                # behavioral FAIL -- it means the measurement/setup was
                # broken, not that a safety property was observed to
                # fail. Any other exception (e.g. a genuine setup
                # RuntimeError from canary creation) remains FAIL, as
                # before this correction.
                overall_verdict = "TECHNICAL_FAILURE" if isinstance(experiment_exception, ProvenanceIncomplete) else "FAIL"
            elif not (
                not secondary_failures
                and scenario_classifications
                and all(v == "PASS" for v in scenario_classifications.values())
                and emergency_results["all_clean"]
                and scratch_root_removed_confirmed
                and fixture_repo_has_only_main_worktree
                and baseline_final_equal
            ):
                overall_verdict = "FAIL"
            else:
                overall_verdict = "PASS"

            summary = {
                "host": host_info,
                "session_id": session_id,
                "scenario_classifications": scenario_classifications,
                "emergency_cleanup_all_clean": emergency_results["all_clean"],
                "scratch_root_removed_confirmed": scratch_root_removed_confirmed,
                "fixture_repo_has_only_main_worktree": fixture_repo_has_only_main_worktree,
                "baseline_final_listings_ok": baseline_final_listings_ok,
                "baseline_final_equal": baseline_final_equal,
                "experiment_exception": (
                    f"{type(experiment_exception).__name__}: {experiment_exception}"
                    if experiment_exception is not None
                    else None
                ),
                "technical_failure_reason": (
                    str(experiment_exception) if isinstance(experiment_exception, ProvenanceIncomplete) else None
                ),
                "secondary_cleanup_failures": list(secondary_failures),
                "overall_verdict": overall_verdict,
            }
            write_evidence("summary.json", summary)
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"summary construction/write raised: {type(exc).__name__}")

        # Phase 9: run-log write. The original-exception log message is
        # constructed and logged HERE, inside this already-guarded
        # phase, rather than in the except block above -- so a failure
        # in `elog` itself (or in the final `write_evidence` call) is
        # recorded as a secondary failure like any other phase-9
        # failure, and can never replace `experiment_exception`, which
        # was already assigned, untouched, before this phase ever runs.
        try:
            if experiment_exception is not None:
                elog(
                    f"EXPERIMENT ERROR (finalization proceeded): "
                    f"{type(experiment_exception).__name__}: {experiment_exception}"
                )
            if summary is not None:
                elog("SUMMARY: " + json.dumps(summary, indent=2, default=str))
            elog(f"Evidence written to: {evidence_dir}")
            write_evidence("run.log", "\n".join(log_lines))
        except BaseException as exc:  # noqa: BLE001
            secondary_failures.append(f"run.log write raised: {type(exc).__name__}")

    # ---- The finally block above has now fully completed. ----
    if experiment_exception is not None:
        # Preserve and re-raise the ORIGINAL failure, with its original
        # traceback (already attached to the exception object itself) --
        # finalization evidence has been attempted for every phase, but
        # an experiment exception is never converted into a successful
        # return, and a secondary cleanup/evidence failure never
        # REPLACES it (it was recorded separately, in
        # `secondary_cleanup_failures`, above).
        raise experiment_exception

    if secondary_failures:
        # No original experiment exception, but finalization itself
        # failed somewhere -- this must never be reported as PASS, and
        # the caller must see a real failure, not a quiet return, only
        # AFTER every finalization phase has already been attempted.
        raise RuntimeError(f"S5 unconditional cleanup/evidence encountered secondary failures: {secondary_failures}")

    return summary


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "--child":
        rest = argv[1:]
        scenario, session_id, run_token, scratch_root, source_repo = rest[:5]
        snapshot_dir = Path(rest[5]) if len(rest) > 5 else None
        sys.exit(child_main(scenario, session_id, run_token, Path(scratch_root), source_repo, snapshot_dir))

    if argv and argv[0] == "--reconcile":
        _, scratch_root, source_repo, session_id = argv
        result = reconcile(Path(scratch_root), source_repo, session_id)
        print(json.dumps(result, indent=2, default=str))
        sys.exit(1 if reconcile_run_has_problem(result) else 0)

    if argv and argv[0] == "--hold-lock":
        _, lock_path = argv
        fd = open_lock_file(Path(lock_path))
        if not try_lock_nonblocking(fd):
            sys.exit(9)
        Path(str(lock_path) + ".acquired").write_text("1")
        while True:
            time.sleep(0.05)

    if argv and argv[0] == "--normalize-docker-inventory":
        # Workflow-diagnostic helper, implemented in Python so it is
        # independently unit-testable: reads exact container IDs (one
        # per line, as produced by `docker ps -aq --no-trunc`) from
        # stdin, independently `docker inspect`s each one, and prints a
        # normalized, stably sorted inventory (stable identity fields
        # only) to stdout. Fails loud (exit 1) on any inspect failure,
        # malformed data, or identity mismatch rather than silently
        # dropping or misparsing an entry.
        container_ids = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
        try:
            normalized = normalize_container_listing(container_ids)
        except ValueError as exc:
            print(f"TECHNICAL_FAILURE: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(normalized, indent=2, sort_keys=True))
        sys.exit(0)

    if argv and argv[0] == "--validate-actions-context":
        # A single, tested Python implementation of Actions-context
        # validation, invoked by the workflow's own shell preflight so
        # the well-formedness rule (URL scheme/host/credentials,
        # repository owner/name shape) is never independently
        # reimplemented -- and potentially drifted -- in bash. A
        # failure here happens before any harness evidence directory
        # exists, so it is reported only as a nonzero exit and a
        # stderr message -- never as a harness summary.json/
        # overall_verdict.
        try:
            ctx = validate_actions_context(os.environ)
        except ProvenanceIncomplete as exc:
            print(f"TECHNICAL_FAILURE: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(ctx, indent=2, sort_keys=True))
        sys.exit(0)

    run_full_experiment()


if __name__ == "__main__":
    main()
