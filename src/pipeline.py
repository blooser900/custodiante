"""Core request -> decision -> payment pipeline.

Extracted out of src/main.py so the CLI and the HTTP API (src/api.py) share
one implementation instead of two copies that could drift apart.
"""

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv

# Must run before the src.* imports below: jev_client.py and
# fallback_classifier.py read their config as module-level constants at
# import time.
load_dotenv()

from src.audit.logger import AuditEntry, FailedAttempt, _current_config_snapshot, build_entry
from src.audit.store import append_entry, read_latest_per_request
from src.decision_engine.engine import DecisionEngine, DecisionScores, Evidence, ScoreResult
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.jev_client import JevDecisionEngine
from src.decision_engine.mock_engine import MockDecisionEngine
from src.decision_engine.system_one import EngineResponseError
from src.history import compute_history
from src.money import parse_usdc
from src.payments.arc_client import (
    ArcPaymentClient,
    PaymentUnconfirmedError,
    payment_idempotency_key,
)
from src.payments.escrow import EscrowClient, EscrowError
from src.payees import PayeeRegistry
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory

logger = logging.getLogger(__name__)

# Engines selectable through the DECISION_ENGINE environment variable.
ENGINES: dict[str, type[DecisionEngine]] = {
    "kev": TypeSafeCompatibleEngine,  # self-hosted, the default
    "jev": JevDecisionEngine,  # real Jev API (needs JEV_API_KEY)
    "mock": MockDecisionEngine,  # offline keyword heuristics, demo only
}

# A payment_status meaning "sent, outcome unknown". Nothing writes this today
# (the pending row carries a `payment_attempt` marker instead, keeping
# payment_status for real outcomes), but `_in_flight` honours it so a future
# implementation cannot silently lose the double-spend guard.
_PENDING_STATUS = "pending"


def build_engine_from_env() -> DecisionEngine:
    """Build the decision engine named by DECISION_ENGINE (default: kev).

    Read at call time rather than import time so it can be changed in .env
    without code edits. An unknown name fails loudly: silently falling back
    to some other engine would make audit entries misleading.
    """
    choice = os.environ.get("DECISION_ENGINE", "kev").strip().lower()
    if choice not in ENGINES:
        raise RuntimeError(
            f"Unknown DECISION_ENGINE {choice!r}. Valid options: {', '.join(sorted(ENGINES))}."
        )
    return ENGINES[choice]()


# Callable returning a ready engine. Kept as a module-level name because
# tests (and any caller) can patch it to inject a fake engine.
DEFAULT_ENGINE_FACTORY = build_engine_from_env


class DuplicateRequestError(RuntimeError):
    """A request_id that already has an entry in the audit log was resubmitted."""


class AlreadyReviewedError(RuntimeError):
    """An escalated request was already approved or denied by a human."""


class PaymentInFlightError(RuntimeError):
    """An operation for this request left our hands without a final outcome.

    A transfer was sent (or an escrow job opened) and the process died before
    the outcome could be recorded. Paying again here is precisely the
    double-spend this guard exists to prevent, so the caller must reconcile
    against Circle/Arc instead.
    """


class PaymentRecoveryError(RuntimeError):
    """Reconciling or retrying a payment was refused or could not be done."""


class PaymentNotFoundError(PaymentRecoveryError):
    """No such request in the audit log."""


class PaymentStateError(PaymentRecoveryError):
    """The request is not in a state this recovery action applies to."""


class PaymentLookupError(PaymentRecoveryError):
    """Circle could not be asked, or its answer cannot be trusted.

    Nothing was recorded and nothing was sent: when in doubt the request stays
    exactly as it was.
    """


class MilestoneError(RuntimeError):
    """A milestone action is not valid for the request's current state."""


class MilestoneNotFoundError(MilestoneError):
    """No audit entry exists for the given request_id."""


# One lock per request_id. Serializes "check the log -> pay -> append to the
# log" for the same request, so two simultaneous submissions (a double click,
# a client retry) cannot both pass the check and both pay. Requests with
# different ids never block each other. In-process only: this protects a
# single uvicorn worker, which is how this app is run.
_locks_guard = threading.Lock()
_request_locks: dict[str, threading.Lock] = {}


def _lock_for(request_id: str) -> threading.Lock:
    with _locks_guard:
        return _request_locks.setdefault(request_id, threading.Lock())


@dataclass
class PaymentOutcome:
    """What happened when we tried to pay an approved request."""

    # "executed" | "failed" | "skipped" | "unconfirmed" for plain transfers;
    # "unconfirmed" means the transfer may have been sent and its outcome could
    # not be established: it is NOT a failure and keeps the in-flight marker.
    # Milestone escrow
    # adds "escrow_open" (job created, contractor has not set a budget yet),
    # "escrowed" (funds locked in the contract), "refunded".
    status: str
    detail: str | None = None
    result: dict | None = None
    escrow_job_id: int | None = None
    escrow_status: str | None = None

    @property
    def executed(self) -> bool:
        return self.status == "executed"


