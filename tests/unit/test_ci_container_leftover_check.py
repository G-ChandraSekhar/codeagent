"""Regression test for `.github/workflows/ci.yml`'s "Verify no leftover
CodeAgent verification containers" step (Milestone 3 Slice 3B-6).

Docker's own `docker ps -a --filter name=X` is a substring match, not
an anchored family-grammar proof, so the CI step instead lists every
container name once and applies an anchored `grep -E` pattern
client-side. Rather than testing a hand-copied duplicate of that
pattern (which could silently drift from the real workflow), this test
reads the actual workflow file, extracts the exact configured `grep -E`
pattern, and tests *that* — it fails if the workflow ever loses its
anchoring or drops one of the three container-name families."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# Matches `grep -E '<pattern>'` (single-quoted, as the workflow writes
# it) and captures `<pattern>` verbatim -- extraction only, never a
# hand-maintained duplicate of the pattern's own content.
_GREP_INVOCATION_RE = re.compile(r"grep -E '([^']*)'")


def _extract_configured_pattern() -> str:
    text = _WORKFLOW_PATH.read_text()
    match = _GREP_INVOCATION_RE.search(text)
    assert match is not None, f"no `grep -E '...'` invocation found in {_WORKFLOW_PATH}"
    return match.group(1)


@pytest.fixture(scope="module")
def configured_pattern() -> re.Pattern[str]:
    return re.compile(_extract_configured_pattern())


def test_workflow_still_configures_exactly_one_anchored_grep_invocation():
    text = _WORKFLOW_PATH.read_text()
    matches = _GREP_INVOCATION_RE.findall(text)
    assert len(matches) == 1, f"expected exactly one grep -E invocation in the workflow, found {len(matches)}"


def test_workflow_pattern_is_anchored_at_the_start(configured_pattern):
    assert _extract_configured_pattern().startswith("^"), (
        "the workflow's leftover-detection pattern must remain anchored "
        "(^) — an unanchored pattern would defeat the whole point of "
        "this client-side check versus Docker's own substring `--filter`"
    )


def test_workflow_pattern_detects_all_three_real_families(configured_pattern):
    assert configured_pattern.match("codeagent-verify-" + "a" * 12)
    assert configured_pattern.match("codeagent-baseline-" + "b" * 32)
    assert configured_pattern.match("codeagent-verification-" + "c" * 32)


def test_workflow_pattern_rejects_decoy_substrings(configured_pattern):
    assert not configured_pattern.match("some-codeagent-verify-thing")
    assert not configured_pattern.match("my-codeagent-baseline-app")
    assert not configured_pattern.match("unrelated-container")
    assert not configured_pattern.match("codeagent-other-thing")


def test_workflow_detection_step_remains_detection_only_and_runs_always():
    text = _WORKFLOW_PATH.read_text()
    # Locate the leftover-detection step's own block (from its `name:`
    # line up to the next top-level `- name:` step) and confirm it
    # never deletes a container and always runs, even on prior failure.
    step_start = text.index("Verify no leftover CodeAgent verification containers")
    block_start = text.rindex("- name:", 0, step_start + 1)
    next_step = text.find("\n      - name:", step_start)
    block = text[block_start : next_step if next_step != -1 else len(text)]
    assert "if: always()" in block
    assert "docker rm" not in block
    assert "docker kill" not in block
