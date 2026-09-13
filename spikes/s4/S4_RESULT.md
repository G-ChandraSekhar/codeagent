# S4_RESULT.md — executor/container isolation

Host: Darwin 25.6.0 (arm64), Docker Desktop, Docker Engine 29.7.2,
cgroup v2, Python 3.12.14. Retained evidence for this run lives under
`spikes/s4/evidence/macos-docker-desktop-arm64/` — a platform-keyed
directory, so a later Linux run is written to its own
`spikes/s4/evidence/linux-<arch>/` directory and can never overwrite
this platform's evidence.

**Platform scope: macOS/Docker Desktop only.** Per `docs/threat-model.md`'s
A9, Linux and macOS/Docker Desktop are separate evidence domains.
Nothing in this document is a claim about native Linux behavior —
Docker Desktop runs containers inside a Linux VM whose networking,
filesystem, and resource-limit behavior can differ from bare-metal
Linux (this run's own CPU-limit and memory-swap findings are examples
of platform-specific behavior worth re-checking on real Linux). A
Linux repetition is future work, not performed in this pass.

## What was tested

The **actual production configuration** — `codeagent.executor.DEFAULT_IMAGE`
and `_SECURITY_FLAGS`, imported directly (`tested_config.json`,
`summary.json`), never an independently retyped "equivalent" tuple:

```
python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

--network none --read-only --tmpfs /tmp:rw,size=64m --memory 512m
--cpus 1 --pids-limit 128 --user 1000:1000 --cap-drop ALL
--security-opt no-new-privileges
```

Two container classes, tracked two different ways:
**Class A** — real `codeagent.executor.DockerVerifier` instances, used
wherever the exact unmodified flags applied (bounded output, timeout,
docker socket, PID limit, the CPU-limited run, and both memory
observations). `DockerVerifier` names and removes these itself; this
spike does not add them to its own resource manifest. Instead, a
container-name snapshot is taken before any check runs and compared
against the final snapshot — any name present only at the end and
starting with the imported `CONTAINER_NAME_PREFIX` would be an
unexpected leftover (see "Cleanup evidence" below). **Class B** —
hand-rolled `docker create`/`run`, used only for negative controls
(one flag added/removed from the imported tuple, documented per case)
and where a container needed inspection before removal (`DockerVerifier`
always removes its own container before returning). Every Class B
resource is tracked by exact name/tag in this spike's own `Manifest`
and independently re-verified absent at the end.

A small custom throwaway image (built fresh each run, no network/apt
step — only `cp`/`chown`/`chmod`/`mkdir` against the pinned base's own
files) backs three checks: a root-owned mode-0600 `/canary-secret`
(non-root), a world-writable `/canary-writable` directory (read-only
FS), and a setuid-root copy of the base image's own `python3`
(no-new-privileges).

## Per-check results

| Check | Primary | Control | Classification |
|---|---|---|---|
| Network blocked | PASS — real connect to a peer-server container's IP failed (`OSError(101, 'Network is unreachable')`) within the 5s bound | PASS — an identical container on the same custom bridge network connected successfully | **PASS** |
| No unintended host mounts | PASS — `.Mounts` showed exactly one read-only bind; `.HostConfig.Tmpfs` showed exactly `/tmp`; a host canary file at an unmounted path was confirmed absent inside the running container | — | **PASS** |
| Non-root | PASS — uid 1000 reading the root-owned 0600 canary raised `PermissionError` | PASS — the identical container as root (`--user 0:0`) read it successfully | **PASS** |
| CAP_SYS_ADMIN unavailable | PASS — a real `mount(2)` syscall (via `ctypes`, not an external `mount` binary) failed `EPERM` under root + `--cap-drop ALL` | PASS — the identical call succeeded once `--cap-add SYS_ADMIN` was added | **PASS** |
| no-new-privileges | PASS — executing the setuid-root `python3` copy under the real flags left `euid=1000` (no escalation) | PASS — removing only `--security-opt no-new-privileges` let it escalate to `euid=0` | **PASS** |
| Read-only root FS | PASS — writing to the world-writable `/canary-writable` failed (`OSError: [Errno 30] Read-only file system`) | PASS — removing only `--read-only` let the identical write succeed | **PASS** |
| Workspace bind mount read-only | PASS — a write to a world-writable host canary bind-mounted `readonly` failed; `/tmp` accepted a write under the same real flags; the host canary was confirmed byte-unchanged | PASS — the identical bind without `readonly` accepted the write (host canary confirmed mutated) | **PASS** |
| Docker socket absent | PASS — `/var/run/docker.sock` and `/run/docker.sock` both absent | — | **PASS** |
| Host environment non-inheritance + explicit passing | PASS — a host-only canary set on the launching process was absent inside; an explicitly `--env`-passed canary was present; full baseline `env` recorded, not asserted against a brittle exact list | — | **PASS** |
| Bounded stdout/stderr | PASS — ~10 MB written independently to each stream via the real `DockerVerifier`; both capped at exactly `_MAX_STREAM_BYTES` + the real `_BoundedCollector`-derived truncation marker length (65,551 bytes each), both ending in that exact marker | — | **PASS** |
| Timeout enforcement | PASS — `sleep 30` under `timeout_seconds=3`; the new container was identified by set difference against a pre-start snapshot (never an arbitrary prefix match), observed genuinely `running` mid-wait, `outcome == TIMEOUT` at ~3.14s (not 30s), confirmed exact-name-absent afterward | — | **PASS** |
| PID limit | PASS — a fixed, linear fork loop (never recursive) up to 140 attempts; 127 succeeded, the 128th failed (not asserted as a specific required index); sampled `pids.current == 128 == pids.max` at the moment of failure — never exceeded | — | **PASS** |
| CPU limit | PASS — 4 busy-loop workers for 4s under real `--cpus 1`, measured via cgroup `cpu.stat` `usage_usec` delta (not a single `docker stats` sample): **1.013 core-equivalents** | PASS — the identical 4-worker run without `--cpus` measured **3.985 core-equivalents**, confirming the host/VM has real spare cores to demonstrate throttling against | **PASS** |
| Memory limit | **INCONCLUSIVE** — see below | n/a | **INCONCLUSIVE** |