def _attempt_payment(
    request: PaymentRequest, amount_usdc: float, memo: str, arc_client=None, attempt: int = 1
) -> PaymentOutcome:
    """Try to pay `amount_usdc` for an already-approved request.

    Never raises: a skipped or failed payment is a recorded outcome, not a
    crash, because the decision itself was already made and must be logged.
    """
    if not request.counterparty_wallet:
        return PaymentOutcome("skipped", "No counterparty_wallet on the request.")
    if not os.environ.get("CIRCLE_API_KEY"):
        return PaymentOutcome("skipped", "CIRCLE_API_KEY is not set.")

    try:
        arc_client = arc_client or ArcPaymentClient()
        result = arc_client.send_payment(
            destination_wallet=request.counterparty_wallet,
            amount_usdc=amount_usdc,
            memo=memo,
            # The same request always produces the same key, so a repeat of
            # this call (a retry, a double click that slipped past the lock, a
            # replay after a crash) cannot make Circle move the money twice.
            idempotency_key=payment_idempotency_key(request.request_id, attempt),
            ref_id=request.request_id,
        )
    except PaymentUnconfirmedError as e:
        # Must come before the broad handler below: reporting this as
        # "failed" would invite a retry of a transfer that may have completed.
        return PaymentOutcome(
            "unconfirmed",
            f"{type(e).__name__}: {e}",
            result={"transaction_id": e.transaction_id} if e.transaction_id else None,
        )
    except Exception as e:  # noqa: BLE001 -- see below
        # Deliberately broad: whatever went wrong (bad credentials, network,
        # a DENIED transfer, a malformed address), the audit log must record
        # that an approved payment did not go out, and why.
        return PaymentOutcome("failed", f"{type(e).__name__}: {e}")

    return PaymentOutcome("executed", result=result)


