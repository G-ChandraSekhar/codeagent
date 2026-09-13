"""
CodeAgent Stage 2 -- S4 spike: executor/container isolation.

Evidence-gathering only. Validates the ACTUAL production configuration
by importing `DEFAULT_IMAGE`, `_SECURITY_FLAGS`, `_MAX_STREAM_BYTES`,
`_BoundedCollector`, `CONTAINER_NAME_PREFIX`, and `DockerVerifier`
directly from `codeagent.executor` -- never an independently
maintained "equivalent" flag tuple. `src/codeagent` is only ever
imported from, never modified.

Two classes of containers, tracked two different ways (never claimed
to be "one manifest" -- they genuinely need different mechanisms):
  - Class A: real `DockerVerifier` instances, used wherever the exact
    unmodified production flag set applies. `DockerVerifier` names and
    removes these itself; this spike never adds them to its own
    resource manifest. Instead, a container-name snapshot is taken
    before any check runs and compared against the final snapshot: any
    name present only in the final snapshot and starting with the
    imported `CONTAINER_NAME_PREFIX` is an unexpected Class A leftover,
    retained as explicit evidence in `cleanup.json` and forced to fail
    the overall run if ever observed.
  - Class B: hand-rolled `docker create`/`run`, used only where a
    negative control needs one flag added/removed, or where a
    container must be inspected before removal (DockerVerifier always
    removes its own container before returning). Every Class B flag
    set is the imported `_SECURITY_FLAGS` tuple with one documented
    surgical change -- never retyped from scratch. Labeled
    `codeagent.spike=s4` plus a per-scenario `codeagent.run_id`, and
    tracked by exact name in this spike's own `Manifest`.

Platform scope: this run is macOS/Docker Desktop ONLY (see
docs/threat-model.md's A9 -- Linux and macOS/Docker Desktop are
separate evidence domains). Nothing here is written up as Linux
evidence. Retained per-run evidence is written under
`spikes/s4/evidence/<platform-key>/` (e.g.
`macos-docker-desktop-arm64/`) so a later Linux run cannot overwrite
this platform's evidence -- only `spike_s4.py`, `test_spike_s4.py`,
and `S4_RESULT.md` itself live directly under `spikes/s4/`.

Result vocabulary: every check reports PASS / FAIL / INCONCLUSIVE /
TECHNICAL_FAILURE for its primary observation and, where applicable,
its negative control, plus one combined classification. A negative
control that itself fails to demonstrate the expected behavior never
upgrades a check to PASS -- at most INCONCLUSIVE.

Cleanup: absence of a Class B container/network/image is confirmed via
the *unfiltered* listing command (`docker ps -a --format '{{.Names}}'`,
`docker network ls --format '{{.Name}}'`, `docker images --format
'{{.Repository}}:{{.Tag}}'`) split into lines and checked by exact
string equality -- never Docker's own `--filter name=...`, which is
substring/regex-based. This spike's own `Manifest` tracks every Class B
resource it creates and independently re-verifies each one absent at
the end; Class A leftovers are separately caught by the baseline/final
prefix-delta check described above. Nothing outside what this spike
itself created is ever deleted.
"""
from __future__ import annotations

import json
import platform
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from codeagent.executor import (  # noqa: E402
    CONTAINER_NAME_PREFIX,
    DEFAULT_IMAGE,
    DockerVerifier,
    _BoundedCollector,
    _MAX_STREAM_BYTES,
    _SECURITY_FLAGS,
)

SPIKE_DIR = Path(__file__).resolve().parent
LABEL = "codeagent.spike=s4"


def _platform_key() -> str:
    """A stable directory name for this platform's retained evidence,
    so a later run on a different platform (the planned Linux
    repetition) is written to its own directory and can never silently
    overwrite this one's evidence."""
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin":
        return f"macos-docker-desktop-{machine}"
    if system == "Linux":
        return f"linux-{machine}"
    return f"{system.lower()}-{machine}"


EVIDENCE_DIR = SPIKE_DIR / "evidence" / _platform_key()

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
TECHNICAL_FAILURE = "TECHNICAL_FAILURE"

_LOG_LINES: list[str] = []


def log(text: str = "") -> None:
    print(text)
    _LOG_LINES.append(text)


