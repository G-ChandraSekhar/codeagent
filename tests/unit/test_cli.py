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


# ---------------------------------------------------------------------------
# ADR 0005 Amendment 1 (ledger D3): `owned_cancellation()` and its 28-row
# signal-ownership failure matrix (D3 plan section 8.2, F1-F23).
#
# `signal.signal`/`signal.getsignal` are replaced by a scripted fake over an
# in-memory handler table, so no real handler is touched except in F11.
# Call numbering (1-based) for an ordinary enter/exit: G1 before SIGINT,
# S1 install SIGINT, G2 before SIGTERM, S2 install SIGTERM; exit S3 restore
# SIGTERM, G3 confirm, S4 restore SIGINT, G4 confirm.
# ---------------------------------------------------------------------------

import ast  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from codeagent.cli import (  # noqa: E402
    SignalFailureKind as K,
    SignalOwnershipError,
    SignalOwnershipReason as R,
    SignalStage as St,
)

INT, TERM = signal.SIGINT, signal.SIGTERM
RAISED, OBS, MISMATCH = K.RAISED, K.OBSERVATION_RAISED, K.IDENTITY_MISMATCH


class Sentinel(Exception):
    pass


class BodyError(Exception):
    pass


ORIG = {INT: object(), TERM: object()}


class SignalHarness:
    """Scripted fake. `s` maps a signal() call number to "raise:x" (raise,
    unchanged), "change_raise:x" (change, then raise) or "noop" (return
    without changing); `g` maps a getsignal() call number to "raise:x"."""

    def __init__(self, monkeypatch, s=None, g=None):
        self.table = dict(ORIG)
        self.s, self.g = dict(s or {}), dict(g or {})
        self.sentinels = {name: Sentinel(f"SENTINEL-{name}-/secret/path") for name in "abcdefghijk"}
        self.signal_calls: list[tuple[int, object]] = []
        self.getsignal_calls = 0
        monkeypatch.setattr(cli.signal, "signal", self._signal)
        monkeypatch.setattr(cli.signal, "getsignal", self._getsignal)

    def _signal(self, sig, handler):
        self.signal_calls.append((sig, handler))
        action = self.s.get(len(self.signal_calls))
        if action == "noop":
            return None
        if action and action.startswith("change_raise:"):
            self.table[sig] = handler
            raise self.sentinels[action.split(":")[1]]
        if action and action.startswith("raise:"):
            raise self.sentinels[action.split(":")[1]]
        previous, self.table[sig] = self.table[sig], handler
        return previous

    def _getsignal(self, sig):
        self.getsignal_calls += 1
        action = self.g.get(self.getsignal_calls)
        if action and action.startswith("raise:"):
            raise self.sentinels[action.split(":")[1]]
        return self.table[sig]

    def restores(self):
        return [(sig, h) for sig, h in self.signal_calls if h is ORIG[sig]]


def _expected(h, spec):
    out = []
    for stage, kind, name in spec:
        out.append((stage, kind, None if name is None else h.sentinels[name]))
    return out


def _check_failures(err, h, spec):
    got = [(f.stage, f.kind, f.exception) for f in err.failures]
    want = _expected(h, spec)
    assert len(got) == len(want)
    for (gs, gk, ge), (ws, wk, we) in zip(got, want):
        assert gs is ws and gk is wk and ge is we
    assert "SENTINEL" not in str(err) and "secret" not in str(err)


def _enter(owner, h):
    try:
        owner.__enter__()
    except SignalOwnershipError as err:
        return err
    return None