def escrow_enabled() -> bool:
    """Kill switch: ESCROW_ENABLED=false pays milestones directly instead."""
    return os.environ.get("ESCROW_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _uses_escrow(request: PaymentRequest) -> bool:
    return request.category == RequestCategory.CONTRACTOR_MILESTONE and escrow_enabled()


def provider_is_ours(request: PaymentRequest) -> bool:
    """True when the contractor's wallet is one WE control (demo setups).

    Only then can we sign the contractor's side of the job (set the budget,
    submit the deliverable). With a real contractor they do both themselves.
    """
    ours = os.environ.get("ESCROW_PROVIDER_WALLET_ADDRESS", "")
    return bool(ours) and ours.lower() == (request.counterparty_wallet or "").lower()


def _open_escrow(request: PaymentRequest, amount_usdc: float, escrow=None) -> PaymentOutcome:
    """Open a milestone escrow for an already-approved request.

    Never raises (same contract as _attempt_payment). The job's on-chain
    description is only the request id: no free text from the request is
    written to a public chain.
    """
    if not request.counterparty_wallet:
        return PaymentOutcome("skipped", "No counterparty_wallet on the request.")
    if not os.environ.get("CIRCLE_API_KEY"):
        return PaymentOutcome("skipped", "CIRCLE_API_KEY is not set.")

    job_id: int | None = None
    try:
        escrow = escrow or EscrowClient()
        job_id, create_tx = escrow.create_job(request.counterparty_wallet, f"custodiante:{request.request_id}")
        if not provider_is_ours(request):
            return PaymentOutcome(
                "escrow_open",
                f"Escrow job {job_id} created; waiting for the contractor to set the budget.",
                result=create_tx,
                escrow_job_id=job_id,
                escrow_status="open",
            )
        escrow.set_budget(job_id, amount_usdc, provider_wallet=request.counterparty_wallet)
        fund_tx = escrow.fund(job_id, amount_usdc)
    except Exception as e:  # noqa: BLE001 -- recorded, never raised (see _attempt_payment)
        # If the job was already created it still exists on-chain: keep its
        # id so an operator can retry funding or reject it from the dashboard.
        return PaymentOutcome(
            "failed",
            f"{type(e).__name__}: {e}",
            escrow_job_id=job_id,
            escrow_status="open" if job_id is not None else None,
        )
    return PaymentOutcome(
        "escrowed", result=fund_tx, escrow_job_id=job_id, escrow_status="funded"
    )


def _settle(
    request: PaymentRequest, amount_usdc: float, memo: str, arc_client=None, escrow=None, attempt: int = 1
) -> PaymentOutcome:
    """Pay an approved request: via escrow for contractor milestones,
    otherwise as a direct transfer."""
    if _uses_escrow(request):
        return _open_escrow(request, amount_usdc, escrow=escrow)
    return _attempt_payment(request, amount_usdc, memo, arc_client, attempt=attempt)


def _engine_name(engine) -> str:
    name = getattr(engine, "name", None)
    return name if isinstance(name, str) else type(engine).__name__


def _in_flight(existing: AuditEntry | None) -> bool:
    """True when `existing` says an operation left our hands without an outcome.

    `existing` must be the NEWEST row of the request (see `_latest_for`): a
    request can have several rows (a pending row plus its outcome, or a failed
    attempt) and only the latest says where things stand.

    The marker is cleared by a FINAL outcome (executed, failed, skipped...).
    The one outcome that keeps it is "unconfirmed" -- the transfer may have
    gone out and we could not find out -- which is exactly the state in which
    paying again is the double-spend.
    """
    if existing is None:
        return False
    marker = getattr(existing, "payment_attempt", None)
    status = getattr(existing, "payment_status", None)
    # A marker still present means nothing has replaced it with an outcome.
    return marker is not None or status == _PENDING_STATUS


def _latest_for(entries: list[AuditEntry], request_id: str) -> AuditEntry | None:
    """The newest row of `request_id` among `entries`.

    Newest means the timestamp; a TIE is broken by position in `entries`, with
    the LAST matching row winning. That assumes the caller's list is in log
    order or newest-first -- either way the relevant row is the one its order
    puts last among equals. That tie-break
    is not cosmetic. An approved payment writes two rows with no delay between
    them -- the pending row carrying the in-flight marker, then its outcome --
    and when the clock is coarser than those two writes they share a timestamp.
    `max()` over the timestamp alone then returned whichever row came first, so
    a legitimately PAID request resolved to its own pending row, `_in_flight`
    read it as money still in the air, and the only way forward was a manual
    reconciliation.
    """
    mine = [e for e in entries if e.request.request_id == request_id]
    if not mine:
        return None
    return max(
        enumerate(mine), key=lambda pair: (pair[1].timestamp, pair[0])
    )[1]


def _attempt_description(request: PaymentRequest, amount_usdc: float) -> str:
    """What the in-flight marker says we were about to do."""
    wallet = request.counterparty_wallet or "(no wallet)"
    if _uses_escrow(request):
        return f"escrow {amount_usdc} USDC for {wallet}"
    return f"transfer {amount_usdc} USDC to {wallet}"


def _pending_entry(
    request: PaymentRequest,
    scores,
    policy_decision,
    amount_usdc: float,
    engine_name: str,
) -> AuditEntry:
    """The entry written BEFORE any money moves.

    Without this, a crash between sending the transfer and writing the result
    leaves no trace, so a retry pays a second time. The marker makes that
    window visible: `payment_attempt` records exactly what we were about to
    do, `payment_amount_usdc` records how much, and `payment_status` stays
    "not_applicable" until something actually happens.
    """
    attempt = _attempt_description(request, amount_usdc)
    return build_entry(
        request=request,
        scores=scores,
        policy_decision=policy_decision,
        payment_executed=False,
        payment_status="not_applicable",
        payment_detail="Payment attempt in flight; this row is replaced by the outcome.",
        engine_name=engine_name,
        payment_attempt=attempt,
        payment_amount_usdc=amount_usdc,
    )


def _confirm_entry(current: AuditEntry, outcome: PaymentOutcome, reason: str | None = None) -> AuditEntry:
    """The entry written AFTER the attempt settled.

    Built from the pending row so the decision, scores and evidence are
    carried over unchanged; only the payment fields and the timestamp are
    replaced (and `reason`, when the caller has one to add).

    `payment_attempt` is cleared by a final outcome: the marker and a final
    outcome must never both be present, which is what `_in_flight` reads. The
    exception is "unconfirmed", where the marker is deliberately KEPT: the
    transfer may have gone out, so the request has to stay blocked until
    someone reconciles it against Circle/Arc.
    """
    unconfirmed = outcome.status == "unconfirmed"
    updates = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payment_executed": outcome.executed,
        "transaction_id": (outcome.result or {}).get("transaction_id"),
        "tx_hash": (outcome.result or {}).get("tx_hash"),
        "payment_status": outcome.status,
        "payment_detail": outcome.detail,
        "escrow_job_id": outcome.escrow_job_id,
        "escrow_status": outcome.escrow_status,
        "payment_attempt": current.payment_attempt if unconfirmed else None,
    }
    if reason is not None:
        updates["reason"] = reason
    return current.model_copy(update=updates)


def _settle_recorded(
    pending: AuditEntry,
    request: PaymentRequest,
    amount_usdc: float,
    memo: str,
    arc_client=None,
    escrow=None,
    reason_after=None,
) -> tuple[AuditEntry, PaymentOutcome]:
    """The ONE way money leaves: record the intent, pay, record the outcome.

    Both paths that pay (the automatic pipeline and a human's approval of an
    escalation) go through here, so neither can skip the step that makes a
    crash visible. Before this existed only `run_pipeline` wrote the pending
    row; a human approval paid first and recorded afterwards, so a crash in
    between left no trace and approving again paid twice.

    `pending` must carry the `payment_attempt` marker. It is appended BEFORE
    anything moves; the returned entry (already appended) is the outcome.
    `reason_after(outcome)` may return a new reason for the outcome row.
    """
    append_entry(pending)
    outcome = _settle(
        request,
        amount_usdc=amount_usdc,
        memo=memo,
        arc_client=arc_client,
        escrow=escrow,
        attempt=pending.attempt_number or 1,
    )
    reason = reason_after(outcome) if reason_after else None
    entry = _confirm_entry(pending, outcome, reason=reason)
    append_entry(entry)
    return entry, outcome


