"""Core request -> decision -> payment pipeline.

Extracted out of src/main.py so the CLI and the HTTP API (src/api.py) share
one implementation instead of two copies that could drift apart.
"""

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv

# Must run before the src.* imports below: jev_client.py and
# fallback_classifier.py read their config as module-level constants at
# import time.
load_dotenv()

from src.audit.logger import AuditEntry, build_entry
from src.audit.store import append_entry, read_latest_per_request
from src.decision_engine.engine import DecisionEngine
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.jev_client import JevDecisionEngine
from src.decision_engine.mock_engine import MockDecisionEngine
from src.payments.arc_client import ArcPaymentClient
from src.payments.escrow import EscrowClient, EscrowError
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory

# Engines selectable through the DECISION_ENGINE environment variable.
ENGINES: dict[str, type[DecisionEngine]] = {
    "kev": TypeSafeCompatibleEngine,  # self-hosted, the default
    "jev": JevDecisionEngine,  # real Jev API (needs JEV_API_KEY)
    "mock": MockDecisionEngine,  # offline keyword heuristics, demo only
}


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

    # "executed" | "failed" | "skipped" for plain transfers; milestone escrow
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
    request: PaymentRequest, amount_usdc: float, memo: str, arc_client=None
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
        job_id, create_tx = escrow.create_job(request.counterparty_wallet, f"tameion:{request.request_id}")
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
    request: PaymentRequest, amount_usdc: float, memo: str, arc_client=None, escrow=None
) -> PaymentOutcome:
    """Pay an approved request: via escrow for contractor milestones,
    otherwise as a direct transfer."""
    if _uses_escrow(request):
        return _open_escrow(request, amount_usdc, escrow=escrow)
    return _attempt_payment(request, amount_usdc, memo, arc_client)


def _engine_name(engine) -> str:
    name = getattr(engine, "name", None)
    return name if isinstance(name, str) else type(engine).__name__


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
    every request is decided and paid at most once.
    """
    with _lock_for(request.request_id):
        if any(e.request.request_id == request.request_id for e in read_latest_per_request()):
            raise DuplicateRequestError(
                f"Request {request.request_id!r} was already processed; "
                "use a new request_id to submit a new payment."
            )

        engine = engine or DEFAULT_ENGINE_FACTORY()
        scores = engine.score(request)
        policy_decision = apply_policy(request, scores)

        approved = policy_decision.decision in (Decision.APPROVE, Decision.APPROVE_REDUCED)
        outcome: PaymentOutcome | None = None
        if approved:
            outcome = _settle(
                request,
                amount_usdc=policy_decision.reduced_amount or request.amount_usdc,
                memo=f"payment for request {request.request_id}",
                arc_client=arc_client,
                escrow=escrow,
            )

        entry = build_entry(
            request=request,
            scores=scores,
            policy_decision=policy_decision,
            payment_executed=bool(outcome and outcome.executed),
            payment_result=outcome.result if outcome else None,
            payment_status=outcome.status if outcome else "not_applicable",
            payment_detail=outcome.detail if outcome else None,
            engine_name=_engine_name(engine),
            escrow_job_id=outcome.escrow_job_id if outcome else None,
            escrow_status=outcome.escrow_status if outcome else None,
        )
        append_entry(entry)

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
    """
    if action not in ("approve", "deny"):
        raise ValueError(f"action must be 'approve' or 'deny', got {action!r}")

    request = original_entry.request

    with _lock_for(request.request_id):
        current = next(
            (e for e in read_latest_per_request() if e.request.request_id == request.request_id),
            None,
        )
        if current is not None and current.human_review is not None:
            raise AlreadyReviewedError(
                f"Request {request.request_id!r} was already {current.human_review} "
                f"by {current.reviewer!r}."
            )

        past_tense = {"approve": "approved", "deny": "denied"}[action]
        reason = f"Escalated request {past_tense} by human reviewer ({reviewer})."

        outcome: PaymentOutcome | None = None
        if action == "approve":
            # A human approving an escalated milestone still goes through
            # escrow: overriding the policy's limits must not also bypass
            # the hold-until-verified protection.
            outcome = _settle(
                request,
                amount_usdc=request.amount_usdc,
                memo=f"human-approved payment for request {request.request_id}",
                arc_client=arc_client,
                escrow=escrow,
            )
            if not outcome.executed:
                reason += f" Payment {outcome.status}: {outcome.detail}"

        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            request=request,
            scores=original_entry.scores,
            decision="approve" if action == "approve" else "escalate",
            reduced_amount=None,
            reason=reason,
            payment_executed=bool(outcome and outcome.executed),
            transaction_id=((outcome.result if outcome else None) or {}).get("transaction_id"),
            tx_hash=((outcome.result if outcome else None) or {}).get("tx_hash"),
            human_review="approved" if action == "approve" else "denied",
            reviewer=reviewer,
            engine_name=original_entry.engine_name,
            payment_status=outcome.status if outcome else "not_applicable",
            payment_detail=outcome.detail if outcome else None,
            escrow_job_id=outcome.escrow_job_id if outcome else None,
            escrow_status=outcome.escrow_status if outcome else None,
        )
        append_entry(entry)
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

    Each step appends a new entry (the log stays append-only). A transaction
    that fails raises and logs nothing: the escrow is exactly where it was
    and the operator can retry.
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
        escrow = escrow or EscrowClient()

        updates: dict = {}
        if action == "fund":
            tx = escrow.fund(job_id, amount)
            updates = dict(escrow_status="funded", payment_status="escrowed",
                           reason=f"Escrow funded: {amount} USDC locked in job {job_id}.")
        elif action == "deliver":
            if not provider_is_ours(request):
                raise MilestoneError("The contractor's wallet is not one we control; they must submit themselves.")
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

        entry = current.model_copy(
            update={
                **updates,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reviewer": reviewer,
                "payment_detail": None,
                "transaction_id": tx["transaction_id"],
                "tx_hash": tx["tx_hash"],
            }
        )
        append_entry(entry)
    return entry
