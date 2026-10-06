"""Core request -> decision -> payment pipeline.

Extracted out of src/main.py so the CLI and the HTTP API (src/api.py) share
one implementation instead of two copies that could drift apart.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

# Must run before the src.* imports below -- see the long comment that used
# to live in main.py: jev_client.py / fallback_classifier.py read their
# config as module-level constants at import time.
load_dotenv()

from src.audit.logger import AuditEntry, build_entry
from src.audit.store import append_entry
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.jev_client import JevDecisionEngine
from src.payments.arc_client import ArcPaymentClient
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest

# Swap this line to switch engines -- both implement the same interface.
# Defaults to Kev (self-hosted, see scripts/setup_arc_wallet.py and the Kev
# quick-start in README) since that's what's actually been validated
# end-to-end; switch to JevDecisionEngine once real Jev API access exists.
DEFAULT_ENGINE_FACTORY = TypeSafeCompatibleEngine
# DEFAULT_ENGINE_FACTORY = JevDecisionEngine  # real Jev API


def run_pipeline(
    request: PaymentRequest,
    engine=None,
    arc_client=None,
    verbose: bool = True,
) -> AuditEntry:
    """Run one payment request through scoring, policy, and (if approved)
    on-chain execution, then persist and return the resulting audit entry.
    """
    engine = engine or DEFAULT_ENGINE_FACTORY()

    scores = engine.score(request)
    policy_decision = apply_policy(request, scores)

    payment_executed = False
    payment_result: dict | None = None
    approved = policy_decision.decision in (Decision.APPROVE, Decision.APPROVE_REDUCED)

    if approved and request.counterparty_wallet and os.environ.get("CIRCLE_API_KEY"):
        arc_client = arc_client or ArcPaymentClient()
        amount = policy_decision.reduced_amount or request.amount_usdc
        try:
            payment_result = arc_client.send_payment(
                destination_wallet=request.counterparty_wallet,
                amount_usdc=amount,
                memo=f"payment for request {request.request_id}",
            )
            payment_executed = True
            if verbose:
                print(
                    f"  -> transaction_id={payment_result['transaction_id']} "
                    f"tx_hash={payment_result['tx_hash']}"
                )
        except (RuntimeError, ValueError) as e:
            if verbose:
                print(f"Payment execution failed for {request.request_id}: {e}")
    elif approved and verbose and (not request.counterparty_wallet or not os.environ.get("CIRCLE_API_KEY")):
        print(
            f"Request {request.request_id} was approved, but on-chain execution "
            "was skipped (missing counterparty_wallet or CIRCLE_API_KEY)."
        )

    entry = build_entry(
        request=request,
        scores=scores,
        policy_decision=policy_decision,
        payment_executed=payment_executed,
        payment_result=payment_result,
    )
    append_entry(entry)

    if verbose:
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
    """
    if action not in ("approve", "deny"):
        raise ValueError(f"action must be 'approve' or 'deny', got {action!r}")

    request = original_entry.request
    payment_executed = False
    payment_result: dict | None = None
    past_tense = {"approve": "approved", "deny": "denied"}[action]
    reason = f"Escalated request {past_tense} by human reviewer ({reviewer})."

    if action == "approve":
        if request.counterparty_wallet and os.environ.get("CIRCLE_API_KEY"):
            arc_client = arc_client or ArcPaymentClient()
            try:
                payment_result = arc_client.send_payment(
                    destination_wallet=request.counterparty_wallet,
                    amount_usdc=request.amount_usdc,
                    memo=f"human-approved payment for request {request.request_id}",
                )
                payment_executed = True
            except (RuntimeError, ValueError) as e:
                reason += f" Payment execution failed: {e}"
        else:
            reason += " On-chain execution skipped (missing counterparty_wallet or CIRCLE_API_KEY)."

    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=request,
        scores=original_entry.scores,
        decision="approve" if action == "approve" else "escalate",
        reduced_amount=None,
        reason=reason,
        payment_executed=payment_executed,
        transaction_id=(payment_result or {}).get("transaction_id"),
        tx_hash=(payment_result or {}).get("tx_hash"),
        human_review="approved" if action == "approve" else "denied",
        reviewer=reviewer,
    )
    append_entry(entry)
    return entry