def run_pipeline(
    request: PaymentRequest,
    engine=None,
    arc_client=None,
    verbose: bool = True,
    escrow=None,
) -> AuditEntry:
    """Run one payment request through scoring, policy, and (if approved)
    on-chain execution, then persist and return the resulting audit entry.

    Raises DuplicateRequestError if this request_id was already processed:
    every request is decided and paid at most once, and InvalidAmountError (a
    ValueError) if the amount is not something USDC can pay: not positive, not
    finite, or with more than 6 decimal places. That is checked first, before the
    engine is called or anything is logged, so a request that could never be paid
    does not consume a scoring call or reach a human's queue.

    When a payment is due, TWO entries are appended: a `pending` row before
    anything moves, and the outcome row after. See `_pending_entry`.
    """
    parse_usdc(request.amount_usdc)

    with _lock_for(request.request_id):
        entries = read_latest_per_request()
        existing = _latest_for(entries, request.request_id)
        if existing is not None:
            # An unfinished attempt means money may already be in flight: refuse
            # before re-paying, and say so distinctly from "already processed".
            if _in_flight(existing):
                raise PaymentInFlightError(
                    f"Request {request.request_id!r} has a payment in flight "
                    f"({existing.payment_attempt or 'recorded but unconfirmed'}). "
                    "Refusing to pay again: reconcile it against Circle/Arc first."
                )
            raise DuplicateRequestError(
                f"Request {request.request_id!r} was already processed; "
                "use a new request_id to submit a new payment."
            )

        engine = engine or DEFAULT_ENGINE_FACTORY()
        # Stamp when the system received this request, unless the caller set it
        # (demo seeds and calibration scenarios do, to stay reproducible). The
        # hour of day is a strong fraud signal and nothing else records it.
        if request.submitted_at is None:
            request = request.model_copy(update={"submitted_at": datetime.now(timezone.utc)})
        # What the system has recorded about this requester and destination.
        history = compute_history(request, entries)
        try:
            scores = engine.score(request, history=history)
        except EngineResponseError as e:
            # The engine answered, and its answer cannot be used. No decision
            # was made and no money can move, but the attempt is still an event
            # a reviewer needs to see: this system's claim is that the log
            # explains every decision, and "the engine returned garbage at
            # 14:02" is part of that story. Recording it also means the row
            # exists when someone asks why a request never came back.
            #
            # Deliberately only THIS error: an unexpected exception (a bug, an
            # exhausted retry, an untyped engine failure) is not recorded,
            # because guessing a decision field for it could put a misleading
            # row in the chain. Those keep failing loudly, which is correct.
            _record_failed_scoring(request, entries, engine, e)
            raise
        # Who may be paid where: the registry from config, taught by the log.
        # Built per request from the entries already in hand, so a payee that
        # has been paid successfully before is known without anyone typing it in.
        payee_registry = PayeeRegistry.from_config().learn_from_log(entries)
        policy_decision = apply_policy(request, scores, payee_registry=payee_registry)
        engine_name = _engine_name(engine)

        approved = policy_decision.decision in (Decision.APPROVE, Decision.APPROVE_REDUCED)
        if not approved:
            # Nothing will move: one entry is the whole story.
            entry = build_entry(
                request=request,
                scores=scores,
                policy_decision=policy_decision,
                payment_executed=False,
                payment_status="not_applicable",
                engine_name=engine_name,
            )
            append_entry(entry)
            outcome = None
        else:
            amount = policy_decision.reduced_amount or request.amount_usdc
            # Record the intent first, so a crash here cannot hide the attempt.
            pending = _pending_entry(request, scores, policy_decision, amount, engine_name)
            entry, outcome = _settle_recorded(
                pending,
                request,
                amount,
                memo=f"payment for request {request.request_id}",
                arc_client=arc_client,
                escrow=escrow,
            )

    if verbose:
        if outcome and outcome.executed:
            print(
                f"  -> transaction_id={outcome.result['transaction_id']} "
                f"tx_hash={outcome.result['tx_hash']}"
            )
        elif outcome and outcome.escrow_job_id is not None and outcome.status != "failed":
            print(f"  -> escrow job {outcome.escrow_job_id}: {outcome.status}")
        elif outcome:
            print(f"  -> payment {outcome.status} for {request.request_id}: {outcome.detail}")
        print(f"Decision for {request.request_id}: {policy_decision.decision.value}")
        print(f"Reason: {policy_decision.reason}")

    return entry


