"""Tests for the offline mock decision engine and the DECISION_ENGINE selector."""

import os
from unittest.mock import patch

import pytest

from src.decision_engine.mock_engine import MockDecisionEngine
from src.pipeline import build_engine_from_env
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory


def make_request(**overrides) -> PaymentRequest:
    base = dict(
        request_id="mock-1",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="ops-team",
        description="Routine expense.",
    )
    return PaymentRequest(**{**base, **overrides})


def test_scores_stay_in_valid_range_and_are_deterministic():
    engine = MockDecisionEngine()
    request = make_request()
    first = engine.score(request)
    assert first == engine.score(request)
    for score in (first.trust, first.urgency, first.importance):
        assert 0 <= score.value <= 1
        assert 0 <= score.confidence <= 1


def test_red_flag_outweighs_praise():
    scores = MockDecisionEngine().score(
        make_request(description="Verified, documented invoice, but requester is unknown.")
    )
    assert scores.trust.value == 0.0


def test_strong_positive_signals_give_confident_high_trust():
    scores = MockDecisionEngine().score(
        make_request(description="Milestone verified complete, clean payment history.")
    )
    assert scores.trust.value == 1.0
    assert scores.trust.confidence >= 0.7


def test_overdue_due_date_raises_urgency():
    scores = MockDecisionEngine().score(
        make_request(description="Urgent renewal.", due_date="2020-01-01T00:00:00Z")
    )
    assert scores.urgency.value == 1.0


@pytest.mark.parametrize(
    ("description", "amount", "expected"),
    [
        ("Verified milestone, known vendor, clean payment history.", 100.0, Decision.APPROVE),
        (
            "Urgent renewal to avoid service interruption, vendor is new to the system.",
            100.0,
            Decision.APPROVE_REDUCED,
        ),
        ("No prior history and vague justification.", 100.0, Decision.ESCALATE),
    ],
)
def test_mock_scores_drive_the_expected_policy_outcomes(description, amount, expected):
    request = make_request(description=description, amount_usdc=amount)
    scores = MockDecisionEngine().score(request)
    assert apply_policy(request, scores).decision == expected


def test_engine_selector_returns_mock_when_requested():
    with patch.dict(os.environ, {"DECISION_ENGINE": "mock"}):
        engine = build_engine_from_env()
    assert isinstance(engine, MockDecisionEngine)
    assert engine.name == "mock"


def test_engine_selector_defaults_to_kev():
    with patch.dict(os.environ, {}, clear=True):
        assert build_engine_from_env().name == "kev"


def test_engine_selector_rejects_unknown_name():
    with patch.dict(os.environ, {"DECISION_ENGINE": "gpt"}):
        with pytest.raises(RuntimeError, match="Unknown DECISION_ENGINE"):
            build_engine_from_env()