# Rows whose installation stops (F2-F4b, F12-F18b, F23).
ENTER_ROWS = {
    "F2": dict(s={1: "raise:a"}, failures=[(St.INSTALL_SIGINT, RAISED, "a")],
               reason=R.INSTALL_FAILED, cause="a", unrestored=(), restore_calls=0, unconfirmed=False),
    "F3": dict(s={2: "raise:b"}, failures=[(St.INSTALL_SIGTERM, RAISED, "b")],
               reason=R.INSTALL_FAILED, cause="b", unrestored=(), restore_calls=1, unconfirmed=False),
    "F4a": dict(s={2: "raise:b", 3: "raise:c"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.RESTORE_SIGINT, RAISED, "c")],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGINT",), restore_calls=1, unconfirmed=True),
    "F4b": dict(s={2: "raise:b", 3: "noop"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.RESTORE_SIGINT, MISMATCH, None)],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGINT",), restore_calls=1, unconfirmed=True),
    "F12": dict(s={1: "change_raise:a"},
                failures=[(St.INSTALL_SIGINT, RAISED, "a"), (St.INSTALL_SIGINT, MISMATCH, None)],
                reason=R.INSTALL_FAILED, cause="a", unrestored=(), restore_calls=1, unconfirmed=False),
    "F13": dict(s={1: "change_raise:a", 2: "raise:c"},
                failures=[(St.INSTALL_SIGINT, RAISED, "a"), (St.INSTALL_SIGINT, MISMATCH, None),
                          (St.RESTORE_SIGINT, RAISED, "c")],
                reason=R.RESTORE_UNCONFIRMED, cause="a", unrestored=("SIGINT",), restore_calls=1, unconfirmed=True),
    "F14": dict(s={1: "change_raise:a", 2: "noop"},
                failures=[(St.INSTALL_SIGINT, RAISED, "a"), (St.INSTALL_SIGINT, MISMATCH, None),
                          (St.RESTORE_SIGINT, MISMATCH, None)],
                reason=R.RESTORE_UNCONFIRMED, cause="a", unrestored=("SIGINT",), restore_calls=1, unconfirmed=True),
    "F15": dict(s={2: "change_raise:b"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.INSTALL_SIGTERM, MISMATCH, None)],
                reason=R.INSTALL_FAILED, cause="b", unrestored=(), restore_calls=2, unconfirmed=False),
    "F16": dict(s={2: "change_raise:b", 3: "raise:d", 4: "raise:e"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.INSTALL_SIGTERM, MISMATCH, None),
                          (St.RESTORE_SIGTERM, RAISED, "d"), (St.RESTORE_SIGINT, RAISED, "e")],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGTERM", "SIGINT"), restore_calls=2,
                unconfirmed=True),
    "F17": dict(s={2: "change_raise:b", 3: "noop"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.INSTALL_SIGTERM, MISMATCH, None),
                          (St.RESTORE_SIGTERM, MISMATCH, None)],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGTERM",), restore_calls=2, unconfirmed=True),
    "F18": dict(s={2: "raise:b", 4: "raise:g"}, g={3: "raise:f"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.INSTALL_SIGTERM, OBS, "f"),
                          (St.RESTORE_SIGINT, RAISED, "g")],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGINT",), restore_calls=2, unconfirmed=True),
    "F18b": dict(g={1: "raise:h"}, failures=[(St.INSTALL_SIGINT, OBS, "h")],
                 reason=R.INSTALL_FAILED, cause="h", unrestored=(), restore_calls=0, unconfirmed=False),
    "F23": dict(s={2: "raise:b"}, g={4: "raise:k"},
                failures=[(St.INSTALL_SIGTERM, RAISED, "b"), (St.RESTORE_SIGINT, OBS, "k")],
                reason=R.RESTORE_UNCONFIRMED, cause="b", unrestored=("SIGINT",), restore_calls=1, unconfirmed=True),
}

# Restore calls are counted as calls after installation stopped.
_INSTALL_CALLS = {"F2": 1, "F3": 2, "F4a": 2, "F4b": 2, "F12": 1, "F13": 1, "F14": 1, "F15": 2, "F16": 2,
                  "F17": 2, "F18": 2, "F18b": 0, "F23": 2}