def apply_human_override(
    original_entry: AuditEntry,
    action: str,
    reviewer: str,
    arc_client=None,
    escrow=None,
) -> AuditEntry:
    """Record a human's approve/deny decision on a previously escalated
    request, executing the payment on approval even though it exceeded the
    policy's automatic limits -- that override authority is the entire point
    of escalating to a human in the first place.

    Appends a NEW audit entry for the same request rather than mutating the
    original (the log stays append-only). `action` is "approve" or "deny".

    Raises AlreadyReviewedError if a human already reviewed this request,
    checked against the log itself (not just `original_entry`, which may be
    stale if two reviewers click at the same moment).

    Raises PaymentInFlightError if a payment for this request is already
    recorded but unconfirmed: approving again would pay a second time.
    """
    if action not in ("approve", "deny"):
        raise ValueError(f"action must be 'approve' or 'deny', got {action!r}")

    request = original_entry.request

    with _lock_for(request.request_id):
        current = _latest_for(read_latest_per_request(), request.request_id)
        # In flight is checked BEFORE "already reviewed": an approval whose
        # payment is unconfirmed carries human_review="approved" too, and the
        # useful thing to tell the operator is "reconcile it", not "someone
        # already approved this".
        if current is not None and _in_flight(current):
            raise PaymentInFlightError(
                f"Request {request.request_id!r} has a payment in flight "
                f"({current.payment_attempt or 'recorded but unconfirmed'}). "
                "Refusing to pay again: reconcile it against Circle/Arc first."
            )
        if current is not None and current.human_review is not None:
            raise AlreadyReviewedError(
                f"Request {request.request_id!r} was already {current.human_review} "
                f"by {current.reviewer!r}."
            )
        # Only an ESCALATED request can be overridden: that is the entire point
        # of escalating. Before this guard, calling `apply_human_override` on a
        # request the policy had already approved re-approved it and paid the
        # FULL requested amount -- measured at 50,000 USDC against a 1,000 USDC
        # category ceiling. The escalation route checks the decision before it
        # calls in, but this is the function that releases money above the
        # policy's limits, so the check belongs here too: a caller that forgets
        # it (a script, the CLI, a future route) must not be able to overrule a
        # decision the policy already made.
        if current is not None and current.decision in ("approve", "approve_reduced"):
            raise AlreadyReviewedError(
                f"Request {request.request_id!r} was already decided by the policy "
                f"({current.decision!r}, {current.reason!r}); there is nothing for a "
                "human to override here."
            )

        past_tense = {"approve": "approved", "deny": "denied"}[action]
        reason = f"Escalated request {past_tense} by human reviewer ({reviewer})."

        def human_entry(**overrides) -> AuditEntry:
            # The rules in force for the human's decision. Read fresh
            # rather than inherited from `original_entry`: a human may be
            # overriding on a later day, under rules that have since changed,
            # and this row has to say which ones were in force for THIS act.
            snapshot, fingerprint = _current_config_snapshot()
            fields = dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request=request,
                scores=original_entry.scores,
                decision="approve" if action == "approve" else "escalate",
                reduced_amount=None,
                reason=reason,
                payment_executed=False,
                human_review="approved" if action == "approve" else "denied",
                reviewer=reviewer,
                engine_name=original_entry.engine_name,
                payment_status="not_applicable",
                config_snapshot=snapshot,
                config_hash=fingerprint,
            )
            fields.update(overrides)
            return AuditEntry(**fields)

        if action == "deny":
            entry = human_entry()
            append_entry(entry)
            return entry

        # A human approving an escalated milestone still goes through
        # escrow: overriding the policy's limits must not also bypass
        # the hold-until-verified protection.
        #
        # The full requested amount is deliberate: the human is overriding
        # the policy's reduction, and the guards above (never reviewed,
        # nothing in flight) are what make paying it exactly once safe.
        # `payment_amount_usdc` records the figure actually authorized, so
        # the row says what was approved without implying a policy cap.
        #
        # The pending row is written BEFORE the money moves, same as the
        # automatic path (see `_settle_recorded`). It already carries the
        # human's approval, but decision="approve" and the in-flight marker
        # take it out of the escalation queue and make a second click refuse
        # instead of paying again.
        amount = request.amount_usdc
        pending = human_entry(
            reason=f"{reason} Payment in flight; this row is replaced by the outcome.",
            payment_detail="Payment attempt in flight; this row is replaced by the outcome.",
            payment_attempt=_attempt_description(request, amount),
            payment_amount_usdc=amount,
        )
        entry, _ = _settle_recorded(
            pending,
            request,
            amount,
            memo=f"human-approved payment for request {request.request_id}",
            arc_client=arc_client,
            escrow=escrow,
            reason_after=lambda outcome: (
                reason if outcome.executed else f"{reason} Payment {outcome.status}: {outcome.detail}"
            ),
        )
    return entry


