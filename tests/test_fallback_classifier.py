"""Tests for the generic TypeSafe-compatible fallback engine (Kev by default).

These mock the HTTP call, same approach as test_jev_client.py: we are
checking that this engine behaves identically to JevDecisionEngine given
the same response shape, since both must be interchangeable behind the
DecisionEngine interface.
"""

from unittest.mock import MagicMock, patch

from src.decision_engine.criteria import IMPORTANCE_CRITERIA, TRUST_CRITERIA, URGENCY_CRITERIA
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.requests.schema import PaymentRequest, RequestCategory


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
                "legend": {str(i): c for i, c in enumerate(TRUST_CRITERIA)},
                "probabilities": {},
                "confidence": 0.7,
            },
            "urgency": {
                "type": "score",
                "score": urgency_score,
                "legend": {str(i): c for i, c in enumerate(URGENCY_CRITERIA)},
                "probabilities": {},
                "confidence": 0.65,
            },
            "importance": {
                "type": "score",
                "score": importance_score,
                "legend": {str(i): c for i, c in enumerate(IMPORTANCE_CRITERIA)},
                "probabilities": {},
                "confidence": 0.75,
            },
        },
        "usage": {"input_tokens": 150, "output_tokens": 30},
    }


@patch("src.decision_engine._http.httpx.post")
def test_fallback_engine_normalizes_scores(mock_post):
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


@patch("src.decision_engine._http.httpx.post")
def test_fallback_engine_uses_configured_model_and_url(mock_post):
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