@pytest.mark.parametrize("row", sorted(ENTER_ROWS))
def test_enter_failure_rows(row, monkeypatch):
    spec = ENTER_ROWS[row]
    h = SignalHarness(monkeypatch, spec.get("s"), spec.get("g"))
    owner = cli.owned_cancellation()
    body_ran = False
    with pytest.raises(SignalOwnershipError) as info:
        with owner:
            body_ran = True
    err = info.value
    assert not body_ran
    assert err.reason is spec["reason"]
    _check_failures(err, h, spec["failures"])
    assert err.unrestored == spec["unrestored"]
    assert err.__cause__ is h.sentinels[spec["cause"]] and err.__suppress_context__
    # Exactly one restoration call per owed signal, none for untouched ones.
    assert len(h.signal_calls) - _INSTALL_CALLS[row] == spec["restore_calls"]
    for sig in (INT, TERM):
        name = "SIGINT" if sig is INT else "SIGTERM"
        if name not in spec["unrestored"]:
            assert h.table[sig] is ORIG[sig]
    expected_state = "enter_failed_unconfirmed" if spec["unconfirmed"] else "enter_failed"
    assert owner._state.value == expected_state


def test_f1_not_main_thread(monkeypatch):
    h = SignalHarness(monkeypatch)
    box = {}

    def run():
        owner = cli.owned_cancellation()
        try:
            owner.__enter__()
        except SignalOwnershipError as err:
            box["err"], box["owner"] = err, owner

    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    err = box["err"]
    assert err.reason is R.NOT_MAIN_THREAD and err.failures == () and err.unrestored == ()
    assert err.__cause__ is None and err.__suppress_context__
    assert h.signal_calls == [] and h.getsignal_calls == 0
    assert box["owner"]._state.value == "enter_failed"


def test_f1_unsupported_platform(monkeypatch):
    h = SignalHarness(monkeypatch)
    monkeypatch.setattr(cli.os, "name", "nt")
    owner = cli.owned_cancellation()
    with pytest.raises(SignalOwnershipError) as info:
        owner.__enter__()
    err = info.value
    assert err.reason is R.UNSUPPORTED_PLATFORM and err.failures == () and err.unrestored == ()
    assert err.__cause__ is None
    assert h.signal_calls == [] and h.getsignal_calls == 0


# Exit rows: (s, g, body raises, failures, cause, unrestored).
EXIT_ROWS = {
    "F6a": dict(s={3: "raise:d", 4: "raise:e"}, body=False,
                failures=[(St.RESTORE_SIGTERM, RAISED, "d"), (St.RESTORE_SIGINT, RAISED, "e")],
                cause="d", unrestored=("SIGTERM", "SIGINT")),
    "F6b": dict(s={3: "noop", 4: "raise:e"}, body=False,
                failures=[(St.RESTORE_SIGTERM, MISMATCH, None), (St.RESTORE_SIGINT, RAISED, "e")],
                cause="e", unrestored=("SIGTERM", "SIGINT")),
    "F6c": dict(s={3: "noop", 4: "noop"}, body=False,
                failures=[(St.RESTORE_SIGTERM, MISMATCH, None), (St.RESTORE_SIGINT, MISMATCH, None)],
                cause=None, unrestored=("SIGTERM", "SIGINT")),
    "F6c-term": dict(s={3: "noop"}, body=False, failures=[(St.RESTORE_SIGTERM, MISMATCH, None)],
                     cause=None, unrestored=("SIGTERM",)),
    "F8": dict(s={3: "raise:d", 4: "noop"}, body=True,
               failures=[(St.RESTORE_SIGTERM, RAISED, "d"), (St.RESTORE_SIGINT, MISMATCH, None)],
               cause="BODY", unrestored=("SIGTERM", "SIGINT")),
    "F20": dict(g={3: "raise:i"}, body=False, failures=[(St.RESTORE_SIGTERM, OBS, "i")],
                cause="i", unrestored=("SIGTERM",)),
    "F21": dict(g={4: "raise:j"}, body=True, failures=[(St.RESTORE_SIGINT, OBS, "j")],
                cause="BODY", unrestored=("SIGINT",)),
}


