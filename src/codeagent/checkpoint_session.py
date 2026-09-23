"""Milestone 2, slice 2B-1: the in-memory checkpoint transition record.

This module is **deliberately unwired**. It is not imported by
`controller.py`, `patch.py`, `workspace.py`, or `executor.py`; it emits
no events, constructs no `OperationalError`, touches no filesystem,
takes no lock, and performs no Git operation of its own. Every Git
effect it has is one call into the `CheckpointRef` primitive it wraps.
Constructing a `CheckpointSession` therefore makes **no production
lifecycle guarantee** — it is the state machine that slice 2B-2 will
drive, reviewed on its own first.

What it is
----------

ADR 0003 Amendment 1 requires a write-ahead record of checkpoint-ref
intent around every create/advance/delete, and ADR 0004 section 5
defines that record's exact shape and legal field combinations. ADR
0003 Amendment 1's "Milestone boundaries" section then scopes Milestone
2 precisely: *"Within Milestone 2 the `checkpoint_ref` transition
record (ADR 0004 section 5) is held only in memory by the running
process."* That is this module.

`CheckpointTransition` uses ADR 0004's field names verbatim
(`accepted_sha`, `expected_old_sha`, `proposed_new_sha`) and enforces
its table, so Milestone 3's durable lifecycle store can serialize this
object rather than redesigning it. Nothing here persists anything.

How collapses are decided
-------------------------

Every collapse is driven by the pair `(operation, MutationOutcome)` —
never by `CheckpointRefFailure` alone, and never by re-observing the
ref. The primitive already classifies the outcome during its own
failure handling and now publishes it, so a second observation would
be both redundant and racy.

    create   APPLIED                          -> present(initial)
    create   UNCHANGED (confirmed absent)     -> absent
    create   UNEXPECTED/SYMBOLIC/UNKNOWN      -> stays creating
    advance  APPLIED                          -> present(new)
    advance  UNCHANGED (confirmed at old)     -> present(old)
    advance  UNEXPECTED/SYMBOLIC/UNKNOWN      -> stays advancing
    delete   APPLIED (confirmed absent)       -> absent
    delete   anything else                    -> stays removing

Two rules are load-bearing rather than incidental:

- **`TRANSACTION_CLEANUP_UNCONFIRMED` never permits a collapse**,
  whatever was observed. A Git child that could not be confirmed dead
  may still hold the ref's lock, so an observation taken around it can
  be invalidated immediately afterwards. The primitive already forces
  that reason to `UNKNOWN`; this module re-applies the rule itself so
  the guarantee does not depend on the collaborator getting it right.
- **An error never collapses to an applied state.** `APPLIED` is
  signalled only by a mutation returning normally. An error that
  somehow claimed `APPLIED` would be a collaborator bug, and treating
  it as authoritative would be guessing; it is degraded to `UNKNOWN`.

A session left in a transitional state (`creating`, `advancing`,
`removing`) refuses every further operation instead of guessing which
value the ref might hold — `removing` included. A delete retry from
`removing` looks safe (same operation, same expected value), but this
record does not retain whether that `removing` arose from a confirmed-
unchanged failure or from an unknown process/lock outcome; only the
first could be retried blindly. Resolving that needs a fresh
inspection of the live ref, which is ADR 0004 section 8 dead-run
reconciliation (Milestone 3), not this module.

Failures propagate as the original `CheckpointRefError`, with the
record already collapsed. This module never translates one into an
`OperationalError`: preserving one exception object per occurrence is
what lets slice 2B-2 build exactly one `OperationalError` with one
`error_id` for it (ADR 0003 Amendment 1's "one occurrence, one
error"), the same split `checkpoint_ref.py` and `workspace.py` already
use.

Recorded for slice 2B-2 (not implemented here; `errors.py` and
`events.py` are deliberately untouched by this slice) — the approved
mapping from this module's outcomes onto the error taxonomy:

    CHECKPOINT_REF_UPDATE_REJECTED   genuine compare-and-swap rejection
                                     only (COMPARE_AND_SWAP_REJECTED)
    CHECKPOINT_REF_OPERATION_FAILED  known command/protocol failure
                                     with confirmed UNCHANGED state
    CHECKPOINT_REF_UNEXPECTED_STATE  UNEXPECTED or SYMBOLIC
    CHECKPOINT_REF_OUTCOME_UNKNOWN   UNKNOWN
    WORKSPACE_ENTRY_GATE_FAILED      entry-gate refusal (2B-2)
    LIFECYCLE_CLEANUP_UNCONFIRMED    unconfirmed terminal cleanup (2B-2)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol

from codeagent.checkpoint_ref import (
    CheckpointRef,
    CheckpointRefError,
    CheckpointRefFailure,
    MutationOutcome,
)

# Structural shape only: the two object-id lengths Git defines. The
# *repository's* actual format is enforced separately by
# CheckpointSession against its CheckpointRef, so a transition record
# can still be constructed and validated standalone (in a test, or by a
# future reader of a persisted record) without a live repository.
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@unique
class CheckpointIntent(str, Enum):
    """The write-ahead intent of ADR 0004 section 5's transition record.

    `absent` and `present` are the two stable states; `creating`,
    `advancing`, and `removing` are transitional and mean a ref
    mutation was attempted and has not been confirmed either way.
    """

    ABSENT = "absent"
    CREATING = "creating"
    PRESENT = "present"
    ADVANCING = "advancing"
    REMOVING = "removing"


_TRANSITIONAL_INTENTS: frozenset[CheckpointIntent] = frozenset(
    {CheckpointIntent.CREATING, CheckpointIntent.ADVANCING, CheckpointIntent.REMOVING}
)


@unique
class CheckpointSessionFailure(str, Enum):
    """Categorical reason a session refused an operation of its own
    accord — distinct from a `CheckpointRefFailure`, which describes a
    Git-level failure of an operation that was actually attempted."""

    INCOMPATIBLE_OPERATION = "incompatible_operation"
    MALFORMED_OID = "malformed_oid"


class CheckpointSessionError(Exception):
    """A session refused an operation before attempting any mutation.

    `reason` is the stable, matchable identifier; `message` is fixed,
    sanitized categorical text (intent names and validated hex object
    ids only) and must never be parsed.
    """

    def __init__(self, reason: CheckpointSessionFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _is_all_zero_oid(value: str) -> bool:
    return set(value) == {"0"}


def _require_oid_shape(name: str, value: str) -> None:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise ValueError(
            f"{name} must be exactly 40 or 64 lowercase hexadecimal characters"
        )
    if _is_all_zero_oid(value):
        # ADR 0004 section 5: null means "no ref"; the zero OID appears
        # only in Git argv (checkpoint_ref.ObjectFormat.zero_oid),
        # never as a persisted or in-memory transition-record value.
        raise ValueError(f"{name} must not be the all-zero object id")


@dataclass(frozen=True)
class CheckpointTransition:
    """One write-ahead checkpoint-ref transition record (ADR 0004
    section 5). `CheckpointSession` always holds it in memory; when
    configured with a `CheckpointTransitionPublisher` (Slice 3B-3), it
    is also synchronously offered to that publication boundary.

    Field names are ADR 0004's verbatim, so Milestone 3's durable
    lifecycle store serializes this object rather than translating it.
    `None` means "no ref"; the zero OID is never a value here — it
    exists only as a Git argument inside `CheckpointRef`.

    The legal combinations are exactly ADR 0004 section 5's table, and
    every other combination is refused at construction:

        intent     accepted_sha  expected_old_sha  proposed_new_sha
        absent     None          None              None
        creating   None          None              <initial>
        present    <current>     None              None
        advancing  A             A                 B  (B != A)
        removing   F             F                 None
    """

    intent: CheckpointIntent
    accepted_sha: str | None = None
    expected_old_sha: str | None = None
    proposed_new_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, CheckpointIntent):
            raise ValueError(f"intent must be a CheckpointIntent, got {self.intent!r}")
        for name in ("accepted_sha", "expected_old_sha", "proposed_new_sha"):
            value = getattr(self, name)
            if value is not None:
                _require_oid_shape(name, value)

        present_fields = (
            self.accepted_sha is not None,
            self.expected_old_sha is not None,
            self.proposed_new_sha is not None,
        )
        expected = _REQUIRED_FIELD_PRESENCE[self.intent]
        if present_fields != expected:
            raise ValueError(
                f"intent {self.intent.value!r} requires "
                f"(accepted_sha, expected_old_sha, proposed_new_sha) presence "
                f"{expected!r}, got {present_fields!r}"
            )

        if self.intent is CheckpointIntent.ADVANCING:
            if self.expected_old_sha != self.accepted_sha:
                raise ValueError(
                    "an advancing record's expected_old_sha must equal its accepted_sha"
                )
            if self.proposed_new_sha == self.accepted_sha:
                raise ValueError(
                    "an advancing record's proposed_new_sha must differ from its accepted_sha"
                )
        if self.intent is CheckpointIntent.REMOVING:
            if self.expected_old_sha != self.accepted_sha:
                raise ValueError(
                    "a removing record's expected_old_sha must equal its accepted_sha"
                )

    @property
    def is_transitional(self) -> bool:
        return self.intent in _TRANSITIONAL_INTENTS


# (accepted_sha, expected_old_sha, proposed_new_sha) presence per intent.
_REQUIRED_FIELD_PRESENCE: dict[CheckpointIntent, tuple[bool, bool, bool]] = {
    CheckpointIntent.ABSENT: (False, False, False),
    CheckpointIntent.CREATING: (False, False, True),
    CheckpointIntent.PRESENT: (True, False, False),
    CheckpointIntent.ADVANCING: (True, True, True),
    CheckpointIntent.REMOVING: (True, True, False),
}

ABSENT_TRANSITION = CheckpointTransition(intent=CheckpointIntent.ABSENT)


class CheckpointTransitionPublisher(Protocol):
    """Optional structural seam (Milestone 3 Slice 3B-3, ADR 0004
    Amendment 4): anything with a `publish(transition)` method. This
    module never imports a concrete implementation -- `lifecycle_store.
    LifecycleCheckpointRefPublisher` is the one Milestone 3 provides,
    constructed and injected by whichever later slice wires a real
    `LifecycleLease` in. `publish()` is called synchronously, in the
    exact places `self._transition` itself changes. A raised exception
    is never wrapped, translated, or swallowed by this module. The one
    exception is during confirmed-`UNCHANGED` recovery: this module
    deliberately catches a publisher exception there solely to raise
    that same exception instance explicitly `from` the existing
    `CheckpointRefError`, establishing projection-consistency failure
    dominance without changing the publisher exception's type or
    identity -- see `establish`/`advance`/`delete` for the precise
    ordering and failure semantics.
    """

    def publish(self, transition: CheckpointTransition) -> None: ...


class CheckpointSession:
    """Drives one lifecycle's checkpoint ref through write-ahead
    intent. The transition record is always held in memory; without a
    configured `transition_publisher` it remains memory-only, and with
    one, every change is also synchronously offered to the configured
    publication boundary at the documented write-ahead/collapse
    moments.

    Each operation records its intent first, calls the wrapped
    `CheckpointRef` exactly once, then collapses the record according to
    the confirmed `MutationOutcome`. On failure the record is collapsed
    (or deliberately left transitional) and the original
    `CheckpointRefError` is re-raised unchanged, so the caller can
    translate that single occurrence into a single `OperationalError`.

    An optional `transition_publisher` (Slice 3B-3) is called every time
    `self._transition` changes -- before the corresponding Git mutation
    for a fresh transitional intent, and again after a confirmed
    collapse -- so a caller with one configured gets a durable write-
    ahead record for free, at exactly the ADR 0004 section 5 moments
    that record requires. Defaulting to `None` preserves every existing
    caller's behavior exactly: `_publish` no-ops when unset.
    """

    def __init__(
        self,
        checkpoint_ref: CheckpointRef,
        *,
        transition_publisher: CheckpointTransitionPublisher | None = None,
    ) -> None:
        self._ref = checkpoint_ref
        self._transition = ABSENT_TRANSITION
        self._transition_publisher = transition_publisher

    def _publish(self, transition: CheckpointTransition) -> None:
        if self._transition_publisher is not None:
            self._transition_publisher.publish(transition)

    @property
    def transition(self) -> CheckpointTransition:
        return self._transition

    @property
    def intent(self) -> CheckpointIntent:
        return self._transition.intent

    @property
    def accepted_sha(self) -> str | None:
        """The last accepted checkpoint, or None when there is none.

        Only meaningful as "the value the ref is believed to hold" when
        the record is not transitional — callers that need certainty
        must check `transition.is_transitional` first.
        """
        return self._transition.accepted_sha

    @property
    def lifecycle_id(self) -> str:
        return self._ref.lifecycle_id

    @property
    def ref_name(self) -> str:
        return self._ref.ref_name

    def establish(self, initial_sha: str) -> None:
        """Create the ref at the starting commit (ADR 0003 Amendment 1
        point 4's "establish, then rely"). Legal only from `absent`.

        Assignment-first ordering (Slice 3B-3): the transitional intent
        is assigned to `self._transition` and published *before*
        `self._ref.create()` is ever called. If publication raises,
        `create()` is never reached, the publication exception
        propagates unchanged, and `self._transition` remains the
        transitional intent just assigned -- no rollback, no retry.
        """
        self._require_intent(
            CheckpointIntent.ABSENT,
            operation="establish",
        )
        self._validate_against_repository("initial_sha", initial_sha)

        transitional = CheckpointTransition(
            intent=CheckpointIntent.CREATING, proposed_new_sha=initial_sha
        )
        self._transition = transitional
        self._publish(transitional)

        try:
            self._ref.create(initial_sha)
        except CheckpointRefError as exc:
            if self._effective_outcome(exc) is MutationOutcome.UNCHANGED:
                # Confirmed still absent: nothing was created.
                recovery = ABSENT_TRANSITION
                self._transition = recovery
                try:
                    self._publish(recovery)
                except Exception as publish_exc:
                    # Projection-consistency failure dominance (distinct
                    # from the repository's cleanup-dominance rule):
                    # this durable-record failure, not the original Git
                    # error, is what the caller must react to.
                    # Deliberately chained, not left to incidental
                    # `__context__`.
                    raise publish_exc from exc
            raise
        collapse = CheckpointTransition(
            intent=CheckpointIntent.PRESENT, accepted_sha=initial_sha
        )
        self._transition = collapse
        self._publish(collapse)

    def advance(self, new_sha: str) -> None:
        """Move the ref to a newly accepted checkpoint by compare-and-
        swap against the currently accepted one. Legal only from
        `present`.

        Assignment-first ordering identical to `establish()`: see that
        method's docstring for the exact publication-failure semantics.
        """
        self._require_intent(CheckpointIntent.PRESENT, operation="advance")
        self._validate_against_repository("new_sha", new_sha)

        accepted = self._transition.accepted_sha
        assert accepted is not None  # guaranteed by the `present` record shape
        if new_sha == accepted:
            raise CheckpointSessionError(
                CheckpointSessionFailure.INCOMPATIBLE_OPERATION,
                "advance requires a new_sha that differs from the accepted checkpoint",
            )

        transitional = CheckpointTransition(
            intent=CheckpointIntent.ADVANCING,
            accepted_sha=accepted,
            expected_old_sha=accepted,
            proposed_new_sha=new_sha,
        )
        self._transition = transitional
        self._publish(transitional)

        try:
            self._ref.advance(expected_old_oid=accepted, new_oid=new_sha)
        except CheckpointRefError as exc:
            if self._effective_outcome(exc) is MutationOutcome.UNCHANGED:
                # Confirmed still at the old value: the commit is
                # unaccepted, and the previous checkpoint still stands.
                recovery = CheckpointTransition(
                    intent=CheckpointIntent.PRESENT, accepted_sha=accepted
                )
                self._transition = recovery
                try:
                    self._publish(recovery)
                except Exception as publish_exc:
                    raise publish_exc from exc
            raise
        collapse = CheckpointTransition(
            intent=CheckpointIntent.PRESENT, accepted_sha=new_sha
        )
        self._transition = collapse
        self._publish(collapse)

    def delete(self) -> None:
        """Delete the ref by compare-and-swap against the accepted
        checkpoint. Legal only from `present`; a no-op from `absent`
        (nothing was ever created, so there is nothing to confirm, no
        Git call is made, and nothing is published).

        Refused from every transitional state, `removing` included.
        Retrying a delete from `removing` looks harmless — same
        operation, same expected value — but this record does not
        retain *why* it is in `removing`: a confirmed-unchanged failure
        and an unknown process/lock outcome collapse to the same
        `removing` record, and only the first would be safe to retry
        blindly. Distinguishing them requires a fresh inspection of the
        live ref, which is ADR 0004 section 8 dead-run reconciliation —
        Milestone 3 work, and the owner of any later deletion retry.

        Assignment-first ordering identical to `establish()`: the
        `removing` transitional intent is published before
        `self._ref.delete()` is called. Unlike `establish()`/
        `advance()`, there is no recovery-collapse branch on failure —
        any failure here leaves the record in `removing` (already
        published) for terminal cleanup-unconfirmed handling, and the
        original error propagates unchanged with no further publish
        attempt.
        """
        if self._transition.intent is CheckpointIntent.ABSENT:
            return
        self._require_intent(CheckpointIntent.PRESENT, operation="delete")

        accepted = self._transition.accepted_sha
        assert accepted is not None  # guaranteed by the `present` record shape
        transitional = CheckpointTransition(
            intent=CheckpointIntent.REMOVING,
            accepted_sha=accepted,
            expected_old_sha=accepted,
        )
        self._transition = transitional
        self._publish(transitional)

        self._ref.delete(expected_oid=accepted)
        # Only a normal return means APPLIED (confirmed absent). Any
        # failure leaves the record in `removing` for terminal
        # cleanup-unconfirmed handling, and propagates unchanged.
        collapse = ABSENT_TRANSITION
        self._transition = collapse
        self._publish(collapse)

    def _require_intent(self, expected: CheckpointIntent, *, operation: str) -> None:
        if self._transition.intent is not expected:
            raise CheckpointSessionError(
                CheckpointSessionFailure.INCOMPATIBLE_OPERATION,
                f"{operation} is not legal from intent "
                f"{self._transition.intent.value!r} (requires {expected.value!r})",
            )

    def _validate_against_repository(self, name: str, value: str) -> None:
        """Validate an object id against the *repository's* actual
        object format, not merely against a generic hex shape — a
        40-character id is malformed in a SHA-256 repository and vice
        versa. Also refuses the all-zero object id: ADR 0004 section 5
        reserves it strictly for Git argv (`ObjectFormat.zero_oid`),
        never as an operator-supplied or persisted transition value —
        checked here too, not only in `CheckpointTransition`'s own
        `__post_init__`, so `establish()`/`advance()` never let a raw
        `ValueError` escape past this module's own sanitized
        `CheckpointSessionError` boundary."""
        expected_length = self._ref.object_format.hex_length
        if (
            not isinstance(value, str)
            or len(value) != expected_length
            or not re.fullmatch(r"[0-9a-f]+", value)
            or _is_all_zero_oid(value)
        ):
            raise CheckpointSessionError(
                CheckpointSessionFailure.MALFORMED_OID,
                f"{name} must be exactly {expected_length} lowercase hexadecimal characters "
                f"for this repository's {self._ref.object_format.value} object format",
            )

    @staticmethod
    def _effective_outcome(exc: CheckpointRefError) -> MutationOutcome:
        """The outcome this session is willing to act on.

        `TRANSACTION_CLEANUP_UNCONFIRMED` is forced to `UNKNOWN`: an
        unconfirmed child may still hold the ref lock, so no observation
        around it can justify a collapse. The primitive already applies
        this rule; re-applying it here means the guarantee does not
        depend on the collaborator.

        An error claiming `APPLIED` is likewise degraded to `UNKNOWN` —
        `APPLIED` is signalled by a normal return and by nothing else,
        so an error asserting it is a collaborator bug, and collapsing
        on it would be guessing.
        """
        if exc.reason is CheckpointRefFailure.TRANSACTION_CLEANUP_UNCONFIRMED:
            return MutationOutcome.UNKNOWN
        if exc.outcome is MutationOutcome.APPLIED:
            return MutationOutcome.UNKNOWN
        return exc.outcome
