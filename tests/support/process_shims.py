"""ADR 0005 Amendment 1 (ledger D3) per-site real-process harness (D3 plan
sections 10.1, 10.2 and 10.4). Test-only; never imported by production.

A Python shim named `git` and `docker` is placed first on `PATH`. Every
production launch site (L1-L7) runs `git` or `docker` by name, and the
sanitized Git environment keeps `PATH` and `CODEAGENT_D3_SHIM_DIR`. The
shim records its own pid/pgid/sid **from inside the launched process while
it is alive**, then, per its configured mode:

- `exec`: `os.execv` the real binary (exec keeps pid, pgid and sid);
- `exit`: exit 0 with no output;
- `gate`: write `ready-<pid>`, record any SIGINT/SIGTERM it receives, and
  block until the `release` file exists (a deadline exits 3);
- `grandchild`: start a marker grandchild in its own process group,
  record both pids, then block until killed.

No post-exit inspection is ever needed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SHIM_ENV = "CODEAGENT_D3_SHIM_DIR"

_SHIM_SOURCE = r'''
import json, os, signal, subprocess, sys, time
d = os.environ["CODEAGENT_D3_SHIM_DIR"]
name = os.path.basename(sys.argv[0])
cfg = json.load(open(os.path.join(d, "config.json")))
mode = cfg["modes"].get(name, "exec")
rec = {"name": name, "argv": sys.argv[1:], "pid": os.getpid(), "pgid": os.getpgid(0),
       "sid": os.getsid(0), "mode": mode, "recorded_alive_at": time.time()}
rec["session_leader"] = rec["pid"] == rec["pgid"] == rec["sid"]
if mode == "grandchild":
    marker = "codeagent-d3-gc-" + cfg["marker"]
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", marker])
    rec["grandchild_pid"], rec["marker"] = gc.pid, marker
tmp = os.path.join(d, "records", ".%d.tmp" % os.getpid())
with open(tmp, "w") as f:
    json.dump(rec, f)
os.rename(tmp, os.path.join(d, "records", "%d.json" % os.getpid()))
if mode == "exec":
    real = cfg["real"][name]
    os.execv(real, [real] + sys.argv[1:])
if mode == "exit":
    sys.exit(0)
if mode == "gate":
    def got(signum, frame):
        with open(os.path.join(d, "records", "%d.signals" % os.getpid()), "a") as f:
            f.write("%d\n" % signum)
    signal.signal(signal.SIGINT, got)
    signal.signal(signal.SIGTERM, got)
    open(os.path.join(d, "ready-%d" % os.getpid()), "w").close()
    deadline = time.monotonic() + 60
    while not os.path.exists(os.path.join(d, "release")):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.01)
    sys.exit(0)
while True:  # grandchild mode: block until killed
    time.sleep(1)
'''


class ShimDir:
    def __init__(self, root: Path, *, modes: dict[str, str]) -> None:
        self.root = root
        self.bin = root / "bin"
        self.records = root / "records"
        self.bin.mkdir(parents=True)
        self.records.mkdir()
        real = {name: shutil.which(name) or "/nonexistent/" + name for name in ("git", "docker")}
        self.marker = uuid.uuid4().hex
        self.configure(modes, real)
        for name in ("git", "docker"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable} -I\n{_SHIM_SOURCE}")
            path.chmod(0o755)

    def configure(self, modes: dict[str, str], real: dict[str, str] | None = None) -> None:
        cfg_path = self.root / "config.json"
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        cfg["modes"] = modes
        if real is not None:
            cfg["real"] = real
        cfg["marker"] = self.marker
        cfg_path.write_text(json.dumps(cfg))

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env[SHIM_ENV] = str(self.root)
        return env

    def install(self, monkeypatch) -> None:
        for key, value in self.env().items():
            if os.environ.get(key) != value:
                monkeypatch.setenv(key, value)

    def records_list(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.records.glob("*.json"))]

    def signals_for(self, pid: int) -> list[int]:
        path = self.records / f"{pid}.signals"
        return [int(x) for x in path.read_text().split()] if path.exists() else []

    def wait_ready(self, deadline_seconds: float = 30.0) -> int:
        deadline = time.monotonic() + deadline_seconds
        while True:
            ready = list(self.root.glob("ready-*"))
            if ready:
                return int(ready[0].name.split("-", 1)[1])
            if time.monotonic() > deadline:
                raise AssertionError("the gated shim never became ready")
            time.sleep(0.01)

    def release(self) -> None:
        (self.root / "release").write_text("")


def _ps(fields: str) -> list[list[str]]:
    out = subprocess.run(["ps", "-A", "-o", fields], capture_output=True, text=True, check=True).stdout
    return [line.split(None, len(fields.split(",")) - 1) for line in out.splitlines() if line.strip()]


def pgid_absent(pgid: int, *, deadline_seconds: float = 15.0) -> None:
    """Bounded polling (D3 plan section 10.2): absence means no process,
    zombies included, with this PGID. PID reuse can only cause a false
    failure, never a false pass."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        present = [row for row in _ps("pid=,pgid=,stat=") if len(row) >= 2 and row[1] == str(pgid)]
        if not present:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"process group {pgid} still has members: {present}")
        time.sleep(0.05)


