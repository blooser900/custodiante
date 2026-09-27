"""Tests for the policy layer -- the part that decides, not just scores."""

from src.decision_engine.engine import DecisionScores, ScoreResult
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory


def make_request(amount: float, category: RequestCategory) -> PaymentRequest:
    return PaymentRequest(
        request_id="test-1",
        category=category,
        amount_usdc=amount,
        requester="test-suite",
        description="A test payment request.",
    )


def make_scores(trust: float, urgency: float, importance: float, confidence: float = 0.9):
    return DecisionScores(
        trust=ScoreResult(value=trust, confidence=confidence),
        urgency=ScoreResult(value=urgency, confidence=confidence),
        importance=ScoreResult(value=importance, confidence=confidence),
    )


def test_low_trust_always_escalates():
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)
    scores = make_scores(trust=0.1, urgency=0.9, importance=0.9)

    result = apply_policy(request, scores)

    assert result.decision == Decision.ESCALATE


def test_high_trust_auto_approves():
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)
    scores = make_scores(trust=0.9, urgency=0.2, importance=0.2, confidence=0.8)

    result = apply_policy(request, scores)

    assert result.decision == Decision.APPROVE


def test_amount_over_category_limit_escalates_regardless_of_trust():
    request = make_request(10_000, RequestCategory.OPERATIONAL_EXPENSE)
    scores = make_scores(trust=0.95, urgency=0.9, importance=0.9)

    result = apply_policy(request, scores)

    assert result.decision == Decision.ESCALATE


def test_medium_trust_high_importance_approves_reduced():
    request = make_request(400, RequestCategory.OPERATIONAL_EXPENSE)
    scores = make_scores(trust=0.5, urgency=0.3, importance=0.8)

    result = apply_policy(request, scores)

    assert result.decision == Decision.APPROVE_REDUCED
    assert result.reduced_amount is not None
