"""The `codeagent` command line (ADR 0004 section 11, Amendment 18).

Exposes exactly one subcommand, the maintenance-only `reconcile`. It
never runs a task. A later `solve` subcommand is one more subparser and
handler; nothing here needs to change for it.

Output is fixed categorical text: identifiers (`repo_key`,
`lifecycle_id`, `maintenance_id`), outcome tokens, fixed detail strings,
and categorical resource summaries. It never prints a host path, the
`--repo` value, raw exception text, Git or Docker output, or the
operator's abandonment reason.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from enum import IntEnum, unique

from .abandonment import validate_reason
from .maintenance import (
    AbandonOutcome,
    InvalidRepositoryError,
    ReconcileResult,
    run_abandon,
    run_reconcile,
)

_LIFECYCLE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REPOSITORY_HINT = 'run "codeagent reconcile --repo <path>" for repository-wide status'


@unique
class ExitCode(IntEnum):
    CLEAN = 0
    INTERNAL_ERROR = 1
    INVALID_INVOCATION = 2
    UNRESOLVED_ACKNOWLEDGED = 3
    BLOCKED = 4


_RESULT_EXIT = {
    ReconcileResult.CLEAN: ExitCode.CLEAN,
    ReconcileResult.UNRESOLVED_ACKNOWLEDGED: ExitCode.UNRESOLVED_ACKNOWLEDGED,
    ReconcileResult.BLOCKED: ExitCode.BLOCKED,
}


class _SanitizingParser(argparse.ArgumentParser):
    """argparse's own diagnostics quote whatever the operator typed (an
    unknown subcommand, an unrecognized argument, a malformed option). This
    parser never prints them: every grammar error is fixed text plus the
    parser's own usage line (built from its definition only), exit 2.
    Subparsers inherit this class through `add_subparsers`."""

    def error(self, message: str):  # noqa: ARG002 - the message may quote operator input
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments; see --help\n")

    def fail(self, fixed_message: str):
        """A post-parse validation failure with this module's own fixed text."""
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {fixed_message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _SanitizingParser(prog="codeagent", description="CodeAgent maintenance commands.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    reconcile = commands.add_parser(
        "reconcile",
        help="reconcile, inspect, or abandon this repository's recorded CodeAgent runs (never runs a task)",
        description=(
            "Reconcile dead CodeAgent runs for one repository (--dry-run: inspect only, write nothing), "
            "or record an administrative abandonment of one run. Abandonment never removes, adopts, or "
            "changes any container, worktree, registration, ref, or projection. --acknowledge-unresolved "
            "permanently records that resources may remain; the repository is never reported clean again."
        ),
    )
    reconcile.add_argument("--repo", required=True, metavar="PATH", help="the repository")
    reconcile.add_argument("--dry-run", action="store_true", help="inspect and report only; write nothing")
    reconcile.add_argument("--abandon", metavar="LIFECYCLE_ID", help="abandon one recorded run")
    reconcile.add_argument(
        "--acknowledge-unresolved",
        action="store_true",
        help="with --abandon: record ABANDONED_UNRESOLVED although resources remain or cannot be inspected",
    )
    reconcile.add_argument(
        "--reason", metavar="TEXT", help="required with --acknowledge-unresolved; stored only in the marker"
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # Fixed messages only: the offending value (in particular the reason)
    # is never echoed.
    if args.abandon is not None and args.dry_run:
        parser.fail("--dry-run cannot be combined with --abandon")
    if args.acknowledge_unresolved and args.abandon is None:
        parser.fail("--acknowledge-unresolved requires --abandon")
    if args.reason is not None and not args.acknowledge_unresolved:
        parser.fail("--reason requires --acknowledge-unresolved")
    if args.acknowledge_unresolved and args.reason is None:
        parser.fail("--acknowledge-unresolved requires --reason")
    if args.abandon is not None and not _LIFECYCLE_ID_RE.fullmatch(args.abandon):
        parser.fail("LIFECYCLE_ID must be exactly 32 lowercase hexadecimal characters")
    if args.reason is not None:
        try:
            validate_reason(args.reason)
        except ValueError:
            parser.fail(
                "--reason must be nonempty, at most 256 UTF-8 bytes, and contain no control, format, "
                "line/paragraph-separator, or surrogate characters"
            )


def _remaining_line(summary: dict) -> str:
    return " ".join(f"{key}={value}" for key, value in summary.items())


def _print_reconcile(report) -> None:
    mode = "dry run" if report.dry_run else "reconcile"
    print(f"codeagent reconcile ({mode}): result={report.result.value}")
    print(f"repository: {report.repo_key or 'unknown'}")
    if report.no_recorded_state:
        print("state: no_recorded_state")
    if report.maintenance_id and report.trace_incomplete:
        print(f"maintenance trace: {report.maintenance_id} (incomplete)")
    else:
        print(f"maintenance trace: {report.maintenance_id or 'none'}")
    if report.blocked_reason:
        print(f"blocked: {report.blocked_reason}")
    if report.mismatched_fields:
        print("mismatched identity fields: " + ", ".join(sorted(report.mismatched_fields)))
    for entry in report.entries:
        print(f"entry {entry.lifecycle_id}: {entry.outcome.value} ({entry.detail})")
        remaining = getattr(entry, "remaining", None)
        if remaining is not None:
            print(f"  remaining: {_remaining_line(remaining.to_summary())}")
            if remaining.cleanup_unconfirmed:
                print("  inspection cleanup unconfirmed: " + ", ".join(remaining.cleanup_unconfirmed))
        if entry.abandonment_temp_leftovers:
            print(f"  abandonment temporary leftovers: {entry.abandonment_temp_leftovers}")
    for entry in report.entries:
        if entry.outcome.value == "skipped_abandoned_unresolved":
            print(
                f"WARNING: run {entry.lifecycle_id} was abandoned with acknowledged unresolved resources "
                f"({_remaining_line(entry.abandonment_remaining or {})}); resources may remain leaked and "
                "this repository is never reported clean while the entry exists",
                file=sys.stderr,
            )
    if report.unconfirmed_stages:
        print("unconfirmed: " + ", ".join(report.unconfirmed_stages))


def _print_abandon(report) -> None:
    print(f"codeagent reconcile --abandon: outcome={report.outcome.value}")
    print(f"lifecycle: {report.lifecycle_id}")
    if report.refusal:
        print(f"refusal: {report.refusal}")
    if report.mismatched_fields:
        print("mismatched identity fields: " + ", ".join(sorted(report.mismatched_fields)))
    if report.remaining is not None:
        print(f"remaining: {_remaining_line(report.remaining.to_summary())}")
    if report.abandonment_temp_leftovers:
        print(f"abandonment temporary leftovers: {report.abandonment_temp_leftovers}")
    print(f"maintenance trace: {report.maintenance_id or 'none'}")
    if report.marker_publication:
        print(f"marker publication: {report.marker_publication}")
    if report.unconfirmed_stages:
        print("unconfirmed: " + ", ".join(report.unconfirmed_stages))
    print(_REPOSITORY_HINT)


def _abandon_exit(report) -> ExitCode:
    if report.unconfirmed_stages:
        return ExitCode.BLOCKED
    if report.outcome is AbandonOutcome.RECORDED_ABANDONED:
        return ExitCode.CLEAN
    if report.outcome is AbandonOutcome.RECORDED_UNRESOLVED:
        return ExitCode.UNRESOLVED_ACKNOWLEDGED
    return ExitCode.BLOCKED


def _dispatch(args: argparse.Namespace) -> ExitCode:
    if args.abandon is not None:
        report = run_abandon(
            args.repo, args.abandon, acknowledge_unresolved=args.acknowledge_unresolved, reason=args.reason
        )
        _print_abandon(report)
        return _abandon_exit(report)
    report = run_reconcile(args.repo, dry_run=args.dry_run)
    _print_reconcile(report)
    return _RESULT_EXIT[report.result]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # SystemExit(2) on a grammar error
    _validate(parser, args)
    try:
        return int(_dispatch(args))
    except InvalidRepositoryError as exc:
        print(f"codeagent: --repo does not name a supported Git repository ({exc.reason})", file=sys.stderr)
        return int(ExitCode.INVALID_INVOCATION)
    except Exception as exc:  # noqa: BLE001 - fixed text only; never str(exc) or a traceback
        print(f"codeagent: internal error ({type(exc).__name__})", file=sys.stderr)
        stages = getattr(exc, "codeagent_unconfirmed_stages", ())
        if stages:
            # Categorical stage names attached by `maintenance` only.
            print("codeagent: cleanup unconfirmed: " + ", ".join(stages), file=sys.stderr)
        return int(ExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    sys.exit(main())
