"""The engine's timeout: configurable, and genuinely absent when disabled."""

from unittest.mock import MagicMock, patch

import pytest

from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.system_one import DEFAULT_TIMEOUT_SECONDS, timeout_from_env
from src.requests.schema import PaymentRequest
from tests.fake_engine_server import fake_engine


def request() -> PaymentRequest:
    return PaymentRequest(request_id="r", category="other", amount_usdc=10.0, requester="t", description="d")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 15.0), ("", 15.0), ("  ", 15.0), ("45", 45.0), ("2.5", 2.5),
     ("none", None), ("NONE", None), ("off", None), ("no", None), ("0", None)],
)
def test_timeout_is_read_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.delenv("ENGINE_TIMEOUT_SECONDS", raising=False)
    if raw is not None:
        monkeypatch.setenv("ENGINE_TIMEOUT_SECONDS", raw)
    assert timeout_from_env() == expected
    assert DEFAULT_TIMEOUT_SECONDS == 15.0


@pytest.mark.parametrize("bad", ["abc", "-3", "1,5", "fast"])
def test_a_garbled_timeout_is_a_clear_error_not_a_silent_default(monkeypatch, bad):
    monkeypatch.setenv("ENGINE_TIMEOUT_SECONDS", bad)
    with pytest.raises(RuntimeError, match="ENGINE_TIMEOUT_SECONDS"):
        timeout_from_env()


def sent_timeout(**engine_kwargs):
    response = MagicMock(status_code=200)
    response.json.return_value = {"answers": {k: {"type": "score", "score": 1, "confidence": 0.5, "probabilities": {}}
                                              for k in ("trust", "urgency", "importance")}}
    with patch("src.decision_engine.system_one.load_config") as cfg, \
         patch("src.decision_engine._http.httpx.post", return_value=response) as post:
        from src.config import load_config as real
        cfg.return_value = real().model_copy(update={"assessment_questions": {}})
        TypeSafeCompatibleEngine(**engine_kwargs).score(request())
    return post.call_args.kwargs["timeout"]


def test_the_default_timeout_is_15_seconds(monkeypatch):
    monkeypatch.delenv("ENGINE_TIMEOUT_SECONDS", raising=False)
    assert sent_timeout() == 15.0


def test_the_environment_can_disable_the_timeout(monkeypatch):
    monkeypatch.setenv("ENGINE_TIMEOUT_SECONDS", "none")
    assert sent_timeout() is None


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ENGINE_TIMEOUT_SECONDS", "none")
    assert sent_timeout(timeout=7) == 7


def test_a_slow_engine_times_out_by_default_but_not_when_the_timeout_is_off():
    """The whole point, over real HTTP: a model that needs 1.5 s."""
    with fake_engine(delay_seconds=1.5) as (url, _):
        impatient = TypeSafeCompatibleEngine(api_url=url, timeout=0.4)
        with pytest.raises(RuntimeError, match="Could not reach the decision engine"):
            impatient.score(request())

        patient = TypeSafeCompatibleEngine(api_url=url, timeout=None)
        scores = patient.score(request())

    assert scores.evidence.model == "fake-1"
