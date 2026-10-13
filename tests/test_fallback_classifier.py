"""Tests for the generic TypeSafe-compatible fallback engine (Kev by default).

These mock the HTTP call, same approach as test_jev_client.py: we are
checking that this engine behaves identically to JevDecisionEngine given
the same response shape, since both must be interchangeable behind the
DecisionEngine interface. The rubric criteria used are a fixed test
fixture (not the real config/rules.yaml), via a mocked load_config.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds

TRUST_CRITERIA = ["bad", "ok", "good"]
URGENCY_CRITERIA = ["low", "medium", "high"]
IMPORTANCE_CRITERIA = ["minor", "moderate", "major"]


def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(),
        thresholds=thresholds(),
        criteria=ScoringCriteria(
            trust=TRUST_CRITERIA, urgency=URGENCY_CRITERIA, importance=IMPORTANCE_CRITERIA
        ),
        # These tests are about the three core questions; extras have their own tests.
        assessment_questions={},
    )


def make_request() -> PaymentRequest:
    return PaymentRequest(
        request_id="req-test",
        category=RequestCategory.SUPPLY_PURCHASE,
        amount_usdc=180.0,
        requester="test-suite",
        description="A test payment request for the fallback engine.",
    )


def fake_response(trust_score: float, urgency_score: float, importance_score: float) -> dict:
    return {
        "model": "kev-latest",
        "answers": {
            "trust": {
                "type": "score",
                "score": trust_score,
                "legend": dict(enumerate(TRUST_CRITERIA)),
                "probabilities": {},
                "confidence": 0.7,
            },
            "urgency": {
                "type": "score",
                "score": urgency_score,
                "legend": dict(enumerate(URGENCY_CRITERIA)),
                "probabilities": {},
                "confidence": 0.65,
            },
            "importance": {
                "type": "score",
                "score": importance_score,
                "legend": dict(enumerate(IMPORTANCE_CRITERIA)),
                "probabilities": {},
                "confidence": 0.75,
            },
        },
        "usage": {"input_tokens": 150, "output_tokens": 30},
    }


@patch("src.decision_engine.system_one.load_config")
@patch("src.decision_engine._http.httpx.post")
def test_fallback_engine_normalizes_scores(mock_post, mock_load_config):
    mock_load_config.return_value = fake_rules_config()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_response(2.0, 0.0, 1.0)
    mock_post.return_value = mock_response

    engine = TypeSafeCompatibleEngine(api_url="http://127.0.0.1:8009/v1/systemone")
    scores = engine.score(make_request())

    assert scores.trust.value == 1.0
    assert scores.urgency.value == 0.0
    assert scores.importance.value == 0.5
    assert scores.urgency.confidence == 0.65


@patch("src.decision_engine.system_one.load_config")
@patch("src.decision_engine._http.httpx.post")
def test_fallback_engine_uses_configured_model_and_url(mock_post, mock_load_config):
    mock_load_config.return_value = fake_rules_config()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_response(1.0, 1.0, 1.0)
    mock_post.return_value = mock_response

    engine = TypeSafeCompatibleEngine(
        api_url="http://127.0.0.1:9999/v1/systemone",
        model="kev-4b",
    )
    engine.score(make_request())

    args, kwargs = mock_post.call_args
    assert args[0] == "http://127.0.0.1:9999/v1/systemone"
    assert kwargs["json"]["model"] == "kev-4b"
    assert set(kwargs["json"]["questions"].keys()) == {"trust", "urgency", "importance"}


@patch("src.decision_engine.system_one.load_config")
@patch("src.decision_engine._http.httpx.post")
def test_score_wraps_connection_error_with_kev_start_instructions(mock_post, mock_load_config):
    mock_load_config.return_value = fake_rules_config()
    mock_post.side_effect = httpx.ConnectError("Connection refused")

    engine = TypeSafeCompatibleEngine(api_url="http://127.0.0.1:8009/v1/systemone")
    with pytest.raises(RuntimeError, match="Is Kev running"):
        engine.score(make_request())


def test_score_raises_clear_error_when_api_key_empty():
    engine = TypeSafeCompatibleEngine(api_url="http://127.0.0.1:8009/v1/systemone", api_key="")
    with pytest.raises(RuntimeError, match="FALLBACK_API_KEY is empty"):
        engine.score(make_request())