# --------------------------------------------------------------------------
# Getting an approved payment out of a stuck state
# --------------------------------------------------------------------------
#
# A direct transfer can end up approved and not paid in three ways, and each
# needs a different answer:
#
#   failed       Circle said no (or the transfer ended FAILED/CANCELLED/DENIED).
#                The money did not move, so it is safe to try again: retry_payment
#                with a NEW idempotency key.
#   unconfirmed  The transfer may have gone out and we could not find out (also a
#   or a crash   crash between "intent recorded" and "outcome recorded"). Never
#   pending row  retried blindly: reconcile_payment asks Circle what happened and
#                records it. Only if Circle has no record may it be resent, with
#                the SAME key, so that Circle rather than our guess is what
#                prevents a second transfer.
#   skipped      Nothing was ever sent (e.g. CIRCLE_API_KEY was missing). Safe to
#                retry once the cause is fixed.

_CIRCLE_FAILED_STATES = {"FAILED", "CANCELLED", "DENIED"}


@dataclass
class ReconcileResult:
    entry: AuditEntry
    changed: bool  # False: Circle was asked, but nothing new could be recorded
    observed: str  # Circle's state for the transfer, or "not_found"
    message: str


def _recovery_target(request_id: str) -> AuditEntry:
    current = _latest_for(read_latest_per_request(), request_id)
    if current is None:
        raise PaymentNotFoundError(f"No request {request_id!r} in the audit log.")
    if _uses_escrow(current.request):
        raise PaymentStateError(
            f"Request {request_id!r} is paid through escrow, whose state is checked on-chain "
            "and moved with the milestone actions, not with payment recovery."
        )
    return current


def _ask_circle(arc_client, current: AuditEntry) -> dict | None:
    """Look the transfer up. Any failure becomes a PaymentLookupError."""
    try:
        arc_client = arc_client or ArcPaymentClient()
        return arc_client.find_transfer(
            ref_id=current.request.request_id,
            transaction_id=current.transaction_id,
            destination_wallet=current.request.counterparty_wallet,
            since=current.timestamp,
        )
    except Exception as e:  # noqa: BLE001 -- the caller needs one error type, and the cause text
        raise PaymentLookupError(
            f"Could not ask Circle about request {current.request.request_id!r}: "
            f"{type(e).__name__}: {e}"
        ) from e


def reconcile_payment(request_id: str, reviewer: str = "operator", arc_client=None) -> ReconcileResult:
    """Find out how a payment that is "in flight" really ended, and record it.

    READ ONLY against Circle: it never sends money. A transfer Circle reports
    COMPLETE becomes `executed`; one it reports FAILED/CANCELLED/DENIED becomes
    `failed`, which unblocks `retry_payment`. Anything else (still in progress,
    or no record found) changes nothing and appends nothing, so pressing the
    button repeatedly does not fill the log.
    """
    with _lock_for(request_id):
        current = _recovery_target(request_id)
        if not _in_flight(current):
            raise PaymentStateError(
                f"Request {request_id!r} has no payment in flight to reconcile "
                f"(payment status: {current.payment_status!r})."
            )

        found = _ask_circle(arc_client, current)
        if found is None:
            return ReconcileResult(
                current, False, "not_found",
                "Circle has no transfer for this request in the searched window. It may never have "
                "been sent, or the search missed it; nothing was recorded.",
            )

        state = (found.get("state") or "").upper()
        if state == "COMPLETE":
            outcome = PaymentOutcome(
                "executed", result={"transaction_id": found.get("transaction_id"), "tx_hash": found.get("tx_hash")}
            )
        elif state in _CIRCLE_FAILED_STATES:
            detail = f"Circle reports the transfer {state}"
            if found.get("error_reason"):
                detail += f": {found['error_reason']}"
            outcome = PaymentOutcome("failed", detail, result={"transaction_id": found.get("transaction_id")})
        else:
            return ReconcileResult(
                current, False, state or "unknown",
                f"Circle reports the transfer as {state or 'in an unreported state'}: not final yet, "
                "so nothing was recorded. Try again in a moment.",
            )

        entry = _confirm_entry(
            current, outcome,
            reason=f"{current.reason} Reconciled against Circle by {reviewer}: transfer {state}.",
        )
        append_entry(entry)
        return ReconcileResult(entry, True, state, f"Recorded: the transfer is {state}.")


