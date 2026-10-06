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
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest

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

    status: str  # "executed" | "failed" | "skipped"
    detail: str | None = None
    result: dict | None = None

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


def _engine_name(engine) -> str:
    name = getattr(engine, "name", None)
    return name if isinstance(name, str) else type(engine).__name__


def run_pipeline(
    request: PaymentRequest,
    engine=None,
    arc_client=None,
    verbose: bool = True,
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
            outcome = _attempt_payment(
                request,
                amount_usdc=policy_decision.reduced_amount or request.amount_usdc,
                memo=f"payment for request {request.request_id}",
                arc_client=arc_client,
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
        )
        append_entry(entry)

    if verbose:
        if outcome and outcome.executed:
            print(
                f"  -> transaction_id={outcome.result['transaction_id']} "
                f"tx_hash={outcome.result['tx_hash']}"
            )
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
            outcome = _attempt_payment(
                request,
                amount_usdc=request.amount_usdc,
                memo=f"human-approved payment for request {request.request_id}",
                arc_client=arc_client,
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
        )
        append_entry(entry)
    return entry
