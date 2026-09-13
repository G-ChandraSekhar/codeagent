# S4_RESULT.md — executor/container isolation

This spike now has retained evidence on **both** platforms named in
`docs/threat-model.md`'s A9 (Linux and macOS/Docker Desktop are
separate evidence domains):

- **macOS/Docker Desktop** — Darwin 25.6.0 (arm64), Docker Desktop,
  Docker Engine 29.7.2, cgroup v2, Python 3.12.14, run manually on the
  author's machine. Evidence: `spikes/s4/evidence/macos-docker-desktop-arm64/`.
- **Linux/x86_64** — Ubuntu 24.04 (GitHub-hosted Actions runner,
  `6.17.0-1022-azure`), native Docker Engine 28.0.4, cgroup v2, Python
  3.12.14, run via the manual `.github/workflows/s4-linux-evidence.yml`
  workflow. Evidence: `spikes/s4/evidence/linux-x86_64/run-<run-id>/`,
  one directory per workflow run — see "Platform comparison" below for
  why there are two.

Each platform's evidence lives under its own platform-keyed directory
so neither can silently overwrite the other. The body of this document
below (per-check results, memory-limit analysis, etc.) was written
against the macOS run first; the "Platform comparison" section
consolidates what changed and stayed the same on Linux rather than
duplicating every check's narrative twice.

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
question. What the swap value shows is that **the observed Docker
HostConfig on this host sets `MemorySwap=1024m`, permitting this
container additional swap on top of that 512 MiB limit** (this same
observation reproduced on native Linux Docker Engine too — see
"Platform comparison" below — so it is reported as an observation on
the hosts actually tested, not attributed to Docker Desktop
specifically), so the combined memory+swap allocation available to it
is larger than 512 MiB alone. **`--memory-swap=512m` would
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
*combined* memory+swap allowance on either platform tested — the
observed default swap grant roughly doubles it on both macOS/Docker
Desktop and native Linux Docker Engine — and the executor's outcome
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

## Platform comparison: macOS vs Linux

| | macOS/Docker Desktop | Linux/x86_64 (corrected) |
|---|---|---|
| Result | 13 PASS, 1 INCONCLUSIVE | 12 PASS, 2 INCONCLUSIVE |
| Overall verdict | `PASS_WITH_OPEN_RISKS` | `PASS_WITH_OPEN_RISKS` |
| Evidence | `evidence/macos-docker-desktop-arm64/` | `evidence/linux-x86_64/run-34764768179/` |

Both platforms land on the same overall verdict. The one classification
difference is `cap_sys_admin`: `PASS` on macOS, `INCONCLUSIVE` on
Linux (see below). Every other check — network isolation, host-path
isolation, non-root enforcement, no-new-privileges, read-only
filesystem (both general and the workspace bind specifically), the
absent Docker socket, environment non-inheritance, bounded output,
timeout enforcement, PID limit, and CPU limit — is `PASS` on both.
`memory_limit` is `INCONCLUSIVE` on both, for the same swap-headroom
reason (see below).