def retry_payment(
    request_id: str,
    reviewer: str = "operator",
    arc_client=None,
    resend_unconfirmed: bool = False,
) -> AuditEntry:
    """Try again to pay a request that was approved but never paid.

    Goes through the same record-then-pay path as every other payment
    (`_settle_recorded`), so a crash during the retry is as visible as one during
    the first attempt.

    - after a definite failure (`failed`) the retry uses a NEW attempt number,
      hence a new Circle idempotency key: the old one may replay the failure;
    - after `skipped` (nothing was ever sent) the attempt number is unchanged;
    - a payment still in flight is refused, unless `resend_unconfirmed` is set AND
      a fresh look at Circle finds no record of it. That resend reuses the SAME
      key, so if the transfer does exist Circle answers with it instead of
      creating a second one.
    """
    with _lock_for(request_id):
        current = _recovery_target(request_id)
        request = current.request

        if current.decision not in ("approve", "approve_reduced"):
            raise PaymentStateError(
                f"Request {request_id!r} is not an approved payment (decision: {current.decision!r})."
            )
        if current.payment_executed or current.payment_status == "executed":
            raise PaymentStateError(f"Request {request_id!r} was already paid.")
        if not request.counterparty_wallet:
            raise PaymentStateError(f"Request {request_id!r} has no counterparty_wallet to pay.")

        amount = current.payment_amount_usdc or current.reduced_amount or request.amount_usdc
        attempt = current.attempt_number or 1

        if _in_flight(current):
            if not resend_unconfirmed:
                raise PaymentInFlightError(
                    f"Request {request_id!r} has a payment in flight "
                    f"({current.payment_attempt or 'unconfirmed'}). Reconcile it against Circle first; "
                    "only if Circle has no record may it be resent."
                )
            found = _ask_circle(arc_client, current)
            if found is not None:
                raise PaymentStateError(
                    f"Circle already has a transfer for request {request_id!r} "
                    f"(state: {found.get('state')}). Reconcile it instead of sending again."
                )
            # Same attempt, same key: Circle, not our guess, decides if it exists.
        elif current.payment_status == "failed":
            attempt += 1
        elif current.payment_status != "skipped":
            raise PaymentStateError(
                f"Request {request_id!r} cannot be retried from payment status {current.payment_status!r}."
            )

        pending = current.model_copy(
            update={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payment_attempt": _attempt_description(request, amount),
                "payment_amount_usdc": amount,
                "payment_status": "not_applicable",
                "payment_executed": False,
                "payment_detail": "Payment retry in flight; this row is replaced by the outcome.",
                "transaction_id": None,
                "tx_hash": None,
                "attempt_number": attempt,
            }
        )
        entry, _ = _settle_recorded(
            pending,
            request,
            amount,
            memo=f"retry (attempt {attempt}) of payment for request {request_id}",
            arc_client=arc_client,
            reason_after=lambda outcome: (
                f"{current.reason} Payment retried by {reviewer} (attempt {attempt}): {outcome.status}."
            ),
        )
        return entry


# --------------------------------------------------------------------------
# Milestone escrow: actions after the job is opened
# --------------------------------------------------------------------------

# action -> (escrow_status values our log must show for it to be allowed).
# "release"/"reject" accept "funded" too because a real contractor submits
# on-chain without telling us: our log can lag the chain, and the contract
# (checked by EscrowClient before sending anything) is the final judge.
_MILESTONE_ALLOWED = {
    "fund": ("open",),
    "deliver": ("funded",),
    "release": ("funded", "submitted"),
    "reject": ("open", "funded", "submitted"),
}
MILESTONE_ACTIONS = tuple(_MILESTONE_ALLOWED)


_MAX_ERROR_CHARS = 500


def _record_failed_attempt(current: AuditEntry, action: str, reviewer: str, error: Exception) -> None:
    """Append a failed escrow action to the audit log, best effort.

    The escrow state is deliberately left as it was: funds did not move, so
    the log must not claim they did (nor that they did not exist).

    Two safeguards:
    - A failure identical to the one already on top of the log (same action,
      same error) is not appended again: repeating it adds no information, and a
      client stuck in a retry loop must not grow the hash-chained log without
      bound.
    - Writing this entry must never hide the real error: if the log write
      itself fails, that is reported via `logging` and the caller still
      gets the original exception.
    """
    text = str(error) if isinstance(error, EscrowError) else f"{type(error).__name__}: {error}"
    text = text[:_MAX_ERROR_CHARS] or type(error).__name__

    previous = current.failed_attempt
    if previous is not None and previous.action == action and previous.error == text:
        return

    attempt = FailedAttempt(
        action=action,
        error=text,
        transaction_id=getattr(error, "transaction_id", None),
        tx_hash=getattr(error, "tx_hash", None),
    )
    entry = current.model_copy(
        update={
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviewer": reviewer,
            "reason": f"Attempt to {action} by {reviewer} failed; escrow unchanged. {text}",
            "failed_attempt": attempt,
        }
    )
    try:
        append_entry(entry)
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the failed %s attempt for %s", action, current.request.request_id)