def run(argv: list[str], *, check: bool = True, **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(argv))
    result = subprocess.run(argv, capture_output=True, text=True, **kw)
    if result.stdout.strip():
        log(result.stdout)
    if result.stderr.strip():
        log(result.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def exit_code_for_overall_verdict(overall: str) -> int:
    """The process exit code main() should use for a given overall
    verdict, AFTER all evidence has already been written. FAIL must
    make the invoking workflow fail; PASS and PASS_WITH_OPEN_RISKS are
    both a successful evidence-gathering run (an open risk is reported
    accurately, not treated as a run failure) and exit 0."""
    return 1 if overall == FAIL else 0


def combine(primary: str, control: str | None = None) -> str:
    """Combines a primary observation with an optional negative
    control into one classification. A failed/inconclusive control
    never upgrades the result to PASS."""
    if primary in (FAIL, TECHNICAL_FAILURE):
        return primary
    if control is not None and control != PASS:
        return INCONCLUSIVE
    return primary


# --------------------------------------------------------------------
# Exact-equality resource listing (never Docker's own --filter)
# --------------------------------------------------------------------


def all_container_names() -> set[str]:
    result = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def all_network_names() -> set[str]:
    result = run(["docker", "network", "ls", "--format", "{{.Name}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def all_image_tags() -> set[str]:
    result = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def container_absent(name: str) -> bool:
    return name not in all_container_names()


def network_absent(name: str) -> bool:
    return name not in all_network_names()


def image_absent(tag: str) -> bool:
    return tag not in all_image_tags()


# --------------------------------------------------------------------
# Resource manifest: every container/network/image/dir this spike
# creates, so cleanup can be verified against a complete list rather
# than only a label scan.
# --------------------------------------------------------------------


class Manifest:
    def __init__(self) -> None:
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.images: list[str] = []
        self.dirs: list[Path] = []

    def add_container(self, name: str) -> str:
        self.containers.append(name)
        return name

    def add_network(self, name: str) -> str:
        self.networks.append(name)
        return name

    def add_image(self, tag: str) -> str:
        self.images.append(tag)
        return tag

    def add_dir(self, path: Path) -> Path:
        self.dirs.append(path)
        return path

    def cleanup_and_verify(self) -> dict:
        """Best-effort removal of everything tracked (skipping anything
        a per-scenario cleanup already removed, to avoid redundant
        "not found" noise), then independent re-verification of
        absence for every single entry. Never touches anything not in
        this manifest."""
        for name in self.containers:
            if not container_absent(name):
                run(["docker", "rm", "--force", name], check=False)
        for name in self.networks:
            if not network_absent(name):
                run(["docker", "network", "rm", name], check=False)
        for tag in self.images:
            if not image_absent(tag):
                run(["docker", "rmi", "--force", tag], check=False)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

        leftover_containers = [n for n in self.containers if not container_absent(n)]
        leftover_networks = [n for n in self.networks if not network_absent(n)]
        leftover_images = [t for t in self.images if not image_absent(t)]
        leftover_dirs = [str(d) for d in self.dirs if d.exists()]

        return {
            "containers_tracked": list(self.containers),
            "networks_tracked": list(self.networks),
            "images_tracked": list(self.images),
            "dirs_tracked": [str(d) for d in self.dirs],
            "leftover_containers": leftover_containers,
            "leftover_networks": leftover_networks,
            "leftover_images": leftover_images,
            "leftover_dirs": leftover_dirs,
            "all_clean": not (
                leftover_containers or leftover_networks or leftover_images or leftover_dirs
            ),
        }


MANIFEST = Manifest()


# --------------------------------------------------------------------
# Flag utilities -- derived from the imported _SECURITY_FLAGS, never
# an independently retyped tuple.
# --------------------------------------------------------------------


def flags_without(flag_name: str, has_value: bool = True) -> tuple[str, ...]:
    flags = list(_SECURITY_FLAGS)
    idx = flags.index(flag_name)
    end = idx + 2 if has_value else idx + 1
    return tuple(flags[:idx] + flags[end:])


def flags_with(*extra: str) -> tuple[str, ...]:
    return tuple(_SECURITY_FLAGS) + tuple(extra)


def flags_replacing_user(uid_gid: str) -> tuple[str, ...]:
    flags = list(flags_without("--user"))
    return tuple(flags + ["--user", uid_gid])


def new_scratch_dir(label: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"codeagent-spike-s4-{label}-"))
    MANIFEST.add_dir(d)
    return d


def new_name(label: str) -> str:
    name = f"codeagent-spike-s4-{label}-{uuid.uuid4().hex[:8]}"
    return name


# --------------------------------------------------------------------
# Custom image: derived from the exact pinned DEFAULT_IMAGE, no network
# or package-install step -- only filesystem operations against what
# the base image already ships (its own python3 interpreter). Serves
# three checks: non-root (root-owned 0600 canary-secret), read-only FS
# (world-writable canary-writable dir), no-new-privileges (setuid-root
# copy of python3).
# --------------------------------------------------------------------


def build_setuid_canary_image() -> str:
    tag = f"codeagent-spike-s4-setuid:{uuid.uuid4().hex[:8]}"
    build_dir = new_scratch_dir("image-build")
    dockerfile = build_dir / "Dockerfile"
    dockerfile.write_text(
        f"FROM {DEFAULT_IMAGE}\n"
        "RUN cp $(which python3) /usr/local/bin/python3-setuid-test \\\n"
        " && chown root:root /usr/local/bin/python3-setuid-test \\\n"
        " && chmod 4755 /usr/local/bin/python3-setuid-test \\\n"
        " && mkdir -m 0777 /canary-writable \\\n"
        " && echo secret-content > /canary-secret \\\n"
        " && chown root:root /canary-secret \\\n"
        " && chmod 0600 /canary-secret\n"
    )
    run(["docker", "build", "--no-cache", "-t", tag, "-f", str(dockerfile), str(build_dir)])
    MANIFEST.add_image(tag)
    return tag


# --------------------------------------------------------------------
# Check 1: network blocked
# --------------------------------------------------------------------


def check_network_blocked() -> dict:
    net_name = new_name("net")
    server_name = new_name("server")
    control_name = new_name("netctl")

    try:
        run(["docker", "network", "create", "--label", LABEL, net_name])
        MANIFEST.add_network(net_name)

        run(
            [
                "docker", "run", "-d",
                "--name", server_name, "--label", LABEL,
                "--network", net_name,
                DEFAULT_IMAGE, "python3", "-m", "http.server", "8000",
            ]
        )
        MANIFEST.add_container(server_name)
        time.sleep(1.0)  # let the server bind before anything connects to it

        ip_result = run(
            [
                "docker", "inspect",
                "--format", f'{{{{(index .NetworkSettings.Networks "{net_name}").IPAddress}}}}',
                server_name,
            ]
        )
        server_ip = ip_result.stdout.strip()
        if not server_ip:
            return {
                "check": "network_blocked",
                "classification": TECHNICAL_FAILURE,
                "notes": ["could not resolve peer-server IP; not a finding about isolation"],
            }

        connect_script = (
            "import socket,sys\n"
            f"s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(5)\n"
            "try:\n"
            f"    s.connect(('{server_ip}', 8000))\n"
            "    sys.exit(0)\n"  # connected
            "except Exception as e:\n"
            "    print('CONNECT_FAILED', repr(e))\n"
            "    sys.exit(1)\n"  # blocked
        )

        # Control: attached to the custom network -- must connect.
        control_result = run(
            [
                "docker", "run", "--rm",
                "--name", control_name, "--label", LABEL,
                "--network", net_name,
                DEFAULT_IMAGE, "python3", "-c", connect_script,
            ],
            check=False,
        )
        control = PASS if control_result.returncode == 0 else FAIL

        # Primary: real DockerVerifier, real --network none, not
        # attached to the custom network at all.
        workdir = new_scratch_dir("network-primary-workdir")
        verifier = DockerVerifier(workdir, command=("python3", "-c", connect_script))
        vresult = verifier.run_baseline()
        # Script exits 1 (isolation held, connect failed) -> TEST_FAILURE
        # from DockerVerifier's perspective, which is exactly what we want.
        primary = PASS if vresult.outcome.value == "test_failure" else (
            FAIL if vresult.outcome.value == "passed" else TECHNICAL_FAILURE
        )

        return {
            "check": "network_blocked",
            "primary": {"result": primary, "verifier_outcome": vresult.outcome.value,
                        "stdout": vresult.stdout, "exit_code": vresult.exit_code},
            "control": {"result": control, "stdout": control_result.stdout,
                        "returncode": control_result.returncode},
            "classification": combine(primary, control),
            "evidence": {"network": net_name, "server_ip": server_ip},
        }
    finally:
        run(["docker", "rm", "--force", server_name], check=False)
        run(["docker", "network", "rm", net_name], check=False)


# --------------------------------------------------------------------
# Check 2: no unintended host paths mounted (.Mounts for the bind,
# .HostConfig.Tmpfs for /tmp -- never assumed to appear in .Mounts)
# --------------------------------------------------------------------


def classify_mounts_check(
    mounts_ok: bool,
    tmpfs_ok: bool,
    exec_returncode: int,
    probe: dict | None,
) -> str:
    """Classification semantics for the mounts/host-isolation check:
    incorrect/unexpected mount configuration or a visible host canary
    is a real security FAIL. An inaccessible intended fixture,
    malformed probe output, or a docker-exec infrastructure failure is
    a TECHNICAL_FAILURE -- this harness's own problem, not a security
    finding, and must never be reported as if it were one."""
    if not mounts_ok or not tmpfs_ok:
        return FAIL
    if exec_returncode != 0 or probe is None:
        return TECHNICAL_FAILURE
    if probe.get("host_secret_absent_at_root") is False:
        return FAIL
    if probe.get("host_secret_absent_at_host_path") is False:
        return FAIL
    if not probe.get("expected_readable") or not probe.get("expected_content_matches"):
        return TECHNICAL_FAILURE
    if probe.get("host_secret_absent_at_root") is not True:
        return TECHNICAL_FAILURE
    if probe.get("host_secret_absent_at_host_path") is not True:
        return TECHNICAL_FAILURE
    return PASS


def check_mounts_and_host_isolation() -> dict:
    name = new_name("mounts")
    workdir = new_scratch_dir("mounts-workspace")
    expected_file = workdir / "expected.txt"
    expected_file.write_text("expected content\n")
    # tempfile.mkdtemp() creates directories mode 0700, owned by
    # whichever host UID ran this script. Native Linux bind mounts
    # preserve those numeric permissions as-is inside the container --
    # if the host UID differs from the container's configured UID
    # 1000 (routinely true on a CI runner), 1000 cannot even traverse
    # a 0700 directory it doesn't own, regardless of the file's own
    # permissions. This is the intended-to-be-readable fixture for
    # this check specifically, so it (and only it) is loosened to
    # 0755/0644 -- not a general scratch-directory policy change, and
    # the secret-canary directory below is deliberately left alone.
    workdir.chmod(0o755)
    expected_file.chmod(0o644)

    canary_host_dir = new_scratch_dir("mounts-canary")
    canary_file = canary_host_dir / "host-secret.txt"
    canary_file.write_text("host secret content\n")

    workdir_stat = workdir.stat()
    host_mount_source_mode_and_owner = {
        "path": str(workdir),
        "mode_octal": oct(stat_module.S_IMODE(workdir_stat.st_mode)),
        "uid": workdir_stat.st_uid,
    }

    run(
        [
            "docker", "create", "--name", name, "--label", LABEL,
            *_SECURITY_FLAGS,
            "--mount", f"type=bind,source={workdir},target=/workspace,readonly",
            "--workdir", "/workspace",
            DEFAULT_IMAGE, "sleep", "30",
        ]
    )
    MANIFEST.add_container(name)

    mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", name]).stdout)
    tmpfs = json.loads(run(["docker", "inspect", "--format", "{{json .HostConfig.Tmpfs}}", name]).stdout)

    mounts_ok = (
        len(mounts) == 1
        and mounts[0]["Type"] == "bind"
        and mounts[0]["Destination"] == "/workspace"
        and mounts[0]["RW"] is False
    )
    tmpfs_ok = "/tmp" in tmpfs

    run(["docker", "start", name])
    # A structured probe reporting each fact independently, rather than
    # one combined assertion that aborts at the first failure -- every
    # field is retained as individual machine-readable evidence even
    # if one of the others fails.
    probe_script = (
        "import json, os\n"
        "result = {}\n"
        "try:\n"
        "    with open('/workspace/expected.txt') as f:\n"
        "        content = f.read()\n"
        "    result['expected_readable'] = True\n"
        "    result['expected_content_matches'] = (content == 'expected content\\n')\n"
        "except Exception as exc:\n"
        "    result['expected_readable'] = False\n"
        "    result['expected_content_matches'] = False\n"
        "    result['expected_error'] = repr(exc)\n"
        "result['host_secret_absent_at_root'] = not os.path.exists('/host-secret.txt')\n"
        f"result['host_secret_absent_at_host_path'] = not os.path.exists({str(canary_file)!r})\n"
        "print(json.dumps(result))\n"
    )
    exec_result = run(["docker", "exec", name, "python3", "-B", "-c", probe_script], check=False)

    probe: dict | None
    try:
        probe = json.loads(exec_result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        probe = None

    run(["docker", "kill", name], check=False)

    classification = classify_mounts_check(mounts_ok, tmpfs_ok, exec_result.returncode, probe)
    return {
        "check": "mounts_and_host_isolation",
        "primary": {
            "result": classification,
            "mounts": mounts,
            "tmpfs": tmpfs,
            "mounts_ok": mounts_ok,
            "tmpfs_ok": tmpfs_ok,
            "host_mount_source_mode_and_owner": host_mount_source_mode_and_owner,
            "exec_returncode": exec_result.returncode,
            "probe": probe,
            "host_isolation_stdout": exec_result.stdout,
            "host_isolation_stderr": exec_result.stderr,
        },
        "classification": classification,
    }


# --------------------------------------------------------------------
# Check 3: non-root, via a root-owned mode-0600 canary file in the
# custom image -- a UID-permission failure, not a low-port/setuid
# ambiguity.
# --------------------------------------------------------------------


def check_non_root(setuid_image: str) -> dict:
    read_canary_script = (
        "import os, sys\n"
        "print('uid', os.getuid(), 'euid', os.geteuid())\n"
        "try:\n"
        "    open('/canary-secret').read()\n"
        "    sys.exit(1)\n"
        "except PermissionError:\n"
        "    sys.exit(0)\n"
    )
    primary_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
         setuid_image, "python3", "-B", "-c", read_canary_script],
        check=False,
    )
    primary = PASS if primary_result.returncode == 0 else FAIL

    control_flags = flags_replacing_user("0:0")
    control_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *control_flags,
         setuid_image, "python3", "-B", "-c",
         "import os; print('uid', os.getuid(), 'euid', os.geteuid()); "
         "print(open('/canary-secret').read())"],
        check=False,
    )
    control = PASS if control_result.returncode == 0 else FAIL

    return {
        "check": "non_root",
        "primary": {"result": primary, "stdout": primary_result.stdout, "returncode": primary_result.returncode},
        "control": {"result": control, "stdout": control_result.stdout, "returncode": control_result.returncode},
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 4: CAP_SYS_ADMIN unavailable, via a real mount(2) syscall.
# A dedicated root-user pair, since uid 1000 alone would confound
# whether a failure is due to capabilities or to plain UID permissions.
# --------------------------------------------------------------------

_MOUNT_SCRIPT = (
    "import ctypes, os, sys\n"
    "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
    # /tmp (not /mnt) -- the only writable location under the real
    # --read-only root, so a failure here is attributable to the
    # capability check alone, never confounded with read-only-fs
    # blocking the makedirs step before mount(2) is even attempted.
    "os.makedirs('/tmp/spiketest', exist_ok=True)\n"
    "res = libc.mount(b'tmpfs', b'/tmp/spiketest', b'tmpfs', 0, None)\n"
    "if res != 0:\n"
    "    e = ctypes.get_errno()\n"
    "    print('MOUNT_FAILED', e, os.strerror(e))\n"
    "    sys.exit(1)\n"
    "else:\n"
    "    print('MOUNT_SUCCEEDED')\n"
    "    sys.exit(0)\n"
)


def check_cap_sys_admin() -> dict:
    root_flags = flags_replacing_user("0:0")

    primary_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *root_flags, DEFAULT_IMAGE,
         "python3", "-B", "-c", _MOUNT_SCRIPT],
        check=False,
    )
    # Expect the mount to FAIL (nonzero exit) -- that's the isolation holding.
    primary = PASS if primary_result.returncode != 0 else FAIL

    control_flags = root_flags + ("--cap-add", "SYS_ADMIN")
    control_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *control_flags, DEFAULT_IMAGE,
         "python3", "-B", "-c", _MOUNT_SCRIPT],
        check=False,
    )
    control = PASS if control_result.returncode == 0 else INCONCLUSIVE

    capeff_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS, DEFAULT_IMAGE,
         "sh", "-c", "grep CapEff /proc/self/status"],
        check=False,
    )

    notes = []
    if control != PASS:
        notes.append(
            "control did not escalate even with --cap-add SYS_ADMIN -- likely a "
            "runtime restriction (seccomp/AppArmor) beyond capabilities alone; "
            "not weakened further to force a pass, classified INCONCLUSIVE instead"
        )

    return {
        "check": "cap_sys_admin",
        "primary": {"result": primary, "stdout": primary_result.stdout, "returncode": primary_result.returncode},
        "control": {"result": control, "stdout": control_result.stdout, "returncode": control_result.returncode},
        "supporting_evidence": {"production_container_capeff": capeff_result.stdout.strip()},
        "notes": notes,
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 5: no-new-privileges, via a real setuid-root escalation
# attempt against the custom image's setuid-root python3 copy.
# --------------------------------------------------------------------

_SETUID_REPORT_SCRIPT = "import os; print('uid', os.getuid(), 'euid', os.geteuid())"


def check_no_new_privileges(setuid_image: str) -> dict:
    primary_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
         setuid_image, "/usr/local/bin/python3-setuid-test", "-c", _SETUID_REPORT_SCRIPT],
        check=False,
    )
    primary_escalated = "euid 0" in primary_result.stdout
    primary = PASS if (not primary_escalated) else FAIL

    control_flags = flags_without("--security-opt")
    control_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *control_flags,
         setuid_image, "/usr/local/bin/python3-setuid-test", "-c", _SETUID_REPORT_SCRIPT],
        check=False,
    )
    control_escalated = "euid 0" in control_result.stdout
    control = PASS if control_escalated else INCONCLUSIVE

    nnp_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS, setuid_image,
         "sh", "-c", "grep NoNewPrivs /proc/self/status"],
        check=False,
    )

    notes = []
    if control != PASS:
        notes.append(
            "the setuid control did not escalate even with no-new-privileges removed "
            "(e.g. a nosuid-mounted layer) -- /proc/self/status's NoNewPrivs field is "
            "recorded as supporting evidence only, never substituted as equivalent "
            "behavioral proof; classified INCONCLUSIVE rather than PASS"
        )

    return {
        "check": "no_new_privileges",
        "primary": {"result": primary, "stdout": primary_result.stdout},
        "control": {"result": control, "stdout": control_result.stdout},
        "supporting_evidence": {"proc_status_nonewprivs": nnp_result.stdout.strip()},
        "notes": notes,
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 6: read-only root filesystem, via a world-writable canary
# directory baked into the custom image -- a write failure there can
# only be attributed to --read-only, never to UID permissions.
# --------------------------------------------------------------------

_WRITE_CANARY_SCRIPT = "open('/canary-writable/f.txt','w').write('x'); print('WRITE_SUCCEEDED')"


def check_read_only_fs(setuid_image: str) -> dict:
    primary_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
         setuid_image, "python3", "-B", "-c", _WRITE_CANARY_SCRIPT],
        check=False,
    )
    primary = PASS if primary_result.returncode != 0 else FAIL

    control_flags = flags_without("--read-only", has_value=False)
    control_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *control_flags,
         setuid_image, "python3", "-B", "-c", _WRITE_CANARY_SCRIPT],
        check=False,
    )
    control = PASS if control_result.returncode == 0 else FAIL

    return {
        "check": "read_only_fs",
        "primary": {"result": primary, "stdout": primary_result.stdout, "stderr": primary_result.stderr},
        "control": {"result": control, "stdout": control_result.stdout},
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 7: the workspace bind mount itself is read-only, and /tmp is
# writable -- distinct from check 6 (root filesystem generally).
# --------------------------------------------------------------------


def check_workspace_readonly_bind() -> dict:
    host_dir = new_scratch_dir("workspace-bind")
    host_dir.chmod(0o777)
    canary = host_dir / "canary.txt"
    canary.write_text("original\n")
    canary.chmod(0o666)

    write_script = "open('canary.txt','a').write('MUTATED'); print('WRITE_SUCCEEDED')"
    tmp_write_script = "open('/tmp/f.txt','w').write('x'); print('TMP_WRITE_SUCCEEDED')"

    result_ro = run(
        [
            "docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
            "--mount", f"type=bind,source={host_dir},target=/workspace,readonly",
            "--workdir", "/workspace",
            DEFAULT_IMAGE, "python3", "-B", "-c", write_script,
        ],
        check=False,
    )
    primary_ro_blocked = result_ro.returncode != 0

    tmp_result = run(
        [
            "docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
            "--mount", f"type=bind,source={host_dir},target=/workspace,readonly",
            "--workdir", "/workspace",
            DEFAULT_IMAGE, "python3", "-B", "-c", tmp_write_script,
        ],
        check=False,
    )
    tmp_write_ok = tmp_result.returncode == 0

    primary = PASS if (primary_ro_blocked and tmp_write_ok) else FAIL

    control_dir = new_scratch_dir("workspace-bind-control")
    control_dir.chmod(0o777)
    control_canary = control_dir / "canary.txt"
    control_canary.write_text("original\n")
    control_canary.chmod(0o666)

    result_rw = run(
        [
            "docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
            "--mount", f"type=bind,source={control_dir},target=/workspace",
            "--workdir", "/workspace",
            DEFAULT_IMAGE, "python3", "-B", "-c", write_script,
        ],
        check=False,
    )
    control = PASS if result_rw.returncode == 0 else FAIL

    return {
        "check": "workspace_readonly_bind",
        "primary": {
            "result": primary,
            "readonly_write_blocked": primary_ro_blocked,
            "readonly_write_stdout": result_ro.stdout,
            "readonly_write_stderr": result_ro.stderr,
            "tmp_write_succeeded": tmp_write_ok,
            "host_canary_unchanged": canary.read_text() == "original\n",
        },
        "control": {
            "result": control,
            "stdout": result_rw.stdout,
            "host_canary_mutated": control_canary.read_text() != "original\n",
        },
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 8: Docker socket absent
# --------------------------------------------------------------------


def check_docker_socket_absent() -> dict:
    workdir = new_scratch_dir("dockersock-workdir")
    verifier = DockerVerifier(
        workdir,
        command=(
            "python3", "-B", "-c",
            "import os, sys\n"
            "present = os.path.exists('/var/run/docker.sock') or os.path.exists('/run/docker.sock')\n"
            "sys.exit(1 if present else 0)\n",
        ),
    )
    result = verifier.run_baseline()
    primary = PASS if result.outcome.value == "passed" else FAIL
    return {
        "check": "docker_socket_absent",
        "primary": {"result": primary, "verifier_outcome": result.outcome.value},
        "classification": primary,
    }


# --------------------------------------------------------------------
# Check 9: host environment non-inheritance and explicit environment
# passing. This validates Docker's own mechanism and the current
# deny-by-default behavior a container gets from `docker run` -- it is
# NOT a claim that CodeAgent implements an environment-allowlist API;
# `DockerVerifier` today passes no `--env` flags at all. What this
# shows is: (a) the launching host process's own environment is never
# silently inherited, and (b) a caller who explicitly opts a variable
# in via `--env` does see it -- the raw material an allowlist could be
# built from, not an allowlist itself.
# --------------------------------------------------------------------


def check_env_non_inheritance() -> dict:
    import os as _os

    host_canary_name = "CODEAGENT_SPIKE_HOST_ONLY"
    allow_name = "CODEAGENT_SPIKE_ALLOWED"
    _os.environ[host_canary_name] = "leak-me-if-inherited"
    try:
        result = run(
            [
                "docker", "run", "--rm", "--label", LABEL, *_SECURITY_FLAGS,
                "--env", f"{allow_name}=present-value",
                DEFAULT_IMAGE, "sh", "-c", "env",
            ],
            check=False,
        )
    finally:
        del _os.environ[host_canary_name]

    lines = result.stdout.splitlines()
    host_canary_absent = not any(line.startswith(f"{host_canary_name}=") for line in lines)
    allow_present = f"{allow_name}=present-value" in lines

    primary = PASS if (host_canary_absent and allow_present) else FAIL
    return {
        "check": "env_non_inheritance",
        "primary": {
            "result": primary,
            "host_canary_absent": host_canary_absent,
            "allowlisted_canary_present": allow_present,
        },
        "evidence": {"observed_baseline_env": lines},
        "classification": primary,
    }


# --------------------------------------------------------------------
# Check 10: bounded stdout/stderr, via the real DockerVerifier and the
# real _BoundedCollector/_MAX_STREAM_BYTES.
# --------------------------------------------------------------------


def check_bounded_output() -> dict:
    marker_collector = _BoundedCollector(1)
    marker_collector.feed(b"x")
    marker_collector.feed(b"y")  # forces truncation
    marker_suffix = marker_collector.text()[1:]  # the "...(truncated)" suffix, derived not hardcoded

    workdir = new_scratch_dir("output-workdir")
    oversized = 10 * 1024 * 1024
    verifier = DockerVerifier(
        workdir,
        command=(
            "python3", "-B", "-c",
            f"import sys; sys.stdout.write('A'*{oversized}); sys.stderr.write('B'*{oversized})",
        ),
    )
    result = verifier.run_baseline()

    stdout_bytes = len(result.stdout.encode("utf-8"))
    stderr_bytes = len(result.stderr.encode("utf-8"))
    bound = _MAX_STREAM_BYTES + len(marker_suffix.encode("utf-8"))

    stdout_ok = stdout_bytes <= bound and result.stdout.endswith(marker_suffix)
    stderr_ok = stderr_bytes <= bound and result.stderr.endswith(marker_suffix)

    primary = PASS if (stdout_ok and stderr_ok) else FAIL
    return {
        "check": "bounded_output",
        "primary": {
            "result": primary,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "bound": bound,
            "marker_suffix": marker_suffix,
            "verifier_outcome": result.outcome.value,
        },
        "classification": primary,
    }


# --------------------------------------------------------------------
# Check 11: timeout enforcement, with the new container identified by
# set difference against a snapshot taken before starting.
# --------------------------------------------------------------------


def check_timeout() -> dict:
    before_names = all_container_names()
    workdir = new_scratch_dir("timeout-workdir")
    verifier = DockerVerifier(workdir, command=("sleep", "30"), timeout_seconds=3)

    observed = {"name": None, "status": None}
    stop_polling = threading.Event()

    def poll() -> None:
        deadline = time.time() + 8
        while time.time() < deadline and not stop_polling.is_set():
            new_names = all_container_names() - before_names
            candidates = [n for n in new_names if n.startswith(CONTAINER_NAME_PREFIX)]
            if candidates:
                name = candidates[0]
                status_result = run(["docker", "inspect", "--format", "{{.State.Status}}", name], check=False)
                if status_result.returncode == 0:
                    observed["name"] = name
                    observed["status"] = status_result.stdout.strip()
                    if observed["status"] == "running":
                        return
            time.sleep(0.05)

    poll_thread = threading.Thread(target=poll)
    start = time.time()
    poll_thread.start()
    result = verifier.run_baseline()
    elapsed = time.time() - start
    stop_polling.set()
    poll_thread.join(timeout=2)

    outcome_ok = result.outcome.value == "timeout"
    elapsed_ok = elapsed < 15
    container_was_running = observed["status"] == "running"
    container_confirmed_gone = observed["name"] is not None and container_absent(observed["name"])

    primary = PASS if (outcome_ok and elapsed_ok and container_was_running and container_confirmed_gone) else FAIL
    return {
        "check": "timeout_enforcement",
        "primary": {
            "result": primary,
            "outcome": result.outcome.value,
            "elapsed_seconds": elapsed,
            "observed_container_name": observed["name"],
            "observed_status_during_wait": observed["status"],
            "confirmed_gone_after": container_confirmed_gone,
        },
        "classification": primary,
    }


# --------------------------------------------------------------------
# Check 12: PID-limit enforcement -- a fixed, linear fork loop, never
# recursive/self-replicating.
# --------------------------------------------------------------------

_PID_LIMIT_SCRIPT = (
    "import os, time\n"
    "succeeded = 0\n"
    "failed_at = None\n"
    "children = []\n"
    "for i in range(1, 141):\n"
    "    try:\n"
    "        pid = os.fork()\n"
    "    except OSError:\n"
    "        failed_at = i\n"
    "        break\n"
    "    if pid == 0:\n"
    "        time.sleep(1.5)\n"
    "        os._exit(0)\n"
    "    else:\n"
    "        children.append(pid)\n"
    "        succeeded += 1\n"
    "try:\n"
    "    with open('/sys/fs/cgroup/pids.current') as f:\n"
    "        current = f.read().strip()\n"
    "except OSError:\n"
    "    current = None\n"
    "try:\n"
    "    with open('/sys/fs/cgroup/pids.max') as f:\n"
    "        limit = f.read().strip()\n"
    "except OSError:\n"
    "    limit = None\n"
    "for pid in children:\n"
    "    try:\n"
    "        os.waitpid(pid, 0)\n"
    "    except ChildProcessError:\n"
    "        pass\n"
    "print(f'RESULT succeeded={succeeded} failed_at={failed_at} pids_current={current} pids_max={limit}')\n"
)


def check_pid_limit() -> dict:
    workdir = new_scratch_dir("pidlimit-workdir")
    verifier = DockerVerifier(workdir, command=("python3", "-B", "-c", _PID_LIMIT_SCRIPT), timeout_seconds=15)
    result = verifier.run_baseline()

    parsed: dict[str, str] = {}
    for token in result.stdout.strip().split():
        if "=" in token:
            k, v = token.split("=", 1)
            parsed[k] = v

    failed_at = parsed.get("failed_at")
    hit_limit = failed_at not in (None, "None")
    pids_current = int(parsed["pids_current"]) if parsed.get("pids_current", "None") not in (None, "None") else None
    pids_max_raw = parsed.get("pids_max")
    pids_max = int(pids_max_raw) if pids_max_raw not in (None, "None", "max") else None
    within_limit = pids_current is None or pids_max is None or pids_current <= pids_max

    primary = PASS if (hit_limit and within_limit and result.outcome.value == "passed") else FAIL
    return {
        "check": "pid_limit",
        "primary": {
            "result": primary,
            "raw_stdout": result.stdout.strip(),
            "parsed": parsed,
            "hit_limit_within_bounded_attempts": hit_limit,
            "pids_current_never_exceeded_limit": within_limit,
            "verifier_outcome": result.outcome.value,
        },
        "classification": primary,
    }


# --------------------------------------------------------------------
# Check 13: CPU limit -- four workers for a fixed duration, cgroup
# accounting rather than a single docker-stats sample, compared
# against a no-limit negative control.
# --------------------------------------------------------------------

_CPU_SCRIPT_TEMPLATE = (
    "import time, subprocess\n"
    "def usage_usec():\n"
    "    with open('/sys/fs/cgroup/cpu.stat') as f:\n"
    "        for line in f:\n"
    "            if line.startswith('usage_usec'):\n"
    "                return int(line.split()[1])\n"
    "start_wall = time.time()\n"
    "start_usage = usage_usec()\n"
    "procs = [subprocess.Popen(['python3','-B','-c',"
    "'import time\\nt=time.time()\\nwhile time.time()-t<{duration}: pass']) "
    "for _ in range({workers})]\n"
    "for p in procs: p.wait()\n"
    "end_wall = time.time()\n"
    "end_usage = usage_usec()\n"
    "cores = (end_usage-start_usage)/1_000_000 / (end_wall-start_wall)\n"
    "print(f'CORES={{cores:.3f}}')\n"
)


def _parse_cores(stdout: str) -> float | None:
    for line in stdout.splitlines():
        if line.startswith("CORES="):
            try:
                return float(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def check_cpu_limit() -> dict:
    script = _CPU_SCRIPT_TEMPLATE.format(duration=4, workers=4)
    workdir = new_scratch_dir("cpu-workdir")

    verifier = DockerVerifier(workdir, command=("python3", "-B", "-c", script), timeout_seconds=15)
    primary_result = verifier.run_baseline()
    primary_cores = _parse_cores(primary_result.stdout)
    tolerance = 1.3
    if primary_cores is None:
        primary = TECHNICAL_FAILURE
    else:
        primary = PASS if primary_cores <= tolerance else FAIL

    control_flags = flags_without("--cpus")
    control_result = run(
        ["docker", "run", "--rm", "--label", LABEL, *control_flags, DEFAULT_IMAGE,
         "python3", "-B", "-c", script],
        check=False,
    )
    control_cores = _parse_cores(control_result.stdout)
    notes = []
    if control_cores is None:
        control = TECHNICAL_FAILURE
    elif control_cores <= tolerance:
        control = INCONCLUSIVE
        notes.append(
            f"negative control measured only {control_cores:.3f} core-equivalents "
            "without --cpus -- this host/VM likely can't demonstrate more than one "
            "core to this container, so the primary result alone doesn't prove "
            "throttling; classified INCONCLUSIVE rather than assumed confirmed"
        )
    else:
        control = PASS

    return {
        "check": "cpu_limit",
        "primary": {"result": primary, "measured_cores": primary_cores, "stdout": primary_result.stdout},
        "control": {"result": control, "measured_cores": control_cores, "stdout": control_result.stdout},
        "notes": notes,
        "classification": combine(primary, control),
    }


# --------------------------------------------------------------------
# Check 14: memory limit -- hand-controlled lifecycle (create/start/
# wait/inspect/remove) so .State.OOMKilled and the configured memory/
# swap values can be read before the container is removed.
# --------------------------------------------------------------------

def _memory_script(megabytes: int) -> str:
    return (
        "chunks = []\n"
        "try:\n"
        f"    for _ in range({megabytes}):\n"
        "        b = bytearray(1024 * 1024)\n"
        "        for i in range(0, len(b), 4096):\n"
        "            b[i] = 1\n"
        "        chunks.append(b)\n"
        "    print('ALLOCATION_SURVIVED', len(chunks))\n"
        "except MemoryError:\n"
        "    print('MEMORY_ERROR_RAISED')\n"
    )


_MEMORY_SCRIPT = _memory_script(600)
# A fixed, bounded (not unbounded/exponential) larger allocation, used
# for a SECOND, separate hand-controlled experiment: the 600 MB
# allocation against a 512 MB limit did not trigger an OOM kill in this
# environment (see check_memory_limit's own notes on why), so a larger
# fixed target is needed to directly confirm what a genuine OOM kill
# looks like on this platform, inspected before its own removal --
# never inferred from exit code alone.
_MEMORY_SCRIPT_LARGE = _memory_script(1900)


def _memory_experiment(name: str, megabytes: int) -> dict:
    """A hand-controlled create/start/wait/inspect/remove lifecycle for
    one fixed allocation size. Reports State.OOMKilled and
    State.ExitCode directly, inspected before the container is
    removed -- an exit code alone (137 or otherwise) is never treated
    as proof of an OOM kill; only the inspected OOMKilled field is."""
    workdir = new_scratch_dir(f"memory-workdir-{megabytes}mb")
    run(
        [
            "docker", "create", "--name", name, "--label", LABEL,
            *_SECURITY_FLAGS,
            "--mount", f"type=bind,source={workdir},target=/workspace,readonly",
            "--workdir", "/workspace",
            DEFAULT_IMAGE, "python3", "-B", "-c", _memory_script(megabytes),
        ]
    )
    MANIFEST.add_container(name)

    run(["docker", "start", name])
    try:
        wait_proc = subprocess.run(["docker", "wait", name], capture_output=True, text=True, timeout=20)
        wait_exit_code_raw = wait_proc.stdout.strip()
        wait_timed_out = False
    except subprocess.TimeoutExpired:
        run(["docker", "kill", name], check=False)
        wait_exit_code_raw = None
        wait_timed_out = True

    oom_killed = run(["docker", "inspect", "--format", "{{.State.OOMKilled}}", name], check=False).stdout.strip() == "true"
    exit_code_str = run(["docker", "inspect", "--format", "{{.State.ExitCode}}", name], check=False).stdout.strip()
    try:
        exit_code: int | None = int(exit_code_str)
    except ValueError:
        exit_code = None
    configured_memory = run(["docker", "inspect", "--format", "{{.HostConfig.Memory}}", name], check=False).stdout.strip()
    configured_memswap = run(["docker", "inspect", "--format", "{{.HostConfig.MemorySwap}}", name], check=False).stdout.strip()
    container_logs = run(["docker", "logs", name], check=False).stdout

    return {
        "name": name,
        "megabytes_allocated_target": megabytes,
        "oom_killed": oom_killed,
        "exit_code": exit_code,
        "configured_memory_bytes": configured_memory,
        "configured_memswap_bytes": configured_memswap,
        "wait_exit_code_raw": wait_exit_code_raw,
        "wait_timed_out": wait_timed_out,
        "container_stdout": container_logs,
    }


def check_memory_limit() -> dict:
    # Experiment 1: the primary, fixed 600 MB allocation. Hand-
    # controlled lifecycle so State.OOMKilled is inspected directly.
    experiment_600 = _memory_experiment(new_name("mem-600"), 600)

    notes = []
    if experiment_600["oom_killed"]:
        primary = PASS
    else:
        primary = INCONCLUSIVE
        notes.append(
            "the 600 MB allocation did not trigger an OOM kill (State.OOMKilled: "
            "false). --memory=512m limits this container's own memory usage to "
            "512 MiB, but the observed Docker HostConfig on this host sets "
            f"MemorySwap={experiment_600['configured_memswap_bytes']} bytes "
            f"(Memory={experiment_600['configured_memory_bytes']} bytes), which "
            "permits additional swap on top of that, so the combined memory+swap "
            "allowance available to this container is larger than 512 MiB alone. "
            "This is reported as an observation on this specific host, not "
            "generalized into a claim about every Docker Engine installation. "
            "--memory-swap=512m would prevent that additional swap and cap the "
            "combined allowance at 512 MiB. Not fixed here -- src/codeagent is "
            "not modified during this spike."
        )

    # Experiment 2: a second, separate hand-controlled container with a
    # larger fixed (1900 MB) allocation, specifically to directly
    # confirm State.OOMKilled/State.ExitCode for a genuine OOM kill on
    # this platform -- inspected for THIS container, before its own
    # removal, not inferred from any other container's behavior.
    experiment_1900 = _memory_experiment(new_name("mem-1900"), 1900)
    if not experiment_1900["oom_killed"]:
        notes.append(
            "the larger 1900 MB allocation also did not directly confirm an OOM "
            "kill on this run (State.OOMKilled: false) -- see its own recorded "
            "fields; no inference about DockerVerifier's disposition of an OOM "
            "kill is drawn below in that case."
        )

    # Separate observation, a DIFFERENT container: the identical 1900 MB
    # allocation run through the real DockerVerifier, which always
    # removes its own container before returning -- State.OOMKilled
    # cannot be inspected for this exact instance. Only what
    # DockerVerifier itself directly establishes (exit code, outcome)
    # is reported as fact; any connection to the confirmed-OOM twin
    # above is stated explicitly as an inference, not fused into one
    # "OOM confirmed" claim.
    workdir2 = new_scratch_dir("memory-disposition-workdir")
    verifier = DockerVerifier(workdir2, command=("python3", "-B", "-c", _MEMORY_SCRIPT_LARGE), timeout_seconds=20)
    vresult = verifier.run_baseline()

    twin_confirmed_oom_at_exit_137 = (
        experiment_1900["oom_killed"] and experiment_1900["exit_code"] == 137
    )
    verifier_run_matches_confirmed_oom_twin = (
        twin_confirmed_oom_at_exit_137 and vresult.exit_code == 137
    )
    disposition_gap_inferred = (
        verifier_run_matches_confirmed_oom_twin and vresult.outcome.value == "test_failure"
    )
    if disposition_gap_inferred:
        notes.append(
            "PRODUCTION DISPOSITION GAP (inferred, not directly proven for this "
            "exact container, not fixed): a separate, hand-controlled container "
            "using the identical image, flags, and 1900 MB allocation script was "
            "directly confirmed OOM-killed (State.OOMKilled=true, "
            "State.ExitCode=137). The real DockerVerifier, run separately against "
            "the same command and flags, also exited 137 and was classified "
            "TEST_FAILURE by DockerVerifier._attempt. Because DockerVerifier "
            "removes its own container before returning, State.OOMKilled could "
            "not be inspected for that specific instance -- this is a strongly "
            "supported inference that it was also OOM-killed, not a directly "
            "confirmed fact for that container. If the inference holds, "
            "DockerVerifier._attempt's exit-code-only disposition logic cannot "
            "distinguish an OOM kill (an operational/environment problem) from an "
            "ordinary failing test, and classifies both as TEST_FAILURE. "
            "src/codeagent was not modified to address this."
        )

    return {
        "check": "memory_limit",
        "primary": {"result": primary, **experiment_600},
        "large_allocation_hand_controlled": experiment_1900,
        "production_disposition_check": {
            "verifier_outcome": vresult.outcome.value,
            "verifier_exit_code": vresult.exit_code,
            "twin_confirmed_oom_at_exit_137": twin_confirmed_oom_at_exit_137,
            "verifier_run_matches_confirmed_oom_twin": verifier_run_matches_confirmed_oom_twin,
            "disposition_gap_inferred": disposition_gap_inferred,
        },
        "notes": notes,
        "classification": primary,
    }


# --------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------

CHECKS: list[tuple[str, str]] = [
    ("network_blocked", "check_network_blocked"),
    ("mounts_and_host_isolation", "check_mounts_and_host_isolation"),
    ("non_root", "check_non_root"),
    ("cap_sys_admin", "check_cap_sys_admin"),
    ("no_new_privileges", "check_no_new_privileges"),
    ("read_only_fs", "check_read_only_fs"),
    ("workspace_readonly_bind", "check_workspace_readonly_bind"),
    ("docker_socket_absent", "check_docker_socket_absent"),
    ("env_non_inheritance", "check_env_non_inheritance"),
    ("bounded_output", "check_bounded_output"),
    ("timeout_enforcement", "check_timeout"),
    ("pid_limit", "check_pid_limit"),
    ("cpu_limit", "check_cpu_limit"),
    ("memory_limit", "check_memory_limit"),
]

_NEEDS_SETUID_IMAGE = {"non_root", "no_new_privileges", "read_only_fs"}


def main() -> None:
    # Fail loudly (the default `check=True`) rather than silently
    # recording an empty docker_version/cgroup_version if the daemon
    # can't answer these -- an empty value there would be indistinguishable
    # from "successfully queried and reported nothing."
    docker_version = run(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip()
    cgroup_version = run(["docker", "info", "--format", "{{.CgroupVersion}}"]).stdout.strip()
    if not docker_version or not cgroup_version:
        raise RuntimeError(
            f"host metadata query returned an empty value "
            f"(docker_version={docker_version!r}, cgroup_version={cgroup_version!r}) "
            "-- treating this as a failure rather than recording it as evidence"
        )

    host_info = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "docker_version": docker_version,
        "cgroup_version": cgroup_version,
    }
    write(EVIDENCE_DIR / "host.json", json.dumps(host_info, indent=2))
    log("host: " + json.dumps(host_info))
    log(
        "PLATFORM SCOPE: this run is macOS/Docker Desktop evidence only "
        "(threat-model.md A9) -- not a Linux claim. "
        f"Evidence directory: {EVIDENCE_DIR}"
    )

    tested_config = {"image": DEFAULT_IMAGE, "security_flags": list(_SECURITY_FLAGS)}
    write(EVIDENCE_DIR / "tested_config.json", json.dumps(tested_config, indent=2))
    log("tested production config: " + json.dumps(tested_config))

    # Class A containers (real DockerVerifier instances) name and
    # remove themselves -- never added to MANIFEST. Caught instead by
    # comparing this baseline snapshot against the final one below.
    class_a_baseline_names = all_container_names()

    results: dict[str, dict] = {}
    setuid_image: str | None = None
    try:
        setuid_image = build_setuid_canary_image()
        namespace = globals()
        for key, func_name in CHECKS:
            log(f"\n--- CHECK: {key} ---")
            func = namespace[func_name]
            try:
                if key in _NEEDS_SETUID_IMAGE:
                    result = func(setuid_image)
                else:
                    result = func()
            except Exception as exc:  # noqa: BLE001 -- record, don't crash the whole spike
                result = {
                    "check": key,
                    "classification": TECHNICAL_FAILURE,
                    "error": repr(exc),
                }
                log(f"TECHNICAL_FAILURE in {key}: {exc!r}")
            results[key] = result
            write(EVIDENCE_DIR / f"check_{key}.json", json.dumps(result, indent=2, default=str))
    finally:
        cleanup = MANIFEST.cleanup_and_verify()

        # Class A leftover detection: any name present now that starts
        # with CONTAINER_NAME_PREFIX and wasn't present at the start is
        # a DockerVerifier container that failed to confirm its own
        # removal -- a real finding about production code, never
        # silently absorbed into "all_clean".
        class_a_final_names_before_extra_cleanup = all_container_names()
        class_a_unexpected_delta = sorted(
            n for n in (class_a_final_names_before_extra_cleanup - class_a_baseline_names)
            if n.startswith(CONTAINER_NAME_PREFIX)
        )
        class_a_removed_by_extra_cleanup: dict[str, bool] = {}
        for name in class_a_unexpected_delta:
            run(["docker", "rm", "--force", name], check=False)
            class_a_removed_by_extra_cleanup[name] = container_absent(name)
        class_a_leftover_after_extra_cleanup = [
            n for n, removed in class_a_removed_by_extra_cleanup.items() if not removed
        ]

        cleanup["class_a_baseline_names"] = sorted(class_a_baseline_names)
        cleanup["class_a_final_names_before_extra_cleanup"] = sorted(class_a_final_names_before_extra_cleanup)
        cleanup["class_a_unexpected_delta"] = class_a_unexpected_delta
        cleanup["class_a_removed_by_extra_cleanup"] = class_a_removed_by_extra_cleanup
        cleanup["class_a_leftover_after_extra_cleanup"] = class_a_leftover_after_extra_cleanup
        # Any unexpected Class A delta is itself the finding, whether
        # or not this spike's own best-effort extra cleanup fixed it
        # afterward -- DockerVerifier is supposed to confirm its own
        # removal before ever returning, so observing this at all means
        # that guarantee did not hold for at least one run.
        cleanup["class_a_clean"] = not class_a_unexpected_delta

        write(EVIDENCE_DIR / "cleanup.json", json.dumps(cleanup, indent=2))

    classifications = {key: r.get("classification") for key, r in results.items()}
    any_fail = any(c == FAIL for c in classifications.values())
    any_technical_failure = any(c == TECHNICAL_FAILURE for c in classifications.values())
    any_inconclusive = any(c == INCONCLUSIVE for c in classifications.values())

    if not cleanup["all_clean"] or not cleanup["class_a_clean"]:
        overall = "FAIL"  # a cleanup technical failure overrides a successful scenario claim
    elif any_fail or any_technical_failure:
        overall = "FAIL"
    elif any_inconclusive:
        overall = "PASS_WITH_OPEN_RISKS"
    else:
        overall = "PASS"

    summary = {
        "host": host_info,
        "tested_image": DEFAULT_IMAGE,
        "tested_security_flags": list(_SECURITY_FLAGS),
        "classifications": classifications,
        "cleanup_all_clean": cleanup["all_clean"],
        "class_a_clean": cleanup["class_a_clean"],
        "overall_verdict": overall,
    }
    write(EVIDENCE_DIR / "summary.json", json.dumps(summary, indent=2))
    log("\n\nSUMMARY:\n" + json.dumps(summary, indent=2))

    write(EVIDENCE_DIR / "run.log", "\n".join(_LOG_LINES) + "\n")

    sys.exit(exit_code_for_overall_verdict(overall))


if __name__ == "__main__":
    main()
