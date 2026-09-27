"""Deterministic policy rules.

This is the "contract" mentioned in the spec: the decision engine's scores
are checked against fixed, explicit thresholds here. The engine cannot
override or bypass this layer -- it only supplies numbers.
"""

from enum import Enum

from src.decision_engine.engine import DecisionScores
from src.policy.limits import get_limit_for_category
from src.requests.schema import PaymentRequest


class Decision(str, Enum):
    APPROVE = "approve"
    APPROVE_REDUCED = "approve_reduced"
    ESCALATE = "escalate"


# Thresholds are intentionally simple and explicit. Tune these based on
# real observed behavior, not guesswork.
TRUST_MIN_TO_AUTO_APPROVE = 0.75
TRUST_MIN_TO_CONSIDER = 0.4
IMPORTANCE_HIGH = 0.7
URGENCY_HIGH = 0.7


class PolicyDecision:
    """Result of applying policy rules to a request's scores."""

    def __init__(self, decision: Decision, reduced_amount: float | None, reason: str) -> None:
        self.decision = decision
        self.reduced_amount = reduced_amount
        self.reason = reason


def apply_policy(request: PaymentRequest, scores: DecisionScores) -> PolicyDecision:
    """Combine the three scores with fixed thresholds to reach a decision.

    This function is the single place where "how cautious is the system"
    is defined. Keep it readable -- a reviewer should be able to follow the
    logic without needing the rest of the codebase.
    """
    category_limit = get_limit_for_category(request.category)

    # Low trust is a hard stop, regardless of urgency or importance.
    if scores.trust.value < TRUST_MIN_TO_CONSIDER:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason="Trust score below minimum threshold.",
        )

    # Request exceeds the category's hard limit -- always needs a human,
    # no matter how confident the engine is.
    if request.amount_usdc > category_limit:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=f"Amount exceeds hard limit for category ({category_limit} USDC).",
        )

    # High trust, and the engine is confident in that trust score: auto-approve.
    if scores.trust.value >= TRUST_MIN_TO_AUTO_APPROVE and scores.trust.confidence >= 0.7:
        return PolicyDecision(
            decision=Decision.APPROVE,
            reduced_amount=None,
            reason="High, confidently-assessed trust score.",
        )

    # Medium trust, but high importance/urgency: approve at a reduced amount
    # rather than blocking or fully escalating.
    if scores.importance.value >= IMPORTANCE_HIGH or scores.urgency.value >= URGENCY_HIGH:
        reduced_amount = min(request.amount_usdc, category_limit * 0.5)
        return PolicyDecision(
            decision=Decision.APPROVE_REDUCED,
            reduced_amount=reduced_amount,
            reason="Medium trust, but high urgency/importance justifies a capped approval.",
        )

    # Default: when in doubt, escalate.
    return PolicyDecision(
        decision=Decision.ESCALATE,
        reduced_amount=None,
        reason="No rule confidently applies; deferring to a human.",
    )
