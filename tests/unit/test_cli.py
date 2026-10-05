"""Tests for the `codeagent` CLI (ADR 0004 section 11, Amendment 18):
grammar, exact exit codes, rendering, and output sanitization."""

from __future__ import annotations

import pytest

from codeagent import cli
from codeagent import maintenance as mt
from codeagent import reconciliation as rc

LID = "a" * 32
REASON = "/Users/SENTINEL-REASON-9b1c/secret"


def test_exit_codes_are_pinned():
    """M12."""
    assert {c.name: c.value for c in cli.ExitCode} == {
        "CLEAN": 0,
        "INTERNAL_ERROR": 1,
        "INVALID_INVOCATION": 2,
        "UNRESOLVED_ACKNOWLEDGED": 3,
        "BLOCKED": 4,
    }


def _exit_of(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    return excinfo.value.code, capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["solve", "--repo", "r"],
        ["reconcile"],
        ["reconcile", "--repo", "r", "--abandon", "XYZ"],
        ["reconcile", "--repo", "r", "--abandon", "A" * 32],
        ["reconcile", "--repo", "r", "--abandon", "a" * 31],
        ["reconcile", "--repo", "r", "--dry-run", "--abandon", LID],
        ["reconcile", "--repo", "r", "--acknowledge-unresolved", "--reason", "x"],
        ["reconcile", "--repo", "r", "--abandon", LID, "--reason", "x"],
        ["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved"],
        ["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved", "--reason", ""],
        ["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved", "--reason", "x" * 257],
    ],
)
def test_invalid_invocation_exits_2(argv, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: called.append(1))
    monkeypatch.setattr(cli, "run_abandon", lambda *a, **k: called.append(1))
    code, _ = _exit_of(argv, capsys)
    assert code == 2 and called == []


@pytest.mark.parametrize("bad", ["two\nlines", "esc\x1b[2J", f"{REASON}‮"])
def test_invalid_reason_is_never_echoed(bad, capsys):
    code, out = _exit_of(
        ["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved", "--reason", bad], capsys
    )
    assert code == 2
    assert bad not in out.err and bad not in out.out
    assert "SENTINEL-REASON" not in out.err + out.out


def test_help_lists_only_reconcile(capsys):
    code, out = _exit_of(["--help"], capsys)
    assert code == 0
    assert "reconcile" in out.out and "solve" not in out.out


def test_invalid_repository_exits_2_without_echoing_the_path(capsys, monkeypatch):
    def raise_invalid(*a, **k):
        raise mt.InvalidRepositoryError("git_discovery_unavailable")

    monkeypatch.setattr(cli, "run_reconcile", raise_invalid)
    assert cli.main(["reconcile", "--repo", "/Users/SENTINEL-PATH/repo"]) == 2
    out = capsys.readouterr()
    assert "SENTINEL-PATH" not in out.out + out.err
    assert "git_discovery_unavailable" in out.err


def test_internal_error_exits_1_with_fixed_text_only(capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("leaky detail /Users/SENTINEL-PATH/secret")

    monkeypatch.setattr(cli, "run_reconcile", boom)
    assert cli.main(["reconcile", "--repo", "r"]) == 1
    out = capsys.readouterr()
    assert out.err == "codeagent: internal error (RuntimeError)\n"
    assert out.out == ""


def test_keyboard_interrupt_is_not_caught(monkeypatch):
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cli.main(["reconcile", "--repo", "r"])


@pytest.mark.parametrize(
    "result,code",
    [
        (mt.ReconcileResult.CLEAN, 0),
        (mt.ReconcileResult.UNRESOLVED_ACKNOWLEDGED, 3),
        (mt.ReconcileResult.BLOCKED, 4),
    ],
)
def test_reconcile_exit_follows_the_result(result, code, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: mt.ReconcileReport(result=result, dry_run=False))
    assert cli.main(["reconcile", "--repo", "r"]) == code


def _unresolved_entry():
    return rc.ReconciliationEntryResult(
        LID,
        rc.ReconciliationEntryOutcome.SKIPPED_ABANDONED_UNRESOLVED,
        "abandoned entry with acknowledged unresolved resources; never clean",
        abandonment_disposition="ABANDONED_UNRESOLVED",
        abandonment_remaining={"baseline_container": "present", "checkpoint_ref": "absent"},
    )


def test_unresolved_warning_names_the_lifecycle_and_summary_never_the_reason(monkeypatch, capsys):
    """M22."""
    report = mt.ReconcileReport(
        result=mt.ReconcileResult.UNRESOLVED_ACKNOWLEDGED, dry_run=False, maintenance_id="f" * 32, entries=(_unresolved_entry(),)
    )
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: report)
    assert cli.main(["reconcile", "--repo", "r"]) == 3
    out = capsys.readouterr()
    warnings = [l for l in out.err.splitlines() if l.startswith("WARNING:")]
    assert len(warnings) == 1 and LID in warnings[0] and "baseline_container=present" in warnings[0]
    assert "SENTINEL-REASON" not in out.out + out.err