def marker_absent(marker: str, *, deadline_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while True:
        present = [row for row in _ps("pid=,command=") if len(row) == 2 and marker in row[1]]
        if not present:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"marker grandchild still running: {present}")
        time.sleep(0.05)


# --- the seven sites -------------------------------------------------------

SITES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")
ABORT_SITES = ("L1", "L2", "L4", "L5", "L7")


def prepare_repo(workdir: Path) -> None:
    """For L6/L7. Call before the shim is installed (uses the real git)."""
    subprocess.run(["git", "init", "-q", str(workdir / "repo")], check=True)


def call_site(site: str, workdir: Path, *, abort: bool = False):
    """Invoke one production launch site the way production does. With
    `abort`, use a short timeout so the site's abort path runs."""
    from codeagent import _bounded_subprocess, _git_safety, checkpoint_ref, executor

    if site == "L1":
        try:
            return _bounded_subprocess.run_bounded_stdout(
                ["docker", "version"], timeout_seconds=1.0 if abort else 30.0, stdout_limit=4096
            )
        except _bounded_subprocess.BoundedProcessError as exc:
            return exc
    if site == "L2":
        verifier = executor.DockerVerifier(workdir, timeout_seconds=1.0 if abort else 30.0)
        original = executor._run_docker
        executor._run_docker = lambda *a, **k: _bounded_subprocess.BoundedProcessResult(returncode=1, stdout=b"")
        try:
            return verifier._start_and_inspect("0" * 64)
        finally:
            executor._run_docker = original
    if site == "L3":
        return _git_safety._run(["--version"])
    if site in ("L4", "L5"):
        fn = _git_safety._read_bounded if site == "L4" else _git_safety._read_bounded_preview
        try:
            return fn(["--version"], env=_git_safety.git_environment(), timeout=1.0 if abort else 30.0, limit=4096)
        except _git_safety.GitSafetyError as exc:
            return exc
    repo = workdir / "repo"
    if not (repo / ".git").exists():
        raise AssertionError("call_site L6/L7 needs prepare_repo() first")
    if site == "L6":
        return checkpoint_ref._run_git(repo, "--version")
    if site == "L7":
        original = checkpoint_ref.GIT_TIMEOUT_SECONDS
        if abort:
            checkpoint_ref.GIT_TIMEOUT_SECONDS = 1.0
        try:
            transaction = checkpoint_ref._RefTransaction(repo)
            transaction.__enter__()
            try:
                transaction.begin("update refs/codeagent/d3-probe " + "1" * 40)
            except checkpoint_ref.CheckpointRefError:
                pass
            try:
                transaction.__exit__(None, None, None)
            except checkpoint_ref.CheckpointRefError as exc:
                return exc
        finally:
            checkpoint_ref.GIT_TIMEOUT_SECONDS = original
        return None
    raise ValueError(site)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SITE_BINARY = {"L1": "docker", "L2": "docker", "L3": "git", "L4": "git", "L5": "git", "L6": "git", "L7": "git"}


