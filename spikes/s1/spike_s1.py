"""
CodeAgent Stage 2 — S1 spike: worktree + Docker + pytest + cleanup.

Corrected per review:
- P1: per-test identity comparison via junit-xml, not aggregate counts.
- P1: cleanup wrapped in try/finally, not just sequential steps.
- P2: structured argv throughout, no shell=True / shell strings.

Produces the full evidence bundle:
  host.json, docker-version.txt, baseline-worktrees.txt, final-worktrees.txt,
  baseline-containers.txt, final-containers.txt,
  trial-N/{stdout.txt, stderr.txt, junit.xml, result.json},
  summary.json, S1_RESULT.md
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

EXPDIR = Path("/tmp/codeagent-spike-s1-v2")
REPO = EXPDIR / "scratch-repo"
IMAGE_TAG = "codeagent-spike-s1-image"
BASE_TAG = "codeagent-spike-s1-base"


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """Structured argv only — never shell=True, never an interpolated string."""
    print("$ " + " ".join(argv))
    result = subprocess.run(argv, capture_output=True, text=True, **kw)
    if result.stdout.strip():
        print(result.stdout)
    if result.stderr.strip():
        print(result.stderr)
    return result


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def record_host_and_docker_info() -> None:
    host_info = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }
    write(EXPDIR / "host.json", json.dumps(host_info, indent=2))

    docker_version = run(["docker", "version", "--format", "{{json .}}"])
    write(EXPDIR / "docker-version.txt", docker_version.stdout)


def parse_junit(junit_path: Path) -> list[dict]:
    """Per-test identity results, not just aggregate pass/fail counts."""
    if not junit_path.exists():
        return []
    tree = ET.parse(junit_path)
    testcases = []
    for tc in tree.iter("testcase"):
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None:
            status = "failed"
        elif error is not None:
            status = "error"
        elif skipped is not None:
            status = "skipped"
        else:
            status = "passed"
        testcases.append({
            "classname": tc.get("classname"),
            "name": tc.get("name"),
            "status": status,
        })
    return testcases


def run_trial(trial_num: int, image_digest: str) -> dict:
    run_id = f"s1-trial{trial_num}-{uuid.uuid4().hex[:8]}"
    container_name = f"codeagent-spike-{run_id}"
    worktree_path = EXPDIR / f"worktree-{run_id}"
    trial_dir = EXPDIR / f"trial-{trial_num}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    junit_host_path = trial_dir / "junit.xml"

    print(f"\n--- TRIAL {trial_num} (id={run_id}) ---")

    worktree_created = False
    docker_exit_code = None
    stdout_text = ""
    stderr_text = ""
    worktree_removed_cleanly = False

    try:
        r = run(["git", "-C", str(REPO), "worktree", "add", str(worktree_path), "HEAD"])
        worktree_created = (r.returncode == 0)

        # container writes junit.xml into the mounted worktree so we can read it back on the host
        r = run([
            "docker", "run", "--rm",
            "--name", container_name,
            "--label", "codeagent.spike=s1",
            "--label", f"codeagent.run_id={run_id}",
            "-v", f"{worktree_path}:/workspace",
            "-w", "/workspace",
            IMAGE_TAG,
            "python3", "-m", "pytest", "-q", "--junit-xml=/workspace/junit.xml",
        ])
        docker_exit_code = r.returncode
        stdout_text = r.stdout
        stderr_text = r.stderr

        junit_container_path = worktree_path / "junit.xml"
        if junit_container_path.exists():
            shutil.copy(junit_container_path, junit_host_path)

    finally:
        # guaranteed cleanup regardless of what happened above
        r2 = run(["git", "-C", str(REPO), "worktree", "remove", str(worktree_path), "--force"])
        worktree_removed_cleanly = (r2.returncode == 0)

    write(trial_dir / "stdout.txt", stdout_text)
    write(trial_dir / "stderr.txt", stderr_text)

    testcases = parse_junit(junit_host_path)

    container_check = run([
        "docker", "ps", "-a",
        "--filter", f"name=^/{container_name}$",
        "--format", "{{.Names}}",
    ])
    container_leftover = container_name in container_check.stdout
    worktree_dir_leftover = worktree_path.exists()

    result = {
        "trial": trial_num,
        "run_id": run_id,
        "image_digest": image_digest,
        "worktree_created": worktree_created,
        "docker_exit_code": docker_exit_code,
        "testcases": testcases,
        "worktree_removed_cleanly": worktree_removed_cleanly,
        "container_leftover": container_leftover,
        "worktree_dir_leftover": worktree_dir_leftover,
    }
    write(trial_dir / "result.json", json.dumps(result, indent=2))
    return result


def main() -> None:
    shutil.rmtree(EXPDIR, ignore_errors=True)
    EXPDIR.mkdir(parents=True)

    record_host_and_docker_info()

    # baseline inventories, saved to files (not just printed)
    write(EXPDIR / "baseline-containers.txt", run(["docker", "ps", "-a"]).stdout)

    REPO.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=REPO)
    run(["git", "config", "user.email", "spike@example.com"], cwd=REPO)
    run(["git", "config", "user.name", "Spike"], cwd=REPO)
    (REPO / "test_sample.py").write_text(
        "def test_addition():\n    assert 1 + 1 == 2\n\n"
        "def test_string():\n    assert \"abc\"[::-1] == \"cba\"\n"
    )
    run(["git", "add", "-A"], cwd=REPO)
    run(["git", "commit", "-q", "-m", "scratch fixture"], cwd=REPO)

    write(EXPDIR / "baseline-worktrees.txt", run(["git", "-C", str(REPO), "worktree", "list"]).stdout)

    # build base rootfs via debootstrap (no docker.io registry involved)
    rootfs = EXPDIR / "rootfs"
    run(["debootstrap", "--variant=minbase", "noble", str(rootfs), "http://archive.ubuntu.com/ubuntu"])

    tar_proc = subprocess.Popen(["tar", "-C", str(rootfs), "-c", "."], stdout=subprocess.PIPE)
    import_proc = subprocess.run(["docker", "import", "-", BASE_TAG], stdin=tar_proc.stdout, capture_output=True, text=True)
    tar_proc.wait()
    print(import_proc.stdout)

    dockerfile = EXPDIR / "Dockerfile.spike"
    dockerfile.write_text(
        f"FROM {BASE_TAG}:latest\n"
        'RUN echo "deb http://archive.ubuntu.com/ubuntu noble main universe" > /etc/apt/sources.list\n'
        "RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pytest "
        "&& rm -rf /var/lib/apt/lists/*\n"
    )
    run(["docker", "build", "-t", IMAGE_TAG, "-f", str(dockerfile), str(EXPDIR)])

    digest_result = run(["docker", "images", "--no-trunc", "--quiet", IMAGE_TAG])
    image_digest = digest_result.stdout.strip()

    results = [run_trial(i, image_digest) for i in range(1, 4)]

    # normalized comparison: per-test identity + status, not aggregate counts
    testcase_sets = [tuple(sorted((tc["classname"], tc["name"], tc["status"]) for tc in r["testcases"])) for r in results]
    all_equivalent = len(set(testcase_sets)) == 1 and len(testcase_sets[0]) > 0

    write(EXPDIR / "final-containers.txt", run(["docker", "ps", "-a"]).stdout)
    write(EXPDIR / "final-worktrees.txt", run(["git", "-C", str(REPO), "worktree", "list"]).stdout)

    labeled_check = run(["docker", "ps", "-a", "--filter", "label=codeagent.spike=s1", "--format", "{{.Names}}"])
    no_labeled_containers_remain = (labeled_check.stdout.strip() == "")

    summary = {
        "host": json.loads((EXPDIR / "host.json").read_text()),
        "image_digest": image_digest,
        "trials": results,
        "per_test_identity_equivalent_across_trials": all_equivalent,
        "no_labeled_containers_remain_after_cleanup": no_labeled_containers_remain,
    }
    write(EXPDIR / "summary.json", json.dumps(summary, indent=2))

    # final cleanup: remove the images we built for this spike, and record that we did
    rmi_result = run(["docker", "rmi", IMAGE_TAG, BASE_TAG])
    write(EXPDIR / "final-image-cleanup.txt", rmi_result.stdout + rmi_result.stderr)

    classification = "PASS" if all_equivalent and no_labeled_containers_remain else "TECHNICAL FAILURE"

    result_md = f"""# S1_RESULT.md — Linux

Host: {summary['host']['system']} {summary['host']['release']} ({summary['host']['machine']})
Image digest: {image_digest}

## Per-trial results
""" + "\n".join(
        f"- Trial {r['trial']} ({r['run_id']}): exit={r['docker_exit_code']}, "
        f"tests={[(tc['name'], tc['status']) for tc in r['testcases']]}, "
        f"worktree_removed={r['worktree_removed_cleanly']}, "
        f"container_leftover={r['container_leftover']}"
        for r in results
    ) + f"""

## Cross-trial comparison
Per-test-identity equivalent across all 3 trials: {all_equivalent}
No labeled (codeagent.spike=s1) containers remain after cleanup: {no_labeled_containers_remain}

## Classification: {classification}
"""
    write(EXPDIR / "S1_RESULT.md", result_md)
    print("\n\n" + result_md)


if __name__ == "__main__":
    main()
