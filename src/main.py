"""Entrypoint: orchestrates one full request -> decision -> payment cycle.

Usage:
    python -m src.main --input path/to/request.json
"""

import argparse
import json
import os
import sys

from src.audit.logger import build_entry
from src.audit.store import append_entry
from src.decision_engine.jev_client import JevDecisionEngine
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.payments.arc_client import ArcPaymentClient
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest

# Swap this line to switch engines -- both implement the same interface.
DEFAULT_ENGINE_FACTORY = JevDecisionEngine
# DEFAULT_ENGINE_FACTORY = TypeSafeCompatibleEngine  # Kev, self-hosted


def process_request(request: PaymentRequest, engine=None, arc_client=None) -> None:
    """Run a single payment request through the full pipeline."""
    engine = engine or DEFAULT_ENGINE_FACTORY()

    scores = engine.score(request)
    policy_decision = apply_policy(request, scores)

    payment_executed = False
    approved = policy_decision.decision in (Decision.APPROVE, Decision.APPROVE_REDUCED)

    if approved and request.counterparty_wallet and os.environ.get("CIRCLE_API_KEY"):
        arc_client = arc_client or ArcPaymentClient()
        amount = policy_decision.reduced_amount or request.amount_usdc
        try:
            arc_client.send_payment(
                destination_wallet=request.counterparty_wallet,
                amount_usdc=amount,
                memo=f"payment for request {request.request_id}",
            )
            payment_executed = True
        except RuntimeError as e:
            # Approved by policy, but the on-chain transfer itself failed
            # (denied, timed out, network error). Logged, not silently
            # swallowed -- the audit trail below still records this request
            # as approved-but-not-executed.
            print(f"Payment execution failed for {request.request_id}: {e}")
    elif approved and (not request.counterparty_wallet or not os.environ.get("CIRCLE_API_KEY")):
        print(
            f"Request {request.request_id} was approved, but on-chain execution "
            "was skipped (missing counterparty_wallet or CIRCLE_API_KEY)."
        )

    entry = build_entry(
        request=request,
        scores=scores,
        policy_decision=policy_decision,
        payment_executed=payment_executed,
    )
    append_entry(entry)

    print(f"Decision for {request.request_id}: {policy_decision.decision.value}")
    print(f"Reason: {policy_decision.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a payment request.")
    parser.add_argument("--input", required=True, help="Path to a JSON payment request.")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    request = PaymentRequest.model_validate(data)
    process_request(request)


if __name__ == "__main__":
    sys.exit(main())
