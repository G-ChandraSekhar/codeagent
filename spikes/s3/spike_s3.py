"""
CodeAgent Stage 2 -- S3 spike: multi-file patch atomicity.

Evidence-gathering only. Does NOT import or exercise anything under
src/codeagent (the real GitPatchApplier is deliberately single-
operation only, per Milestone 1 slice B's scope, and cannot express a
multi-file proposal at all) -- this script contains its own throwaway,
minimal multi-op "apply" implementation built only to answer S3's
question. Nothing here is production code and nothing here is reused
by import; a future Milestone 2 patch engine may reuse the *algorithm*
(refactored), never this module.

Runs three independent experiments, each against its own fresh,
disposable scratch Git repository + worktree created under a unique
`tempfile.mkdtemp(prefix="codeagent-spike-s3-")` root -- never against
the CodeAgent checkout, and never a fixed, recursively-deleted /tmp
path shared across runs:

  1. Prevalidation atomicity: an invalid 3-op proposal must be
     rejected before any file is touched.
  2. Handled mid-application failure + explicit rollback: a valid
     3-op proposal is interrupted by a caught exception after op 1's
     write lands; an explicit rollback strategy (git checkout) is
     evaluated for whether it actually restores pre-apply state.
  3. Interruption (SIGKILL) detection: the same proposal is applied in
     a child process that is SIGKILLed at a deterministic barrier
     right after op 1's write completes; the resulting worktree state
     is classified into one of four buckets. This is NOT a crash-
     consistency guarantee (see S3_RESULT.md) -- it tests only whether
     an interrupted apply is left in a *detectable* state, not whether
     the data itself survives correctly.

Infrastructure subprocess calls (git, mostly) fail loudly by default
(`run(..., check=True)`, the default): a nonzero exit raises
immediately rather than being silently treated as empty/valid output.
The one deliberate exception is experiment 2's rollback command itself
-- whether *that* succeeds or fails is the thing being measured, so it
is called with `check=False` and its outcome is recorded, not asserted.

The script writes the complete retained evidence bundle directly into
this same directory (spikes/s3/) as it runs -- host.json, run.log,
baseline-worktrees.txt, final-worktrees.txt, result1.json,
result2.json, result3.json, summary.json. A clean rerun regenerates
all of them; nothing needs to be copied from /tmp by hand. The unique
scratch root is removed unconditionally at the end, and
`summary.json["scratch_root_removed"]` records whether that removal
was verified to have actually happened.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()

FILES = ("a.py", "b.py", "c.py")
ORIGINAL_CONTENT = {
    "a.py": "def a():\n    return 'A_ORIGINAL'\n",
    "b.py": "def b():\n    return 'B_ORIGINAL'\n",
    "c.py": "def c():\n    return 'C_ORIGINAL'\n",
}

_LOG_LINES: list[str] = []


def log(text: str = "") -> None:
    """Prints AND retains every line, so spikes/s3/run.log is a
    complete, self-generated transcript -- no manual copying needed."""
    print(text)
    _LOG_LINES.append(text)


def run(argv: list[str], *, check: bool = True, **kw) -> subprocess.CompletedProcess:
    """Structured argv only -- never shell=True, never an interpolated
    string. Fails loudly by default: a nonzero exit raises
    CalledProcessError immediately rather than being silently returned
    as (possibly empty) stdout that a caller might mistake for real
    evidence. Pass check=False only where a nonzero exit is itself the
    thing an experiment is measuring."""
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


@dataclass(frozen=True)
class PatchOp:
    relative_path: str
    expected_text: str
    new_text: str


class ValidationError(Exception):
    def __init__(self, op: PatchOp, reason: str) -> None:
        super().__init__(f"{op.relative_path}: {reason}")
        self.op = op
        self.reason = reason


def validate_op(worktree: Path, op: PatchOp) -> None:
    """Raises ValidationError if op cannot be safely applied. Read-only
    -- never writes anything."""
    target = worktree / op.relative_path
    if not target.is_file():
        raise ValidationError(op, "target file does not exist")
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(op.expected_text)
    if occurrences == 0:
        raise ValidationError(op, "expected_text not found")
    if occurrences > 1:
        raise ValidationError(op, f"expected_text matches {occurrences} times, ambiguous")


def validate_all(worktree: Path, ops: list[PatchOp]) -> list[dict]:
    """Validates every op (does not stop at the first failure, so a
    single run reports everything wrong with a proposal). Returns a
    list of {relative_path, reason} dicts -- empty if every op is
    valid. Never writes anything regardless of outcome."""
    errors = []
    for op in ops:
        try:
            validate_op(worktree, op)
        except ValidationError as exc:
            errors.append({"relative_path": op.relative_path, "reason": exc.reason})
    return errors


def apply_one(worktree: Path, op: PatchOp) -> None:
    """Applies exactly one op: read, replace-once, write. Deliberately
    the naive non-atomic approach (no write-to-scratch-then-rename) --
    that is exactly the thing experiment 3 is checking the consequences
    of."""
    target = worktree / op.relative_path
    text = target.read_text(encoding="utf-8")
    new_text = text.replace(op.expected_text, op.new_text, 1)
    with target.open("w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_info(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    data = path.read_bytes()
    return {"exists": True, "size": len(data), "sha256": sha256_bytes(data)}


def git_head(worktree: Path) -> str:
    # check=True (the default): a failing `git rev-parse` raises rather
    # than silently handing back "" -- an empty HEAD must never be
    # read as "matches another empty HEAD" evidence of anything.
    return run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()


def git_status(worktree: Path) -> str:
    return run(["git", "-C", str(worktree), "status", "--porcelain=v1"]).stdout


def manifest(worktree: Path) -> dict:
    return {
        "head": git_head(worktree),
        "status_porcelain": git_status(worktree),
        "files": {name: file_info(worktree / name) for name in FILES},
    }


def init_scratch_repo(repo_dir: Path) -> Path:
    repo_dir.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=repo_dir)
    run(["git", "config", "user.email", "spike@example.com"], cwd=repo_dir)
    run(["git", "config", "user.name", "Spike"], cwd=repo_dir)
    for name, content in ORIGINAL_CONTENT.items():
        (repo_dir / name).write_text(content)
    run(["git", "add", "-A"], cwd=repo_dir)
    run(["git", "commit", "-q", "-m", "scratch fixture"], cwd=repo_dir)
    return repo_dir


def make_worktree(scratch_root: Path, repo_dir: Path, run_id: str) -> Path:
    worktree_path = scratch_root / f"worktree-{run_id}"
    run(["git", "-C", str(repo_dir), "worktree", "add", str(worktree_path), "HEAD"])
    return worktree_path


def remove_worktree(repo_dir: Path, worktree_path: Path) -> bool:
    r = run(["git", "-C", str(repo_dir), "worktree", "remove", str(worktree_path), "--force"])
    return r.returncode == 0


def valid_ops() -> list[PatchOp]:
    return [
        PatchOp("a.py", "A_ORIGINAL", "A_PATCHED"),
        PatchOp("b.py", "B_ORIGINAL", "B_PATCHED"),
        PatchOp("c.py", "C_ORIGINAL", "C_PATCHED"),
    ]


# --------------------------------------------------------------------
# Experiment 1: prevalidation atomicity
# --------------------------------------------------------------------

def experiment_1_prevalidation(scratch_root: Path, repo_dir: Path) -> dict:
    run_id = f"s3-exp1-{uuid.uuid4().hex[:8]}"
    worktree = make_worktree(scratch_root, repo_dir, run_id)
    try:
        ops = valid_ops()
        # op 3 (c.py) made invalid in a validation-detectable way:
        # expected_text simply is not present in the file at all.
        ops[2] = PatchOp("c.py", "TEXT_NOT_PRESENT_ANYWHERE", "C_PATCHED")

        manifest_before = manifest(worktree)
        errors = validate_all(worktree, ops)

        # The gate under test: apply only if validation reported zero
        # errors. Since op 3 is invalid, nothing is applied at all.
        applied_anything = False
        if not errors:
            for op in ops:
                apply_one(worktree, op)
            applied_anything = True

        manifest_after = manifest(worktree)
        unchanged = manifest_before == manifest_after

        result = {
            "run_id": run_id,
            "errors": errors,
            "rejected_before_any_write": (len(errors) > 0 and not applied_anything),
            "manifest_before": manifest_before,
            "manifest_after": manifest_after,
            "worktree_byte_for_byte_unchanged": unchanged,
        }
    finally:
        removed = remove_worktree(repo_dir, worktree)
        result["worktree_removed_cleanly"] = removed
    return result


# --------------------------------------------------------------------
# Experiment 2: handled mid-application failure + rollback
# --------------------------------------------------------------------

class InjectedFailure(Exception):
    """Stands in for a real, caught mid-application failure (e.g. a
    disk-full error, a second validation surprise discovered only at
    write time). Deterministic and intentional -- not a bug."""


def experiment_2_handled_failure_rollback(scratch_root: Path, repo_dir: Path) -> dict:
    run_id = f"s3-exp2-{uuid.uuid4().hex[:8]}"
    worktree = make_worktree(scratch_root, repo_dir, run_id)
    try:
        ops = valid_ops()  # all three genuinely valid this time

        manifest_before = manifest(worktree)

        # Apply op 1, then deterministically fail before op 2 -- this
        # is the exact "operation 1 written, operation 2 not started"
        # window the spike is required to inspect.
        apply_one(worktree, ops[0])
        mid_state_manifest = manifest(worktree)  # captured BEFORE any rollback attempt
        caught_exception = None
        try:
            raise InjectedFailure("deterministic injected failure after op 1, before op 2")
        except InjectedFailure as exc:
            caught_exception = str(exc)

        # Explicit rollback strategy under evaluation: restore every
        # file touched so far from the worktree's own HEAD via
        # `git checkout -- <path>`. Only op 1's target (a.py) was
        # touched, so only it needs restoring. check=False here is the
        # ONE deliberate exception to "fail loudly by default": whether
        # this command succeeds or fails is exactly what this
        # experiment measures, so it must not raise on our behalf.
        rollback_result = run(
            ["git", "-C", str(worktree), "checkout", "--", ops[0].relative_path],
            check=False,
        )
        rollback_command_succeeded = (rollback_result.returncode == 0)

        manifest_after_rollback = manifest(worktree)
        fully_restored = manifest_after_rollback == manifest_before

        result = {
            "run_id": run_id,
            "caught_exception": caught_exception,
            "manifest_before": manifest_before,
            "mid_state_manifest_before_rollback": mid_state_manifest,
            "intermediate_state_was_inconsistent": (mid_state_manifest != manifest_before),
            "rollback_command_succeeded": rollback_command_succeeded,
            "manifest_after_rollback": manifest_after_rollback,
            "fully_restored_to_pre_apply_state": fully_restored,
        }
    finally:
        removed = remove_worktree(repo_dir, worktree)
        result["worktree_removed_cleanly"] = removed
    return result


# --------------------------------------------------------------------
# Experiment 3: interruption (SIGKILL) detection -- NOT crash
# consistency. See S3_RESULT.md for the distinction.
# --------------------------------------------------------------------

def _crash_child_main(worktree_str: str, marker_str: str, ops_json_str: str) -> None:
    """Runs INSIDE the child process only (see __main__ dispatch below).
    Applies op 1, fsyncs it, writes a marker file (also fsynced) to
    signal the parent that the barrier has been reached, then spins
    forever with no further work and no cleanup handler -- there is
    nothing registered to run on SIGKILL, deliberately, since a real
    SIGKILL cannot run Python finally/atexit/signal handlers at all.
    The parent is solely responsible for killing this process; it does
    not exit on its own."""
    worktree = Path(worktree_str)
    marker = Path(marker_str)
    ops = [PatchOp(**d) for d in json.loads(Path(ops_json_str).read_text())]

    apply_one(worktree, ops[0])  # op 1 only -- op 2/3 deliberately never start

    with marker.open("w") as f:
        f.write("ready")
        f.flush()
        os.fsync(f.fileno())

    while True:
        time.sleep(0.05)


def experiment_3_interruption_detection(scratch_root: Path, repo_dir: Path) -> dict:
    run_id = f"s3-exp3-{uuid.uuid4().hex[:8]}"
    worktree = make_worktree(scratch_root, repo_dir, run_id)
    marker = scratch_root / f"barrier-{run_id}.marker"
    ops_json = scratch_root / f"ops-{run_id}.json"
    marker.unlink(missing_ok=True)

    try:
        ops = valid_ops()
        write(ops_json, json.dumps([asdict(op) for op in ops]))

        manifest_before = manifest(worktree)

        child = subprocess.Popen(
            [sys.executable, str(THIS_FILE), "--crash-child", str(worktree), str(marker), str(ops_json)]
        )

        barrier_reached = False
        deadline = time.time() + 15
        while time.time() < deadline:
            if marker.exists():
                barrier_reached = True
                break
            time.sleep(0.01)

        if not barrier_reached:
            # Infra failure, not a finding about CodeAgent -- record it
            # honestly and stop rather than fabricate a classification.
            child.kill()
            child.wait(timeout=5)
            result = {
                "run_id": run_id,
                "barrier_reached": False,
                "note": "child never signaled readiness within the timeout; "
                        "no interruption-detection evidence was produced this run",
            }
            return result

        # Child is now guaranteed spinning at the barrier -- op 1's
        # write is complete and fsynced, op 2 has not started.
        pid = child.pid
        os.kill(pid, signal.SIGKILL)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child_returncode = child.returncode

        manifest_after_kill = manifest(worktree)

        a_applied = manifest_after_kill["files"]["a.py"]["sha256"] == sha256_bytes(
            ORIGINAL_CONTENT["a.py"].replace("A_ORIGINAL", "A_PATCHED").encode("utf-8")
        )
        b_applied = manifest_after_kill["files"]["b.py"]["sha256"] == sha256_bytes(
            ORIGINAL_CONTENT["b.py"].replace("B_ORIGINAL", "B_PATCHED").encode("utf-8")
        )
        c_applied = manifest_after_kill["files"]["c.py"]["sha256"] == sha256_bytes(
            ORIGINAL_CONTENT["c.py"].replace("C_ORIGINAL", "C_PATCHED").encode("utf-8")
        )
        b_untouched = manifest_after_kill["files"]["b.py"] == manifest_before["files"]["b.py"]
        c_untouched = manifest_after_kill["files"]["c.py"] == manifest_before["files"]["c.py"]
        head_unchanged = manifest_after_kill["head"] == manifest_before["head"]
        status_dirty = manifest_after_kill["status_porcelain"].strip() != ""

        fully_original = (
            not a_applied and b_untouched and c_untouched
            and not status_dirty and head_unchanged
        )
        # A genuinely "fully applied" outcome would mean every op landed
        # AND the transaction was marked complete (a new commit, clean
        # tree) -- not just files matching their patched content.
        fully_applied_and_committed = (
            a_applied and b_applied and c_applied
            and not status_dirty and not head_unchanged
        )
        detectably_incomplete = (
            a_applied and b_untouched and c_untouched
            and status_dirty and head_unchanged
        )

        if fully_original:
            classification = "fully_original"
        elif fully_applied_and_committed:
            classification = "fully_applied"
        elif detectably_incomplete:
            classification = "partial_but_detectably_incomplete"
        else:
            classification = "partial_and_indistinguishable"

        result = {
            "run_id": run_id,
            "barrier_reached": True,
            "child_returncode": child_returncode,
            "child_killed_by_sigkill": (child_returncode == -signal.SIGKILL.value),
            "manifest_before": manifest_before,
            "manifest_after_kill": manifest_after_kill,
            "a_py_patch_applied": a_applied,
            "b_py_patch_applied": b_applied,
            "c_py_patch_applied": c_applied,
            "b_py_untouched": b_untouched,
            "c_py_untouched": c_untouched,
            "head_unchanged": head_unchanged,
            "working_tree_dirty": status_dirty,
            "classification": classification,
        }
    finally:
        marker.unlink(missing_ok=True)
        ops_json.unlink(missing_ok=True)
        removed = remove_worktree(repo_dir, worktree)
        result["worktree_removed_cleanly"] = removed
    return result


# --------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------

def main() -> None:
    scratch_root = Path(tempfile.mkdtemp(prefix="codeagent-spike-s3-"))
    log(f"scratch_root = {scratch_root}")

    try:
        host_info = {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        }
        write(SPIKE_DIR / "host.json", json.dumps(host_info, indent=2))

        repo_dir = init_scratch_repo(scratch_root / "scratch-repo")
        write(
            SPIKE_DIR / "baseline-worktrees.txt",
            run(["git", "-C", str(repo_dir), "worktree", "list"]).stdout,
        )

        log("\n--- EXPERIMENT 1: prevalidation atomicity ---")
        result1 = experiment_1_prevalidation(scratch_root, repo_dir)
        write(SPIKE_DIR / "result1.json", json.dumps(result1, indent=2))

        log("\n--- EXPERIMENT 2: handled mid-application failure + rollback ---")
        result2 = experiment_2_handled_failure_rollback(scratch_root, repo_dir)
        write(SPIKE_DIR / "result2.json", json.dumps(result2, indent=2))

        log("\n--- EXPERIMENT 3: interruption (SIGKILL) detection ---")
        result3 = experiment_3_interruption_detection(scratch_root, repo_dir)
        write(SPIKE_DIR / "result3.json", json.dumps(result3, indent=2))

        write(
            SPIKE_DIR / "final-worktrees.txt",
            run(["git", "-C", str(repo_dir), "worktree", "list"]).stdout,
        )
        remaining = run(["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"]).stdout
        only_main_worktree_remains = remaining.strip().count("worktree ") <= 1
        leftover_worktree_dirs = [
            p.name for p in scratch_root.iterdir() if p.name.startswith("worktree-")
        ]

        summary = {
            "host": host_info,
            "experiment_1_classification": "PASS" if result1["worktree_byte_for_byte_unchanged"]
            and result1["rejected_before_any_write"] else "FAIL",
            "experiment_2_classification": "ROLLBACK_RESTORED_STATE" if result2["fully_restored_to_pre_apply_state"]
            else "ROLLBACK_DID_NOT_FULLY_RESTORE",
            "experiment_2_intermediate_state_was_inconsistent": result2["intermediate_state_was_inconsistent"],
            "experiment_3_classification": result3.get("classification", "INCONCLUSIVE_INFRA_FAILURE"),
            "no_leftover_worktrees_before_scratch_root_removal": (
                only_main_worktree_remains and not leftover_worktree_dirs
            ),
            "leftover_worktree_dirs": leftover_worktree_dirs,
        }
    finally:
        # Unconditional final cleanup of the ENTIRE unique scratch
        # root, regardless of what happened above.
        shutil.rmtree(scratch_root, ignore_errors=True)

    scratch_root_removed = not scratch_root.exists()
    summary["scratch_root"] = str(scratch_root)
    summary["scratch_root_removed"] = scratch_root_removed
    write(SPIKE_DIR / "summary.json", json.dumps(summary, indent=2))
    log("\n\nSUMMARY:\n" + json.dumps(summary, indent=2))

    write(SPIKE_DIR / "run.log", "\n".join(_LOG_LINES) + "\n")

    if not scratch_root_removed:
        raise AssertionError(f"scratch root was not removed: {scratch_root}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        _crash_child_main(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()
