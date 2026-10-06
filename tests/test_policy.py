"""Tests for the policy layer -- the part that decides, not just scores.

load_config() is mocked in both src.policy.rules and src.policy.limits (each
module imports it separately) so these tests never touch the real
config/rules.yaml on disk, and always run against a fixed, known set of
thresholds/limits regardless of what a user has configured.
"""

from unittest.mock import patch

import pytest

from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory


def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits={
            "contractor_milestone": 2000.0,
            "supply_purchase": 1000.0,
            "operational_expense": 500.0,
            "other": 200.0,
        },
        thresholds=Thresholds(
            trust_min_auto_approve=0.75,
            trust_min_confidence_for_auto_approve=0.7,
            trust_min_to_consider=0.4,
            importance_high=0.7,
            urgency_high=0.7,
            reduced_amount_fraction=0.5,
        ),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
    )


@pytest.fixture(autouse=True)
def mock_config():
    config = fake_rules_config()
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        yield config


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


def test_custom_threshold_changes_outcome():
    """Confirms thresholds are actually read from config, not hardcoded:
    a trust score that would normally auto-approve should instead need
    escalation once the configured bar is raised above it.
    """
    strict_config = fake_rules_config()
    strict_config.thresholds.trust_min_auto_approve = 0.99

    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)
    scores = make_scores(trust=0.9, urgency=0.2, importance=0.2, confidence=0.95)

    with patch("src.policy.rules.load_config", return_value=strict_config):
        result = apply_policy(request, scores)

    assert result.decision != Decision.APPROVE