def _run_exit_row(row, monkeypatch):
    spec = EXIT_ROWS[row]
    h = SignalHarness(monkeypatch, spec.get("s"), spec.get("g"))
    owner = cli.owned_cancellation()
    body = BodyError("BODY-SECRET")
    result = None
    with pytest.raises(SignalOwnershipError) as info:
        with owner as source:
            assert source is owner.source
            result = "bound-before-exit"
            if spec["body"]:
                raise body
    err = info.value
    assert result == "bound-before-exit"  # the body's result stays bound in the caller
    return spec, h, owner, err, body


@pytest.mark.parametrize("row", sorted(EXIT_ROWS))
def test_exit_failure_rows(row, monkeypatch):
    spec, h, owner, err, body = _run_exit_row(row, monkeypatch)
    assert err.reason is R.RESTORE_UNCONFIRMED
    _check_failures(err, h, spec["failures"])
    assert err.unrestored == spec["unrestored"]
    expected_cause = body if spec["cause"] == "BODY" else (
        None if spec["cause"] is None else h.sentinels[spec["cause"]])
    assert err.__cause__ is expected_cause and err.__suppress_context__
    assert "BODY-SECRET" not in str(err)
    # Every owed restore attempted exactly once, even after a failure (M21):
    # S3 (SIGTERM) then S4 (SIGINT), each back to its original handler.
    assert h.signal_calls[2:] == [(TERM, ORIG[TERM]), (INT, ORIG[INT])]
    assert len(h.signal_calls) == 4
    assert owner._state.value == "closed_unconfirmed"


@pytest.mark.parametrize("row", ["F6a", "F6b", "F6c", "F8", "F20", "F21"])
def test_f10_f22_repeated_close_reraises_the_same_object(row, monkeypatch):
    spec, h, owner, err, _ = _run_exit_row(row, monkeypatch)
    failures, unrestored = err.failures, err.unrestored
    calls, gets = len(h.signal_calls), h.getsignal_calls
    for again in (owner.close, lambda: owner.__exit__(None, None, None)):
        with pytest.raises(SignalOwnershipError) as info:
            again()
        assert info.value is err
        assert err.failures is failures and err.unrestored == unrestored
    assert len(h.signal_calls) == calls and h.getsignal_calls == gets


@pytest.mark.parametrize("row", ["F4a", "F4b", "F13", "F14", "F16", "F17", "F18", "F23"])
def test_f19_repeated_close_after_unconfirmed_enter(row, monkeypatch):
    spec = ENTER_ROWS[row]
    h = SignalHarness(monkeypatch, spec.get("s"), spec.get("g"))
    owner = cli.owned_cancellation()
    with pytest.raises(SignalOwnershipError) as info:
        owner.__enter__()
    err = info.value
    failures = err.failures
    calls, gets = len(h.signal_calls), h.getsignal_calls
    for _ in range(2):
        with pytest.raises(SignalOwnershipError) as again:
            owner.close()
        assert again.value is err and err.failures is failures and err.unrestored == spec["unrestored"]
    assert len(h.signal_calls) == calls and h.getsignal_calls == gets


@pytest.mark.parametrize("row", ["F2", "F3", "F12", "F15", "F18b"])
def test_f19b_close_after_confirmed_enter_failure_is_a_noop(row, monkeypatch):
    spec = ENTER_ROWS[row]
    h = SignalHarness(monkeypatch, spec.get("s"), spec.get("g"))
    owner = cli.owned_cancellation()
    with pytest.raises(SignalOwnershipError):
        owner.__enter__()
    calls, gets = len(h.signal_calls), h.getsignal_calls
    owner.close()
    owner.__exit__(None, None, None)
    assert len(h.signal_calls) == calls and h.getsignal_calls == gets


def test_f19b_close_after_f1_is_a_noop(monkeypatch):
    h = SignalHarness(monkeypatch)
    monkeypatch.setattr(cli.os, "name", "nt")
    owner = cli.owned_cancellation()
    with pytest.raises(SignalOwnershipError):
        owner.__enter__()
    owner.close()
    assert h.signal_calls == [] and h.getsignal_calls == 0


