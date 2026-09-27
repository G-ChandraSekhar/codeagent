"""Milestone 3 Slice 3B-6: real, lifecycle-aware Docker verification
(ADR 0004 Amendment 6).

Real `prepare_lifecycle()` lease + real `_LifecycleProjectionWriter` +
real `LifecycleContainerPublisher` + real Docker, end to end — no
mocking of the writer or the Docker CLI anywhere in this file. Requires
a working local Docker daemon; skipped otherwise (see
`tests/integration/test_slice_c.py` for the identical convention this
file reuses, including the CI-only `CODEAGENT_REQUIRE_DOCKER=1` hard
failure instead of a silent skip).
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import signal
import shutil
import subprocess
import threading
import time

import pytest

from codeagent import _docker_ownership as docker_ownership
from codeagent import container_lifecycle as cl
from codeagent import events
from codeagent import executor as ex
from codeagent import lifecycle_store as ls
from codeagent import reconciliation as rc
from codeagent import repo_identity as ri
from codeagent import state_locks as sl
from codeagent import state_root as sr
from codeagent._lifecycle_fs import resolve_state_root_path


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_required() -> bool:
    return os.environ.get("CODEAGENT_REQUIRE_DOCKER") == "1"


if not _docker_available() and _docker_required():
    pytest.fail(
        "CODEAGENT_REQUIRE_DOCKER=1 but no Docker daemon is available — "
        "this environment is expected to guarantee Docker; failing instead of skipping.",
        pytrace=False,
    )

# Correction pass: this used to be a module-wide `pytestmark`, which
# also skipped this file's own mock-only cleanup-helper regression
# tests (they make no Docker call at all -- they exist specifically to
# test failure injection) whenever Docker happened to be unavailable
# locally. Applied per-test instead, to exactly the tests that
# genuinely perform a real Docker operation (directly, or transitively
# through a real `reconcile_repository()` pass, which always issues a
# real `docker ps -a` for any eligible entry it processes).
requires_docker = pytest.mark.skipif(not _docker_available(), reason="requires a running local Docker daemon")


def _run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _run("git", "init", "-q", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "a@b.com")
    _run("git", "-C", str(repo), "config", "user.name", "a")
    (repo / "f.txt").write_text("x")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-q", "-m", "init")
    return repo


def _set_state_dir(monkeypatch, tmp_path, name="state-root"):
    state_dir = tmp_path / name
    monkeypatch.setenv("CODEAGENT_STATE_DIR", str(state_dir))
    return state_dir


def _require_valid_container_id(container_id: str) -> str:
    """Refuses to treat anything but an exact 64-lowercase-hex string as
    a container id (correction pass) — a malformed or empty value must
    never reach `docker rm`."""
    if not docker_ownership.CONTAINER_ID_HEX_RE.fullmatch(container_id):
        raise AssertionError(
            f"refusing to treat {container_id!r} as a container id: "
            "not exactly 64 lowercase hexadecimal characters"
        )
    return container_id


def _container_exists(name_or_id: str) -> bool:
    """Positive-evidence-only presence check (correction pass): derives
    presence/absence exclusively from a fresh, strict, unfiltered
    listing via the shared `_docker_ownership.
    docker_ps_all_id_name_pairs()` — never from `docker inspect`'s own
    ambiguous nonzero exit, which cannot distinguish "genuinely absent"
    from "the daemon is unavailable" or any other inspection failure.
    A listing that cannot be obtained at all (`DockerListingError`:
    launch failure, timeout, nonzero exit, or malformed output)
    propagates unchanged — it is never silently treated as absence."""
    name_to_id, id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
    if docker_ownership.CONTAINER_ID_HEX_RE.fullmatch(name_or_id):
        return name_or_id in id_to_name
    return name_or_id in name_to_id


def _force_remove_container(container_id: str) -> None:
    """Load-bearing cleanup (correction pass): validates `container_id`
    is a real immutable id, removes it (its own exit code is never
    authoritative for removal), then performs a fresh strict listing
    and confirms that exact id is genuinely absent — never a silently
    ignored `docker rm` exit code, and never `docker inspect`'s own
    ambiguous nonzero exit. Raises if absence cannot be confirmed."""
    _require_valid_container_id(container_id)
    subprocess.run(["docker", "rm", "--force", container_id], capture_output=True, text=True)
    _name_to_id, id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
    if container_id in id_to_name:
        raise AssertionError(
            f"test cleanup could not confirm container {container_id} removed "
            f"(still present as {id_to_name[container_id]!r})"
        )


def _cleanup_deterministic_name_if_present(name: str) -> None:
    """Safety-net cleanup for a test's own deterministic container name,
    used from `finally` blocks so a failed mid-test assertion never
    leaves a real container behind. The candidate id comes from the
    same strict name->id listing this file uses everywhere else
    (correction pass) — never from `docker inspect <name>`'s own
    ambiguous nonzero exit — and removal is still by that validated
    immutable id only, never by name."""
    name_to_id, _id_to_name = docker_ownership.docker_ps_all_id_name_pairs()
    candidate_id = name_to_id.get(name)
    if candidate_id is not None:
        _force_remove_container(candidate_id)


# ---------------------------------------------------------------------------
# Test-infrastructure regressions for the fail-closed cleanup helpers
# above (correction pass) — these are ordinary mock-based checks (no
# real Docker call is actually made by any of them), kept in this file
# because they exercise this file's own real-Docker-test cleanup
# helpers directly.
# ---------------------------------------------------------------------------


def test_container_exists_does_not_treat_listing_failure_as_absence(monkeypatch):
    def _boom(**kwargs):
        raise docker_ownership.DockerListingError("docker unavailable")

    monkeypatch.setattr(docker_ownership, "docker_ps_all_id_name_pairs", _boom)
    with pytest.raises(docker_ownership.DockerListingError):
        _container_exists("a" * 64)


def test_force_remove_container_rejects_malformed_id_before_any_rm(monkeypatch):
    rm_calls: list = []

    def _fake_run(argv, *a, **k):
        rm_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(AssertionError):
        _force_remove_container("not-a-valid-id")
    assert rm_calls == []


def test_force_remove_container_fails_when_still_present(monkeypatch):
    container_id = "a" * 64
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(
        docker_ownership,
        "docker_ps_all_id_name_pairs",
        lambda **k: ({"some-name": container_id}, {container_id: "some-name"}),
    )
    with pytest.raises(AssertionError):
        _force_remove_container(container_id)


def test_force_remove_container_succeeds_on_fresh_listing_confirming_absence(monkeypatch):
    container_id = "b" * 64
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(docker_ownership, "docker_ps_all_id_name_pairs", lambda **k: ({}, {}))
    _force_remove_container(container_id)  # must not raise


def test_cleanup_deterministic_name_if_present_removes_by_listed_id_only(monkeypatch):
    name = "codeagent-baseline-" + "c" * 32
    container_id = "d" * 64
    rm_calls: list = []

    def _fake_run(argv, *a, **k):
        if argv[:2] == ["docker", "rm"]:
            rm_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    listing_calls = {"n": 0}

    def _fake_listing(**k):
        listing_calls["n"] += 1
        if listing_calls["n"] == 1:
            return ({name: container_id}, {container_id: name})
        return ({}, {})  # confirms absence after removal

    monkeypatch.setattr(docker_ownership, "docker_ps_all_id_name_pairs", _fake_listing)
    _cleanup_deterministic_name_if_present(name)
    # Removal is always by the validated immutable id -- never by name.
    assert rm_calls == [["docker", "rm", "--force", container_id]]


def _reconcile_fresh(repo_path, state_dir) -> rc.ReconciliationPassResult:
    """Independently re-derives the trusted substrate and runs exactly
    one `reconcile_repository()` pass. Releases the repository lock
    before closing the state root, attempting both regardless of an
    earlier failure and chaining a cleanup failure from whatever
    failure was already active — matching `lifecycle_store.
    LifecycleLease.close()`'s own established convention (correction
    pass: neither resource was previously released at all)."""
    identity, context = ri.discover_repository_identity_and_context(repo_path)
    location = resolve_state_root_path()
    root_fd, canonical_root_path = sr.open_or_create_canonical_root(location)
    sr.validate_state_root_containment(canonical_root_path, context)
    state_root = sr.init_state_root(root_fd, canonical_root_path)
    repository_lock = sl.acquire_repository_lock(state_root, identity.repo_key)
    try:
        identity = ri.load_or_create_repo_json(state_root, identity, repository_lock)
        return rc.reconcile_repository(
            state_root=state_root, identity=identity, context=context, repository_lock=repository_lock
        )
    finally:
        _close_reconcile_resources(repository_lock, state_root)


def _close_reconcile_resources(repository_lock, state_root) -> None:
    lock_exc: Exception | None = None
    try:
        repository_lock.release()
    except Exception as exc:  # noqa: BLE001 - state root close is still attempted below
        lock_exc = exc
    try:
        state_root.close()
    except Exception as exc:
        if lock_exc is not None:
            raise exc from lock_exc
        raise
    if lock_exc is not None:
        raise lock_exc


@requires_docker
def test_reconcile_fresh_releases_the_repository_lock(tmp_path, monkeypatch):
    """Load-bearing regression (correction pass) for `_reconcile_fresh`'s
    own cleanup: if the repository lock leaked, a fresh acquisition
    immediately afterward would raise `LockError(BUSY)`."""
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    seed_lease = ls.prepare_lifecycle(str(repo), run_id="seed")
    seed_lease.close()

    _reconcile_fresh(str(repo), str(state_dir))

    identity, context = ri.discover_repository_identity_and_context(str(repo))
    location = resolve_state_root_path()
    root_fd, canonical_root_path = sr.open_or_create_canonical_root(location)
    sr.validate_state_root_containment(canonical_root_path, context)
    state_root = sr.init_state_root(root_fd, canonical_root_path)
    try:
        repository_lock = sl.acquire_repository_lock(state_root, identity.repo_key)
        repository_lock.release()
    finally:
        state_root.close()


# ---------------------------------------------------------------------------
# Real end-to-end: names, labels, sequential verification-role reuse
# ---------------------------------------------------------------------------


@requires_docker
def test_real_lifecycle_aware_baseline_and_two_verification_attempts_reuse_role(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="3b6-e2e")
    baseline_name = None
    verification_name = None
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)
        ctx = ex.DockerVerifierLifecycleContext(publisher=publisher)
        # Mounts the real repository under verification, not tmp_path's
        # parent-of-state-root directory (correction pass); "true" is a
        # deterministic, always-successful command available in the
        # pinned image, so a genuine PASSED outcome is actually proven,
        # not merely a cleanup-status side effect of some other failure.
        verifier = ex.DockerVerifier(repo, command=("true",), lifecycle_context=ctx)
        baseline_name = cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=lease.lifecycle_id)
        verification_name = cl.deterministic_container_name(
            role=cl.ContainerRole.VERIFICATION, lifecycle_id=lease.lifecycle_id
        )

        baseline_result = verifier.run_baseline()
        assert baseline_result.outcome is events.VerificationOutcome.PASSED
        assert baseline_result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
        assert publisher.current.baseline.intent is cl.ContainerIntent.ABSENT

        attempt1 = verifier.run(1)
        assert attempt1.outcome is events.VerificationOutcome.PASSED
        assert attempt1.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
        assert publisher.current.verification.intent is cl.ContainerIntent.ABSENT

        attempt2 = verifier.run(2)
        assert attempt2.outcome is events.VerificationOutcome.PASSED
        assert attempt2.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
        assert publisher.current.verification.intent is cl.ContainerIntent.ABSENT

        # Both sequential verification attempts targeted the identical
        # deterministic name -- one reusable role, never a fresh
        # per-attempt name -- and the final durable projection is
        # absent for both roles.
        assert not _container_exists(verification_name)
        assert publisher.current.baseline.intent is cl.ContainerIntent.ABSENT
        assert publisher.current.verification.intent is cl.ContainerIntent.ABSENT
        baseline_name = None
        verification_name = None
    finally:
        if baseline_name is not None:
            _cleanup_deterministic_name_if_present(baseline_name)
        if verification_name is not None:
            _cleanup_deterministic_name_if_present(verification_name)
        lease.close()


@requires_docker
def test_real_docker_create_sets_exact_deterministic_name_and_four_labels(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="3b6-labels")
    expected_name = None
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)
        ctx = ex.DockerVerifierLifecycleContext(publisher=publisher)
        # A slow command gives this test a real window to inspect the
        # container while it is still alive, by its deterministic name.
        verifier = ex.DockerVerifier(repo, command=("sleep", "3"), lifecycle_context=ctx)
        expected_name = cl.deterministic_container_name(
            role=cl.ContainerRole.BASELINE, lifecycle_id=lease.lifecycle_id
        )

        result_holder: dict = {}

        def _run_verifier():
            result_holder["result"] = verifier.run_baseline()

        thread = threading.Thread(target=_run_verifier)
        thread.start()

        deadline = time.monotonic() + 10
        labels_json = None
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", expected_name],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                labels_json = probe.stdout
                break
            time.sleep(0.1)
        thread.join(timeout=15)
        # The label-inspection worker must have genuinely finished
        # before this test proceeds to close the lease out from under
        # it (correction pass) -- a still-running thread here would
        # mean the assertions above raced a container the thread itself
        # might still be trying to clean up.
        assert not thread.is_alive(), "label-inspection worker thread did not finish within the timeout"

        assert labels_json is not None, "container never appeared at its expected deterministic name in time"
        labels = json.loads(labels_json)
        assert labels[cl.CONTAINER_LABEL_SCHEMA] == "1"
        assert labels[cl.CONTAINER_LABEL_STATE_ROOT_ID] == lease.state_root.state_root_id
        assert labels[cl.CONTAINER_LABEL_ID] == lease.lifecycle_id
        assert labels[cl.CONTAINER_LABEL_ROLE] == "baseline"
        assert "codeagent.lifecycle.attempt" not in labels

        result = result_holder["result"]
        assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
        expected_name = None
    finally:
        if expected_name is not None:
            _cleanup_deterministic_name_if_present(expected_name)
        lease.close()


@requires_docker
def test_real_occupied_name_recovery_accepts_extra_image_provided_label(tmp_path, monkeypatch):
    """Replaces the earlier draft's unproven "may contain labels"
    version (correction pass): pre-creates a real, correctly owned
    container carrying the four required labels *plus* one genuine
    unrelated label, proves that extra label is actually present, then
    runs the lifecycle-aware verifier and proves occupied-name recovery
    accepts ownership despite it, removes the pre-existing container by
    its immutable ID, and completes a new run safely."""
    repo = _make_repo(tmp_path)
    _set_state_dir(monkeypatch, tmp_path)
    lease = ls.prepare_lifecycle(str(repo), run_id="3b6-extra-label-recovery")
    pre_created_id = None
    baseline_name = None
    try:
        writer, current = lease.open_projection_writer()
        publisher = ls.LifecycleContainerPublisher(writer, current)
        baseline_name = cl.deterministic_container_name(
            role=cl.ContainerRole.BASELINE, lifecycle_id=lease.lifecycle_id
        )

        required = cl.required_labels(
            state_root_id=lease.state_root.state_root_id,
            lifecycle_id=lease.lifecycle_id,
            role=cl.ContainerRole.BASELINE,
        )
        extra_label_key = "com.example.unrelated"
        extra_label_value = "genuinely-present"

        # Durable write-ahead first, matching what a real DockerVerifier
        # would already have published before any container with this
        # name existed -- required for the subsequent recovery edges
        # (CREATING->PRESENT->REMOVING->ABSENT) to be legal.
        publisher.publish(role=cl.ContainerRole.BASELINE, intent=cl.ContainerIntent.CREATING, id=None)

        create_argv = ["docker", "create", "--name", baseline_name]
        for key, value in {**required, extra_label_key: extra_label_value}.items():
            create_argv += ["--label", f"{key}={value}"]
        create_argv += [ex.DEFAULT_IMAGE, "sleep", "0"]
        create_result = subprocess.run(create_argv, capture_output=True, text=True, check=True)
        pre_created_id = create_result.stdout.strip()

        # Prove the extra label is genuinely present before recovery —
        # not merely asserted about the image's own defaults.
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", pre_created_id],
            capture_output=True,
            text=True,
            check=True,
        )
        pre_labels = json.loads(inspect.stdout)
        assert pre_labels.get(extra_label_key) == extra_label_value
        assert pre_labels[cl.CONTAINER_LABEL_SCHEMA] == "1"

        ctx = ex.DockerVerifierLifecycleContext(publisher=publisher)
        verifier = ex.DockerVerifier(repo, command=("true",), lifecycle_context=ctx)
        result = verifier.run_baseline()

        assert result.outcome is events.VerificationOutcome.PASSED
        assert result.cleanup_status is events.ContainerCleanupStatus.CONFIRMED_ABSENT
        # The pre-existing, extra-labeled container was genuinely
        # removed by its immutable ID during occupied-name recovery.
        assert not _container_exists(pre_created_id)
        pre_created_id = None
        baseline_name = None
    finally:
        if pre_created_id is not None:
            _force_remove_container(pre_created_id)
        if baseline_name is not None:
            _cleanup_deterministic_name_if_present(baseline_name)
        lease.close()


# ---------------------------------------------------------------------------
# Real SIGKILL boundaries, each followed by a fresh reconcile_repository()
# pass proving the correct residual-state resolution (ADR 0004 Amendment 6
# section 5 / the final correction plan's corrected §7 scenarios).
# ---------------------------------------------------------------------------


def _run_lifecycle_aware_baseline_and_sigkill(
    repo_path: str, state_dir: str, worktree_path: str, id_file_path: str, stop_intent: str
) -> None:
    """Module-level (picklable) child-process target: prepares its own
    real lifecycle lease, drives a real `DockerVerifier.run_baseline()`
    against a real Docker daemon, and self-SIGKILLs the instant the
    wrapped publisher confirms the durable write for `stop_intent`
    ("creating"/"present"/"removing") — simulating a real crash at that
    exact boundary. Writes its own minted `lifecycle_id` to
    `id_file_path` (flushed and fsynced) immediately after
    `prepare_lifecycle()` succeeds, since a SIGKILL child cannot return
    a value the ordinary way."""
    os.environ["CODEAGENT_STATE_DIR"] = state_dir
    import codeagent.executor as ex_child
    import codeagent.lifecycle_store as ls_child

    lease = ls_child.prepare_lifecycle(repo_path, run_id=f"sigkill-{stop_intent}")
    with open(id_file_path, "w") as f:
        f.write(lease.lifecycle_id)
        f.flush()
        os.fsync(f.fileno())

    writer, current = lease.open_projection_writer()
    publisher = ls_child.LifecycleContainerPublisher(writer, current)

    real_publish = publisher.publish

    def _hook(*, role, intent, id):
        real_publish(role=role, intent=intent, id=id)
        if intent.value == stop_intent:
            os.kill(os.getpid(), signal.SIGKILL)

    publisher.publish = _hook

    ctx = ex_child.DockerVerifierLifecycleContext(publisher=publisher)
    verifier = ex_child.DockerVerifier(worktree_path, lifecycle_context=ctx)
    verifier.run_baseline()
    # Unreachable for every stop_intent this file uses -- every one of
    # "creating"/"present"/"removing" is published during a normal run.
    os.kill(os.getpid(), signal.SIGKILL)


def _spawn_and_await_sigkill(target, args) -> None:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=target, args=args)
    proc.start()
    proc.join(timeout=30)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=10)
        pytest.fail("child process did not self-SIGKILL within the timeout; killed and cleaned up")
    assert proc.exitcode == -signal.SIGKILL


@requires_docker
def test_real_sigkill_after_creating_leaves_no_container_and_reconciles(tmp_path, monkeypatch):
    """Scenario 1: SIGKILL immediately after the durable `CREATING`
    publish, strictly before this invocation's own `docker create` is
    ever issued — no container exists anywhere for the reconciler to
    find; its own `CREATING` branch collapses directly to absent."""
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    id_file = tmp_path / "lifecycle_id.txt"

    _spawn_and_await_sigkill(
        _run_lifecycle_aware_baseline_and_sigkill,
        (str(repo), str(state_dir), str(repo), str(id_file), "creating"),
    )
    lifecycle_id = id_file.read_text()
    baseline_name = cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=lifecycle_id)
    assert not _container_exists(baseline_name)

    result = _reconcile_fresh(str(repo), str(state_dir))
    entry = next(e for e in result.entries if e.lifecycle_id == lifecycle_id)
    assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED


@requires_docker
def test_real_sigkill_after_present_recovers_created_not_started_container(tmp_path, monkeypatch):
    """Scenario 2: SIGKILL immediately after the durable `PRESENT(id)`
    publish, strictly before `docker start --attach` is ever issued —
    a real container exists, created but never started. The
    reconciler's `PRESENT`/`REMOVING` branch proves ownership and
    removes it."""
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    id_file = tmp_path / "lifecycle_id.txt"

    _spawn_and_await_sigkill(
        _run_lifecycle_aware_baseline_and_sigkill,
        (str(repo), str(state_dir), str(repo), str(id_file), "present"),
    )
    lifecycle_id = id_file.read_text()
    baseline_name = cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=lifecycle_id)
    container_id = None
    try:
        assert _container_exists(baseline_name)
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}\t{{.State.Status}}", baseline_name],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id, status = inspect.stdout.strip().split("\t")
        assert status != "running"  # created, never started

        result = _reconcile_fresh(str(repo), str(state_dir))
        entry = next(e for e in result.entries if e.lifecycle_id == lifecycle_id)
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert not _container_exists(baseline_name)
        container_id = None  # confirmed removed by reconciliation
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


@requires_docker
def test_real_sigkill_after_removing_recovers_started_and_finished_container(tmp_path, monkeypatch):
    """Scenario 3: SIGKILL immediately after the durable `REMOVING(id)`
    publish, strictly before `docker rm` is ever issued — a real
    container exists that was started and ran to completion. The
    reconciler's same-id-continuity `OWNED_REMOVE` path resumes
    removal."""
    repo = _make_repo(tmp_path)
    state_dir = _set_state_dir(monkeypatch, tmp_path)
    id_file = tmp_path / "lifecycle_id.txt"

    _spawn_and_await_sigkill(
        _run_lifecycle_aware_baseline_and_sigkill,
        (str(repo), str(state_dir), str(repo), str(id_file), "removing"),
    )
    lifecycle_id = id_file.read_text()
    baseline_name = cl.deterministic_container_name(role=cl.ContainerRole.BASELINE, lifecycle_id=lifecycle_id)
    container_id = None
    try:
        assert _container_exists(baseline_name)
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}\t{{.State.Status}}", baseline_name],
            capture_output=True,
            text=True,
            check=True,
        )
        container_id, status = inspect.stdout.strip().split("\t")
        assert status == "exited"  # started and ran to completion

        result = _reconcile_fresh(str(repo), str(state_dir))
        entry = next(e for e in result.entries if e.lifecycle_id == lifecycle_id)
        assert entry.outcome is rc.ReconciliationEntryOutcome.RECONCILED
        assert not _container_exists(baseline_name)
        container_id = None  # confirmed removed by reconciliation
    finally:
        if container_id is not None:
            _force_remove_container(container_id)


# ---------------------------------------------------------------------------
# Structural proof (correction pass): exactly the genuine real-Docker
# tests carry `@requires_docker` -- never the mock-only cleanup-helper
# regressions above, and never a stray extra test gaining a Docker
# dependency without anyone noticing.
# ---------------------------------------------------------------------------


def test_exactly_the_real_docker_tests_carry_the_requires_docker_marker():
    import sys

    this_module = sys.modules[__name__]
    expected_marked = {
        "test_reconcile_fresh_releases_the_repository_lock",
        "test_real_lifecycle_aware_baseline_and_two_verification_attempts_reuse_role",
        "test_real_docker_create_sets_exact_deterministic_name_and_four_labels",
        "test_real_occupied_name_recovery_accepts_extra_image_provided_label",
        "test_real_sigkill_after_creating_leaves_no_container_and_reconciles",
        "test_real_sigkill_after_present_recovers_created_not_started_container",
        "test_real_sigkill_after_removing_recovers_started_and_finished_container",
    }

    def _carries_requires_docker(func) -> bool:
        return any(
            mark.name == "skipif" and mark.kwargs.get("reason") == "requires a running local Docker daemon"
            for mark in getattr(func, "pytestmark", [])
        )

    actual_marked = {
        name
        for name in dir(this_module)
        if name.startswith("test_") and _carries_requires_docker(getattr(this_module, name))
    }
    assert actual_marked == expected_marked
    # And every other test function in this module -- the five mock-only
    # cleanup-helper regressions plus this test itself -- carries no
    # Docker dependency at all.
    all_test_names = {name for name in dir(this_module) if name.startswith("test_")}
    unmarked = all_test_names - actual_marked
    assert unmarked == {
        "test_container_exists_does_not_treat_listing_failure_as_absence",
        "test_force_remove_container_rejects_malformed_id_before_any_rm",
        "test_force_remove_container_fails_when_still_present",
        "test_force_remove_container_succeeds_on_fresh_listing_confirming_absence",
        "test_cleanup_deterministic_name_if_present_removes_by_listed_id_only",
        "test_exactly_the_real_docker_tests_carry_the_requires_docker_marker",
    }