**Note on naming**: the environment check above was originally called
"env allowlisting" during design; it is renamed here to **"host
environment non-inheritance and explicit environment passing"**
because that is precisely what it validates — Docker's own mechanism
and its current deny-by-default behavior (a container never silently
inherits the launching process's environment; an explicitly
`--env`-passed variable does appear). It is **not** a claim that
CodeAgent implements an environment-allowlist API: `DockerVerifier`
today passes zero `--env` flags of its own. This check demonstrates
the raw material an allowlist could be built from, not an
already-built allowlist.

## Memory limit: three separate observations, not one fused claim

**Observation 1 — the primary, fixed 600 MB allocation** (hand-controlled
lifecycle, `State.OOMKilled`/`State.ExitCode` inspected directly before
removal): did **not** trigger an OOM kill (`State.OOMKilled: false`,
`State.ExitCode: 0`, logged `ALLOCATION_SURVIVED 600`). Its own
inspected `HostConfig` explains why: `Memory=536870912` (512 MiB) but
`MemorySwap=1073741824` (1024 MiB). **`--memory=512m` does limit this
container's own memory usage to 512 MiB** — that flag is not in
question. What the swap value shows is that **Docker Desktop's default
`MemorySwap=1024m` permits this container additional swap on top of
that 512 MiB limit**, so the combined memory+swap allocation available
to it is larger than 512 MiB alone. **`--memory-swap=512m` would
prevent that additional swap and cap the combined allowance at
512 MiB** — that flag is simply absent from the current production
configuration. This is recorded as a finding, not fixed:
`src/codeagent` was not modified during this spike, per its explicit
scope.

**Observation 2 — a second, separate hand-controlled container**, an
identical lifecycle with a larger fixed (not unbounded) 1900 MB
target, specifically to directly confirm what a genuine OOM kill looks
like on this platform: **`State.OOMKilled: true`, `State.ExitCode: 137`** —
inspected for this exact container, before its own removal. This is
direct, confirmed evidence, not an inference.

**Observation 3 — a third, separate container**: the identical 1900 MB
allocation run through the real `DockerVerifier`, which always removes
its own container before returning, so `State.OOMKilled` could not be
inspected for this specific instance. What `DockerVerifier` itself
directly establishes for this container: `exit_code=137`,
`outcome=TEST_FAILURE`.