def test_f5_confirmed_close_and_f9_repeat_is_a_noop(monkeypatch):
    h = SignalHarness(monkeypatch)
    owner = cli.owned_cancellation()
    with owner as source:
        assert h.table[INT] is h.table[TERM]  # our handler on both
        assert not source.is_cancelled()
    assert h.table == ORIG
    assert [sig for sig, _ in h.signal_calls] == [INT, TERM, TERM, INT]
    assert owner._state.value == "closed_confirmed"
    calls, gets = len(h.signal_calls), h.getsignal_calls
    owner.close()
    owner.__exit__(None, None, None)
    assert len(h.signal_calls) == calls and h.getsignal_calls == gets


@pytest.mark.parametrize("exc_type", [BodyError, KeyboardInterrupt, SystemExit])
def test_f7_body_exception_propagates_unchanged(exc_type, monkeypatch):
    h = SignalHarness(monkeypatch)
    body = exc_type("x")
    with pytest.raises(exc_type) as info:
        with cli.owned_cancellation():
            raise body
    assert info.value is body
    assert h.table == ORIG


def test_handler_requests_with_the_signal_reason(monkeypatch):
    h = SignalHarness(monkeypatch)
    with cli.owned_cancellation(record_requests=True) as source:
        try:
            h.table[TERM](int(TERM), None)
            h.table[INT](int(INT), None)
        except BaseException as exc:  # noqa: BLE001 - M1: the handler must only request
            pytest.fail(f"the handler raised {type(exc).__name__} instead of requesting")
        assert source.candidates == ("signal:SIGTERM",)
        assert source.requests_received == 2


def test_f11_real_signals_inside_and_after_the_window():
    """Real handlers: inside the window a real SIGINT only requests; after a
    confirmed restore the previous disposition runs."""
    seen = []
    previous = {sig: signal.getsignal(sig) for sig in (INT, TERM)}
    recorder = lambda signum, frame: seen.append(signum)  # noqa: E731
    signal.signal(INT, recorder)
    signal.signal(TERM, recorder)
    try:
        with cli.owned_cancellation(record_requests=True) as source:
            os.kill(os.getpid(), INT)
            deadline = time.monotonic() + 5
            while source.requests_received < 1:
                assert time.monotonic() < deadline
                time.sleep(0.001)
            os.kill(os.getpid(), TERM)
            while source.requests_received < 2:
                assert time.monotonic() < deadline
                time.sleep(0.001)
            assert source.candidates == ("signal:SIGINT",) and seen == []
        assert signal.getsignal(INT) is recorder and signal.getsignal(TERM) is recorder
        os.kill(os.getpid(), INT)
        deadline = time.monotonic() + 5
        while seen != [INT]:
            assert time.monotonic() < deadline
            time.sleep(0.001)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_owned_cancellation_cannot_be_entered_twice(monkeypatch):
    SignalHarness(monkeypatch)
    owner = cli.owned_cancellation()
    with owner:
        with pytest.raises(RuntimeError):
            owner.__enter__()


# --- D3 boundary pins ------------------------------------------------------

_SRC = Path(inspect.getfile(cli)).parent


def test_parser_subcommands_are_exactly_reconcile():
    parser = cli.build_parser()
    (sub,) = [a for a in parser._actions if a.dest == "command"]
    assert set(sub.choices) == {"reconcile"}


def test_main_and_dispatch_never_reference_owned_cancellation():
    """M27: D3 does not wire `main()`; `reconcile` is never wrapped."""
    tree = ast.parse(Path(inspect.getfile(cli)).read_text())
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"main", "_dispatch"}):
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not ({"owned_cancellation", "_OwnedCancellation", "signal"} & (names | attrs))


def test_production_signal_signal_calls_exist_only_in_cli():
    """M29: no other production module calls `signal.signal`."""
    offenders = []
    for path in sorted(_SRC.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "signal"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "signal"
            ):
                offenders.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module == "signal":
                assert all(a.name != "signal" for a in node.names), path.name
    assert set(offenders) == {"cli.py"}