**Linux required one correction before reaching this result.** The
first Linux workflow run
([34764206614](https://github.com/G-ChandraSekhar/codeagent/actions/runs/34764206614),
commit `8db44a9`) reported `mounts_and_host_isolation: FAIL` and
`overall_verdict: FAIL`. Investigation (retained as
`evidence/linux-x86_64/run-34764206614/`, plus that run's
`RUN_INFO.json`) found this was **a bug in the spike harness's own
test fixture, not a finding about container isolation**:
`tempfile.mkdtemp()` creates directories mode `0700` by default, owned
by whichever host user ran the script. On the GitHub-hosted runner
that user's UID is `1001`; the test container runs as the production
configuration's UID `1000`. A `0700` directory owned by `1001` is not
even traversable by UID `1000`, so the container could never see its
own intended-readable fixture file regardless of the bind mount itself
being correctly configured (`.Mounts`/`.HostConfig.Tmpfs` were already
correct in that first run). Locally on macOS this never manifested,
because `tempfile.mkdtemp()`'s host-side owner UID there happened not
to matter for the check as originally written.

The fix (commit `a557cf2`) chmods only this one intended-readable
fixture directory/file to `0755`/`0644` — not scratch directories in
general, and not the deliberately-restrictive secret-canary directory
used by other checks — and replaces the single combined assertion with
a structured probe reporting four independent fields (`expected_readable`,
`expected_content_matches`, `host_secret_absent_at_root`,
`host_secret_absent_at_host_path`), plus a `classify_mounts_check()`
function (with its own unit tests) that keeps a real security finding
(bad mount config, a visible host canary) classified `FAIL`, while an
inaccessible intended fixture, malformed probe output, or a
`docker exec` infrastructure failure is classified `TECHNICAL_FAILURE`
— never conflated with a security `FAIL`. The corrected run
([34764768179](https://github.com/G-ChandraSekhar/codeagent/actions/runs/34764768179),
commit `a557cf2`) shows `mounts_and_host_isolation: PASS` with all four
probe fields `true`, and is retained as the current, authoritative
Linux evidence.

**`cap_sys_admin` is genuinely `INCONCLUSIVE` on Linux, for a reason
this evidence does not fully identify.** The primary observation
(`mount(2)` blocked under root + `--cap-drop ALL`) is `PASS` on both
platforms. The negative control (`--cap-add SYS_ADMIN`, otherwise
identical) succeeded on macOS but failed on Linux — and it failed
differently: macOS's failure mode was `EPERM` (errno 1, "no
capability"); Linux's was `EACCES` (errno 13, "Permission denied").
That difference in errno is consistent with a runtime restriction
layered on top of capabilities (a seccomp filter or an AppArmor policy
blocking the `mount` syscall outright) — but **this evidence does not
prove which mechanism, or confirm one is active at all**; `errno 13`
alone does not identify its cause, and no further probing of the
runner's seccomp/AppArmor configuration was performed. The harness
correctly refused to weaken AppArmor, seccomp, or any other runner
security setting to force the control to pass, and classified this
`INCONCLUSIVE` rather than guessing. This remains a genuinely open
question, not a resolved platform difference.

**Memory/swap and the OOM disposition gap reproduced identically on
Linux**, using the same 600 MB / 1900 MB two-experiment design: 600 MB
survived (`MemorySwap=1073741824`, `Memory=536870912` — the same
values as macOS), a separate 1900 MB hand-controlled container was
directly confirmed OOM-killed, and the same `DockerVerifier`
disposition gap was inferred. Confirming this on a second,
architecturally different Docker installation (native Linux Engine vs.
Docker Desktop's Linux VM) is why the finding is now described as "the
observed Docker HostConfig on this host" rather than attributed to
"Docker Desktop" specifically — it is not generalized further than
that into a claim about every Docker Engine installation, since only
two hosts have actually been observed.

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
`codeagent.executor`, never modifies it) and `test_spike_s4.py` (28
focused tests, all passing on both platforms, run separately from the
main suite). Per-platform evidence bundles (`host.json`,
`tested_config.json`, `check_*.json` per check, `cleanup.json`,
`summary.json`, `run.log`, the complete command transcript generated
by the script itself):

- `spikes/s4/evidence/macos-docker-desktop-arm64/` — the manual macOS
  run described throughout this document.
- `spikes/s4/evidence/linux-x86_64/run-34764206614/` — the first
  (failed, harness-bug) Linux attempt, retained unmodified with its
  own `RUN_INFO.json` recording the workflow URL and source commit.
- `spikes/s4/evidence/linux-x86_64/run-34764768179/` — the corrected,
  authoritative Linux run, likewise with its own `RUN_INFO.json`.

Host metadata (`docker_version`, `cgroup_version`) is queried with
`check=True` (the harness default) — a failed query raises immediately
rather than being silently recorded as an empty string, on both
platforms.

## Overall verdict

**`PASS_WITH_OPEN_RISKS` on both platforms** (`summary.json` in each
evidence directory): macOS is 13 PASS + 1 INCONCLUSIVE (`memory_limit`);
the corrected Linux run is 12 PASS + 2 INCONCLUSIVE (`memory_limit`,
`cap_sys_admin`). `memory_limit`'s three separately reported
observations (a real finding about swap headroom beyond the nominal
`--memory` limit; a directly confirmed OOM kill on a larger allocation;
a strongly-inferred-but-not-directly-proven disposition gap in
`DockerVerifier`) and Linux's additional `cap_sys_admin` finding (a
negative control blocked by an unidentified runtime restriction) are
carried forward as open risks rather than silently dropped or
overclaimed. Cleanup was fully confirmed clean for both Class A and
Class B resources on both platforms, so it does not override either
verdict. No src/codeagent, production tests, production security
flags, or ADR files were modified in gathering this evidence.

---

## Post-hardening follow-up (macOS only) — commit `00063d445d9c25f957c0c6474701fb61d1217f67`

**This section is a distinct, later run, not part of the original S4
experiment above.** Everything before this heading describes the
original spike as it was run and evaluated at the time; it is
unmodified. This section records a narrower follow-up, run with a
separate driver (`spike_s4_m3_followup.py`, not `spike_s4.py`), against
the production code *after* the Milestone 3 hardening commit
(`security: enforce Docker memory ceiling and classify OOM`,
`00063d445d9c25f957c0c6474701fb61d1217f67`) that was made in direct
response to this spike's own `memory_limit` finding above. It validates
the fix, it does not redo the original 14-check sweep.

**Scope**: macOS/Docker Desktop only in this pass (per this document's
own A9 platform-separation rule). A Linux repetition of this follow-up
is separate, later work and has not been done.

**What changed in production and what this follow-up checked**:
1. `_SECURITY_FLAGS` gained `--memory-swap 512m` alongside the existing
   `--memory 512m`.
2. `DockerVerifier._attempt` now classifies a Docker-confirmed OOM kill
   as `ENVIRONMENT_FAILURE` / `ErrorCode.EXECUTOR_OOM_KILLED`, ahead of
   the ordinary nonzero-exit → `TEST_FAILURE` rule.

**Evidence**: `spikes/s4/evidence/macos-docker-desktop-arm64/run-m3-followup-20260913T160240Z-523c01b6/`
(a new run-specific subdirectory of the existing macOS platform
directory — the original flat evidence files one level up are
untouched). Host: Darwin 25.6.0 (arm64), Docker Desktop, Docker Engine
29.7.2, cgroup v2, Python 3.12.14. `RUN_INFO.json` confirms the actual
git `HEAD` at run time matched the expected commit above.

**Check 1 — `memory_swap_configuration`: PASS.** Two independent
direct observations, not one fused into the other:
  - A hand-controlled Class B twin container, built from the real
    imported `_SECURITY_FLAGS` (the same `spike_s4._memory_experiment`
    machinery the original run used), directly inspected before
    removal: `HostConfig.Memory=536870912`, `HostConfig.MemorySwap=
    536870912` (both exactly 512 MiB — no additional swap headroom).
  - The REAL `DockerVerifier`'s own container (not a twin): observed
    via an evidence-only interception kept entirely inside
    `spike_s4_m3_followup.py` — it temporarily wraps
    `codeagent.executor._run_docker` (the same module attribute the
    project's own unit tests patch) so that, only on the exact `rm
    --force <name>` call `DockerVerifier._cleanup` was already about
    to make, a read-only `docker inspect` runs first and the result is
    recorded before the real removal proceeds unmodified. This is
    direct observation of the actual production code path's own
    container, distinct from the twin above (which uses the same
    flags but not the same code path). Result: `HostConfig.Memory=
    536870912`, `HostConfig.MemorySwap=536870912` — identical to the
    twin. This closes the exact gap the original run found above
    (`Memory=536870912`, `MemorySwap=1073741824`).

**Check 2 — `oom_classification`: PASS.** A real 1900 MB allocation
(the same fixed script the original spike used) run through the
genuine public `DockerVerifier.run()` API: `outcome=environment_
failure`, `error.code=executor_oom_killed`, `exit_code=137` (preserved,
not nulled), `error.message="verification container was killed for
exceeding its memory limit"` (the fixed literal defined in
`executor.py`, confirmed both by exact match and by a mechanical
sanitization check — no raw `OOMKilled`/`HostConfig`/JSON payload
fragments). Not classified as `test_failure`. This directly observes,
for the first time, what the original run could only infer from a
separate confirmed-OOM twin plus a same-flags `DockerVerifier` run that
happened to also exit 137.

**Check 3 — `negative_control_ordinary_test_failure`: PASS.** An
ordinary `sys.exit(1)` command through the same real `DockerVerifier`
API: `outcome=test_failure`, `exit_code=1`, `error=None` — proving the
OOM classification above is a real, narrow distinction and not simply
"every nonzero exit is now an operational error."

**Cleanup**: fully confirmed clean, distinguishing the two resource
classes exactly as the original spike did — Class A (three real
`DockerVerifier` containers: the HostConfig-observation run, the OOM
run, and the negative control) confirmed via an empty baseline/final
container-name-prefix delta; Class B (the one hand-controlled twin
container plus its four scratch directories) confirmed via
`spike_s4.Manifest.cleanup_and_verify()`'s independent
re-verification. Both `all_clean: true`. A failed listing is never
treated as confirmed-clean here: `all_container_names()` is reused
unmodified from `spike_s4.py`, which raises on a nonzero `docker ps -a`
rather than returning an empty set.

**Follow-up verdict**: **`PASS`** — all three checks PASS, cleanup
fully confirmed, no `INCONCLUSIVE`/`FAIL`/`TECHNICAL_FAILURE` observed.

**Limitations, stated plainly**:
- macOS/Docker Desktop only. Linux is not yet re-validated against the
  hardened configuration in this pass.
- This does not re-run or reopen the original 14-check S4 sweep;
  `cap_sys_admin`'s Linux `INCONCLUSIVE` finding (unrelated to memory/
  OOM) is untouched and remains open.
- The `HostConfig.Memory`/`MemorySwap` interception observes exactly
  one `DockerVerifier` container per run by construction (the harness
  asserts this and fails loudly otherwise); it has not been exercised
  against concurrent `DockerVerifier` instances.
- `message_matches_expected_literal` pins today's exact wording of the
  fixed OOM message as a convenience signal for noticing future
  wording drift; the property this follow-up actually depends on is
  sanitization (checked independently), not the exact string.