**The connection between observations 2 and 3 is an inference, stated
explicitly as such, never fused into a single "OOM confirmed" claim**:
both containers ran the identical image, flags, and 1900 MB allocation
script; observation 2's twin was directly confirmed OOM-killed at exit
137; observation 3 also exited 137 and was classified `TEST_FAILURE`.
This is a **strongly supported inference** that observation 3's
container was also OOM-killed — not a directly proven fact about that
specific container, since its `State.OOMKilled` was never observable.
**If the inference holds**, it reveals a genuine gap in
`src/codeagent/executor.py`'s current disposition logic: `DockerVerifier
._attempt` classifies any genuinely-exited nonzero exit code as
`TEST_FAILURE` (a domain outcome — "the repository's tests failed"),
with no way to distinguish that from a container being killed by the
kernel for exceeding its memory limit (an operational/environment
outcome). This is recorded here as a discovered gap, not fixed —
`src/codeagent` was not modified during this spike, per its explicit
scope. Machine-readable evidence for all three observations is in
`check_memory_limit.json`'s `primary`, `large_allocation_hand_controlled`,
and `production_disposition_check` fields respectively.

## Is standard Docker sufficient for v1, on this platform?

**For everything except explicit memory/swap hardness and the inferred
OOM/TEST_FAILURE disposition gap: yes**, on the evidence gathered here.
Thirteen of fourteen checks passed with a validated negative control
where one applied — network isolation, host-path isolation, non-root
enforcement, capability dropping (verified via a real privileged
syscall, not configuration inspection), no-new-privileges (verified
via a real setuid escalation attempt), read-only root filesystem (both
generally and for the specific workspace bind mount), the absent
Docker socket, environment non-inheritance, bounded output, forceful
timeout termination (with the container's own live "still running"
state observed mid-wait, not assumed), and PID-limit enforcement
(cgroup-verified, never exceeding the configured value) all held under
real, behavioral, adversarial-shaped tests — not merely by reading
`docker inspect`'s configuration back.

**Memory is the one open risk**: `--memory` alone does not cap the
*combined* memory+swap allowance on this platform — Docker Desktop's
default swap grant roughly doubles it — and the executor's outcome
classification cannot currently, even by strong inference, be *proven*
to tell a real OOM kill apart from an ordinary failing test (though the
evidence strongly suggests it cannot). Both are real, evidenced gaps
that should be weighed before treating the current flag set as
sufficient for v1's memory-exhaustion threat (T-J5 in the threat
model).

## CAP_SYS_ADMIN and no-new-privileges: how close to "validated" is this?

Both checks' negative controls **succeeded** on this platform (the
`--cap-add SYS_ADMIN` control genuinely enabled the `mount(2)` syscall;
removing `no-new-privileges` genuinely let the setuid binary escalate)
— so neither needed the `INCONCLUSIVE` fallback the design anticipated
for a platform where seccomp/AppArmor/a `nosuid` mount might block the
control regardless of the flag under test. That fallback path exists
in the harness (and is exercised correctly by the **CPU** check's own
INCONCLUSIVE-condition logic, not triggered this run since the control
did show excess cores), but was never actually needed for
CAP_SYS_ADMIN or no-new-privileges here — both got clean PASS/PASS
results. Still only one platform's evidence, per the A9 scope note
above.

## Cleanup evidence

**Class B** (hand-rolled containers/networks/images/scratch
directories): every one this spike created is tracked by exact
name/tag in this spike's own `Manifest` and independently re-verified
absent via the *unfiltered* listing commands (`docker ps -a --format
'{{.Names}}'`, `docker network ls --format '{{.Name}}'`, `docker
images --format '{{.Repository}}:{{.Tag}}'`, split into lines and
checked by exact string equality — never Docker's own substring/regex
`--filter`). Result: `"all_clean": true` (`cleanup.json`).

**Class A** (real `DockerVerifier` instances, which name and remove
themselves — never added to the manifest above): a container-name
snapshot taken before any check ran (`class_a_baseline_names`, empty
on this run) is compared against the snapshot taken after all checks
completed and Class B cleanup ran (`class_a_final_names_before_extra_cleanup`,
also empty). Any name present only in the second snapshot and starting
with the imported `CONTAINER_NAME_PREFIX` would be recorded as
`class_a_unexpected_delta` and would force the overall verdict to
`FAIL`, regardless of best-effort extra cleanup afterward — because
observing one at all means `DockerVerifier`'s own "confirm removal
before returning" guarantee did not hold for that run. On this run:
`"class_a_unexpected_delta": []`, `"class_a_clean": true`.

`docker ps -a` on the host was confirmed empty both before and after
the full run. Nothing outside what this spike itself created was ever
deleted.

## Retained evidence

`spike_s4.py` (self-contained driver, imports only from
`codeagent.executor`, never modifies it), `test_spike_s4.py` (15
focused tests, all passing, run separately from the main suite), and
`spikes/s4/evidence/macos-docker-desktop-arm64/`: `host.json`,
`tested_config.json`, `check_*.json` (one per check), `cleanup.json`,
`summary.json`, `run.log` (complete command transcript generated by
the script itself). Host metadata (`docker_version`, `cgroup_version`)
is queried with `check=True` (the harness default) — a failed query
raises immediately rather than being silently recorded as an empty
string.

## Overall verdict

`PASS_WITH_OPEN_RISKS` (`summary.json`): every mandatory check passed
except memory, which is `INCONCLUSIVE` with three separately reported
observations (a real finding about swap headroom beyond the nominal
`--memory` limit; a directly confirmed OOM kill on a larger allocation;
a strongly-inferred-but-not-directly-proven disposition gap in
`DockerVerifier`) carried forward as open risks rather than silently
dropped or overclaimed. Cleanup was fully confirmed clean for both
Class A and Class B resources, so it does not override this verdict.
No src/codeagent, tests/, CI, or ADR files were modified. No Linux
evidence is claimed.
