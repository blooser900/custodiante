"""Tests for the Jev decision engine client.

These do not hit the real Jev API -- they mock httpx.post so we can check
that the request is shaped correctly and, more importantly, that the raw
Jev score (which lands in [0, num_levels - 1]) is normalized to [0, 1]
before it reaches the policy layer.
"""

from unittest.mock import MagicMock, patch

from src.decision_engine.criteria import (
    IMPORTANCE_CRITERIA,
    TRUST_CRITERIA,
    URGENCY_CRITERIA,
    normalize_score,
)
from src.decision_engine.jev_client import JevDecisionEngine
from src.requests.schema import PaymentRequest, RequestCategory


def make_request() -> PaymentRequest:
    return PaymentRequest(
        request_id="req-test",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=250.0,
        requester="test-suite",
        description="A test payment request for the Jev client.",
    )


def fake_jev_response(trust_score: float, urgency_score: float, importance_score: float) -> dict:
    """Build a response shaped like the real Jev API (see docs.typesafe.ai/api)."""
    return {
        "model": "jev-1.13.0",
        "answers": {
            "trust": {
                "type": "score",
                "score": trust_score,
                "legend": {str(i): c for i, c in enumerate(TRUST_CRITERIA)},
                "probabilities": {},
                "confidence": 0.85,
            },
            "urgency": {
                "type": "score",
                "score": urgency_score,
                "legend": {str(i): c for i, c in enumerate(URGENCY_CRITERIA)},
                "probabilities": {},
                "confidence": 0.80,
            },
            "importance": {
                "type": "score",
                "score": importance_score,
                "legend": {str(i): c for i, c in enumerate(IMPORTANCE_CRITERIA)},
                "probabilities": {},
                "confidence": 0.90,
            },
        },
        "usage": {"input_tokens": 200, "output_tokens": 40},
    }


def test_normalize_maps_raw_score_to_zero_one_range():
    # 3-level rubric: raw scores span [0, 2]
    assert normalize_score(0.0, 3) == 0.0
    assert normalize_score(1.0, 3) == 0.5
    assert normalize_score(2.0, 3) == 1.0


@patch("src.decision_engine._http.httpx.post")
def test_score_normalizes_and_maps_confidence(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_jev_response(
        trust_score=2.0, urgency_score=1.0, importance_score=0.0
    )
    mock_post.return_value = mock_response

    engine = JevDecisionEngine(api_key="fake-key")
    scores = engine.score(make_request())

    assert scores.trust.value == 1.0  # top of a 3-level rubric
    assert scores.urgency.value == 0.5  # middle level
    assert scores.importance.value == 0.0  # bottom level
    assert scores.trust.confidence == 0.85


@patch("src.decision_engine._http.httpx.post")
def test_score_sends_expected_request_shape(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_jev_response(1.0, 1.0, 1.0)
    mock_post.return_value = mock_response

    engine = JevDecisionEngine(api_key="fake-key", api_url="https://api.typesafe.ai/v1/systemone")
    engine.score(make_request())

    _, kwargs = mock_post.call_args
    payload = kwargs["json"]

    assert payload["model"] == "jev-latest"
    assert set(payload["questions"].keys()) == {"trust", "urgency", "importance"}
    assert payload["questions"]["trust"]["type"] == "score"
    assert payload["questions"]["trust"]["criteria"] == TRUST_CRITERIA
    assert kwargs["headers"]["Authorization"] == "Bearer fake-key"