def test_dry_run_output_states_no_maintenance_trace(monkeypatch, capsys):
    """C4."""
    plan_entry = rc.PlanEntry(LID, rc.PlanEntryOutcome.PENDING, "eligible", remaining=None)
    report = mt.ReconcileReport(result=mt.ReconcileResult.BLOCKED, dry_run=True, entries=(plan_entry,))
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: report)
    assert cli.main(["reconcile", "--repo", "r", "--dry-run"]) == 4
    out = capsys.readouterr().out
    assert "maintenance trace: none" in out and "(dry run)" in out and "pending" in out


@pytest.mark.parametrize(
    "outcome,stages,code",
    [
        (mt.AbandonOutcome.RECORDED_ABANDONED, (), 0),
        (mt.AbandonOutcome.RECORDED_UNRESOLVED, (), 3),
        (mt.AbandonOutcome.REFUSED, (), 4),
        (mt.AbandonOutcome.NOT_RECORDED, (), 4),
        (mt.AbandonOutcome.RECORDED_ABANDONED, ("marker_directory_fsync",), 4),
        (mt.AbandonOutcome.RECORDED_UNRESOLVED, ("maintenance_event",), 4),
    ],
)
def test_abandon_exit_is_disposition_based(outcome, stages, code, monkeypatch, capsys):
    """U3 / U6."""
    report = mt.AbandonReport(outcome=outcome, lifecycle_id=LID, unconfirmed_stages=stages)
    monkeypatch.setattr(cli, "run_abandon", lambda *a, **k: report)
    argv = ["reconcile", "--repo", "r", "--abandon", LID]
    if outcome is mt.AbandonOutcome.RECORDED_UNRESOLVED:
        argv += ["--acknowledge-unresolved", "--reason", REASON]
    assert cli.main(argv) == code
    out = capsys.readouterr()
    assert 'codeagent reconcile --repo <path>" for repository-wide status' in out.out
    assert "SENTINEL-REASON" not in out.out + out.err


def test_abandon_passes_the_reason_only_to_maintenance(monkeypatch, capsys):
    seen = {}

    def run_abandon(repo, lifecycle_id, *, acknowledge_unresolved, reason):
        seen.update(repo=repo, lifecycle_id=lifecycle_id, ack=acknowledge_unresolved, reason=reason)
        return mt.AbandonReport(outcome=mt.AbandonOutcome.RECORDED_UNRESOLVED, lifecycle_id=lifecycle_id)

    monkeypatch.setattr(cli, "run_abandon", run_abandon)
    cli.main(["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved", "--reason", REASON])
    assert seen == {"repo": "r", "lifecycle_id": LID, "ack": True, "reason": REASON}
    out = capsys.readouterr()
    assert "SENTINEL-REASON" not in out.out + out.err


# ---------------------------------------------------------------------------
# D1 correction pass: F3 parser diagnostics, F4/F5 rendering, F2 stages
# ---------------------------------------------------------------------------