def _record_failed_scoring(
    request: PaymentRequest,
    entries: list[AuditEntry],
    engine,
    error: EngineResponseError,
) -> None:
    """Append the fact that the engine could not answer, best effort.

    There is no decision to log, so the row carries `decision="error"` and the
    cause in `failed_attempt` -- the same shape the escrow path uses for an
    action that was tried and failed. `payment_status` stays
    "not_applicable": nothing was sent, and claiming otherwise would be worse
    than saying nothing.

    Two safeguards, matching `_record_failed_attempt`:

    - A repeat of a failure already on top of the log for this request is not
      appended again, so a caller cannot grow the hash-chained log without
      bound by resubmitting the same broken request.
    - Writing this must never hide the real error: if the append itself fails,
      that is reported through `logging` and the engine's error still reaches
      the caller.

    The row is NOT built from an existing entry (there is none for a first
    attempt), so `scores` is empty: the engine did not produce any, and
    inventing zeros would look like a decision.
    """
    text = (str(error) or type(error).__name__)[:_MAX_ERROR_CHARS]
    previous = _latest_for(entries, request.request_id)
    if previous is not None and previous.failed_attempt is not None:
        if previous.failed_attempt.action == "score" and previous.failed_attempt.error == text:
            return

    placeholder = ScoreResult(value=0.0, confidence=0.0)
    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=request,
        scores=DecisionScores(
            trust=placeholder,
            urgency=placeholder,
            importance=placeholder,
            evidence=Evidence(
                model=_engine_name(engine),
                state={},
                answers={},
                problems=[text],
            ),
        ),
        decision="error",
        reduced_amount=None,
        reason=(
            f"The decision engine could not be used for this request, so no decision was "
            f"made and no payment was attempted. {text}"
        ),
        payment_executed=False,
        payment_status="not_applicable",
        engine_name=_engine_name(engine),
        failed_attempt=FailedAttempt(action="score", error=text),
    )
    try:
        append_entry(entry)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not record the failed scoring attempt for %s", request.request_id
        )


def advance_milestone(
    request_id: str,
    action: str,
    reviewer: str = "operator",
    reason: str | None = None,
    escrow=None,
) -> AuditEntry:
    """Move a milestone escrow forward and record the step in the audit log.

    fund     retry locking the funds (e.g. the contractor set the budget late)
    deliver  DEMO ONLY: submit the deliverable as the contractor, possible
             only when the contractor's wallet is one we control
    release  verified: release the escrowed funds to the contractor
    reject   not delivered: refund the escrow to the treasury

    Each step appends a new entry (the log stays append-only). If the escrow
    layer fails (a transaction reverts, Circle errors, the budget does not
    match what was approved...), the failure is appended too, as a copy of
    the current entry carrying `failed_attempt` with the escrow state left
    untouched, and the error is re-raised so the caller sees it. Refusals
    made before reaching the escrow (unknown request, wrong state) are not
    logged: they never touched anything.
    """
    if action not in _MILESTONE_ALLOWED:
        raise ValueError(f"action must be one of {MILESTONE_ACTIONS}, got {action!r}")

    with _lock_for(request_id):
        current = next(
            (e for e in read_latest_per_request() if e.request.request_id == request_id), None
        )
        if current is None:
            raise MilestoneNotFoundError(f"No request {request_id!r} in the audit log.")
        if current.escrow_job_id is None or current.escrow_status not in _MILESTONE_ALLOWED[action]:
            raise MilestoneError(
                f"Cannot {action} request {request_id!r}: escrow status is "
                f"{current.escrow_status!r}, expected {' or '.join(_MILESTONE_ALLOWED[action])}."
            )

        request = current.request
        job_id = current.escrow_job_id
        amount = current.reduced_amount or request.amount_usdc

        # A state refusal, not an attempt: checked before the try so it is
        # not logged as a failure.
        if action == "deliver" and not provider_is_ours(request):
            raise MilestoneError("The contractor's wallet is not one we control; they must submit themselves.")

        try:
            escrow = escrow or EscrowClient()
            if action == "fund":
                tx = escrow.fund(job_id, amount)
                updates = dict(escrow_status="funded", payment_status="escrowed",
                               reason=f"Escrow funded: {amount} USDC locked in job {job_id}.")
            elif action == "deliver":
                tx = escrow.submit(job_id, f"deliverable:{request_id}", provider_wallet=request.counterparty_wallet)
                updates = dict(escrow_status="submitted", payment_status="escrowed",
                               reason=f"Contractor submitted the deliverable for job {job_id} (simulated).")
            elif action == "release":
                tx = escrow.complete(job_id, reason or f"milestone verified by {reviewer}")
                updates = dict(escrow_status="released", payment_status="executed", payment_executed=True,
                               reason=f"Milestone verified by {reviewer}; escrow released to the contractor.")
            else:  # reject
                tx = escrow.reject(job_id, reason or f"milestone rejected by {reviewer}")
                updates = dict(escrow_status="rejected", payment_status="refunded",
                               reason=f"Milestone rejected by {reviewer}; escrow refunded to the treasury.")
        except Exception as e:
            _record_failed_attempt(current, action, reviewer, e)
            raise

        entry = current.model_copy(
            update={
                **updates,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reviewer": reviewer,
                "payment_detail": None,
                "transaction_id": tx["transaction_id"],
                "tx_hash": tx["tx_hash"],
                "failed_attempt": None,  # a success clears the last failure
            }
        )
        append_entry(entry)
    return entry