def _new_shim(site: str, workdir: Path, mode: str) -> ShimDir:
    if site in ("L6", "L7"):
        prepare_repo(workdir)
    other = "exit"
    modes = {"git": other, "docker": other}
    modes[_SITE_BINARY[site]] = mode
    return ShimDir(workdir / "shim", modes=modes)


def check_session(site: str, workdir: Path, monkeypatch) -> None:
    """All seven sites: the launched process records, while alive, that it
    leads its own session, distinct from the caller's (M2, M3, M26f, M36)."""
    mode = "exec" if _SITE_BINARY[site] == "git" else "exit"
    shim = _new_shim(site, workdir, mode)
    shim.install(monkeypatch)
    call_site(site, workdir)
    records = [r for r in shim.records_list() if r["name"] == _SITE_BINARY[site]]
    assert records, f"{site}: the shim recorded nothing (identity must be recorded while alive)"
    for record in records:
        assert record["pid"] == record["pgid"] == record["sid"], (site, record)
        assert record["session_leader"]
        assert record["sid"] != os.getsid(0), (site, record)


def check_group_sigint_spared(site: str, workdir: Path) -> None:
    """All seven sites: a SIGINT to the caller's process group, sent while
    the launched child is gated, never reaches the child."""
    shim = _new_shim(site, workdir, "gate")
    helper = spawn_helper(site, shim, workdir, PROJECT_ROOT)
    try:
        child_pid = shim.wait_ready()
        os.killpg(helper.pid, signal.SIGINT)
        # Release only once the helper itself has acknowledged the signal.
        receipt = workdir / "helper-sigint"
        deadline = time.monotonic() + 15
        while not receipt.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the helper never received the group SIGINT")
            time.sleep(0.01)
        shim.release()
        out, err = helper.communicate(timeout=60)
    finally:
        shim.release()
        if helper.poll() is None:
            os.killpg(helper.pid, signal.SIGKILL)
            helper.wait()
    assert helper.returncode == 0, err
    result = json.loads(out.strip().splitlines()[-1])
    assert result["received"] == [signal.SIGINT], (site, result)
    assert shim.signals_for(child_pid) == [], site
    (record,) = [r for r in shim.records_list() if r["pid"] == child_pid]
    assert record["sid"] == child_pid != helper.pid


def check_abort_group_kill(site: str, workdir: Path, monkeypatch) -> None:
    """L1, L2, L4, L5, L7: an abort (a short timeout) kills the child's
    whole process group, so a marker grandchild is gone too (M4)."""
    shim = _new_shim(site, workdir, "grandchild")
    shim.install(monkeypatch)
    try:
        call_site(site, workdir, abort=True)
        (record,) = [r for r in shim.records_list() if r["name"] == _SITE_BINARY[site]]
        assert record["pgid"] == record["pid"]
        pgid_absent(record["pgid"])
        marker_absent(record["marker"])
    finally:
        for record in shim.records_list():
            for pid in (record.get("grandchild_pid"), record["pid"]):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


def spawn_helper(site: str, shim: ShimDir, workdir: Path, project_root: Path) -> subprocess.Popen:
    """Run `call_site(site)` in a separate process that is its own process
    group (so pytest is never signalled) and that ignores SIGINT itself."""
    env = shim.env()
    env["PYTHONPATH"] = os.pathsep.join([str(project_root / "src"), str(project_root)])
    return subprocess.Popen(
        [sys.executable, "-m", "tests.support.process_shims", "helper", site, str(workdir)],
        cwd=project_root,
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _helper_main(site: str, workdir: str) -> None:
    received: list[int] = []

    def on_sigint(signum, frame):
        received.append(signum)
        (Path(workdir) / "helper-sigint").write_text("")

    signal.signal(signal.SIGINT, on_sigint)
    result = call_site(site, Path(workdir))
    print(json.dumps({"received": received, "result": type(result).__name__}))


if __name__ == "__main__":
    if sys.argv[1] == "helper":
        _helper_main(sys.argv[2], sys.argv[3])