PATH_SENTINEL = "/Users/PRIVATE-REVIEW-SENTINEL/secret"


@pytest.mark.parametrize(
    "argv",
    [
        [PATH_SENTINEL],  # invalid subcommand
        ["reconcile", "--repo", "/unused", "--dry-run", PATH_SENTINEL],  # extra argument
        ["reconcile", "--repo", "/unused", f"--{PATH_SENTINEL}"],  # unknown option
        ["reconcile", "--repo", "/unused", f"--dry-run={PATH_SENTINEL}"],  # value for a flag
        ["reconcile", f"--repo={PATH_SENTINEL}", "--a", LID],  # ambiguous abbreviation
        ["reconcile", "--repo"],  # missing value
        ["reconcile", "--repo", PATH_SENTINEL, "--abandon", PATH_SENTINEL],  # malformed id
        ["reconcile", "--repo", "/unused", "--abandon", LID, "--reason", REASON],  # reason without ack
        ["reconcile", "--repo", "/unused", "--abandon", LID, "--acknowledge-unresolved", "--reason", REASON, PATH_SENTINEL],
        [f"--{PATH_SENTINEL}", "reconcile"],
    ],
)
def test_f3_grammar_errors_never_echo_operator_input(argv, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: called.append(1))
    monkeypatch.setattr(cli, "run_abandon", lambda *a, **k: called.append(1))
    code, out = _exit_of(argv, capsys)
    text = out.out + out.err
    assert code == 2 and called == []
    assert "PRIVATE-REVIEW-SENTINEL" not in text and "SENTINEL-REASON" not in text
    assert "usage: codeagent" in out.err and "error:" in out.err


def test_f3_help_is_still_useful(capsys):
    code, out = _exit_of(["reconcile", "--help"], capsys)
    assert code == 0 and "--abandon" in out.out and "--dry-run" in out.out


def test_f2_internal_error_names_cleanup_stages_only(capsys, monkeypatch):
    def boom(*a, **k):
        exc = RuntimeError("/Users/SENTINEL-PATH/secret")
        exc.codeagent_unconfirmed_stages = ("lifecycle_lock", "state_root")
        raise exc

    monkeypatch.setattr(cli, "run_reconcile", boom)
    assert cli.main(["reconcile", "--repo", "r"]) == 1
    err = capsys.readouterr().err
    assert err == (
        "codeagent: internal error (RuntimeError)\n"
        "codeagent: cleanup unconfirmed: lifecycle_lock, state_root\n"
    )


def test_f5_incomplete_trace_is_rendered(monkeypatch, capsys):
    report = mt.ReconcileReport(
        result=mt.ReconcileResult.BLOCKED,
        dry_run=False,
        maintenance_id="f" * 32,
        trace_incomplete=True,
        blocked_reason="reconciliation_substrate_unavailable",
    )
    monkeypatch.setattr(cli, "run_reconcile", lambda *a, **k: report)
    assert cli.main(["reconcile", "--repo", "r"]) == 4
    assert f"maintenance trace: {'f' * 32} (incomplete)" in capsys.readouterr().out


def test_f4_inspection_cleanup_is_rendered_and_exits_4(monkeypatch, capsys):
    remaining = rc.RemainingResources(
        "absent", "absent", "absent", "unknown", "absent", "absent", cleanup_unconfirmed=("admin_scan",)
    )
    report = mt.AbandonReport(
        outcome=mt.AbandonOutcome.REFUSED,
        lifecycle_id=LID,
        refusal="inspection_cleanup_unconfirmed",
        remaining=remaining,
        unconfirmed_stages=("inspection_admin_scan",),
    )
    monkeypatch.setattr(cli, "run_abandon", lambda *a, **k: report)
    argv = ["reconcile", "--repo", "r", "--abandon", LID, "--acknowledge-unresolved", "--reason", "x"]
    assert cli.main(argv) == 4
    out = capsys.readouterr().out
    assert "refusal: inspection_cleanup_unconfirmed" in out and "unconfirmed: inspection_admin_scan" in out
