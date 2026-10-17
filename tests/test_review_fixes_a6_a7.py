"""Regressions for two engine-boundary defects: a malformed engine answer and a
blank input in settings.

The first had three symptoms with one root cause: the engine's response was
trusted without being checked. `body["answers"]` raised a bare `KeyError` when
the key was absent, a score or confidence outside its range escaped as a Pydantic
`ValidationError`, and either way the request failed with no trace in the audit
log -- in a system whose whole pitch is that every decision is recorded.

The second is a browser-side defect with the same shape: `ui/settings.html`
turned an empty or unparseable input into `0` (`parseFloat(value) || 0`), and `0`
passes every server validator, so a blank field on the thresholds screen silently
became `trust_min_auto_approve = 0` -- a policy that approves everything.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import RulesConfig, ScoringCriteria
from src.decision_engine.jev_client import JevDecisionEngine
from src.pipeline import run_pipeline
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds

UI = Path(__file__).resolve().parent.parent / "ui" / "settings.html"


def request() -> PaymentRequest:
    return PaymentRequest(
        request_id="a6-1",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="test-suite",
        description="A request whose engine call will fail.",
        counterparty_wallet="0x" + "ab" * 20,
    )


def rules() -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        require_known_payee=False,
    )


def valid_answers() -> dict:
    return {
        "trust": {"type": "score", "score": 1.0, "confidence": 0.8},
        "urgency": {"type": "score", "score": 0.0, "confidence": 0.8},
        "importance": {"type": "score", "score": 0.0, "confidence": 0.8},
        **{q: {"type": "noul", "noul": 0.05} for q in ("fraud_indicators", "description_matches_category")},
        "natureza_transacao": {
            "type": "choice",
            "choice": "compra_padrao",
            "confidence": 0.7,
            "probabilities": {"compra_padrao": 0.7, "assinatura_recorrente": 0.1,
                              "transferencia_atipica": 0.1, "risco_fraude": 0.1},
        },
    }


def score_with(body):
    """Score a request against a canned engine response body."""
    response = MagicMock(status_code=200)
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    with patch("src.decision_engine.system_one.load_config", return_value=rules()), patch(
        "src.decision_engine._http.httpx.post", return_value=response
    ):
        return JevDecisionEngine(api_key="k").score(request())


# --------------------------------------------------------------------------
# a malformed engine answer is a typed error, never a bare KeyError
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expect",
    [
        ({"model": "m"}, "answers"),
        ({"model": "m", "answers": "not a mapping"}, "answers"),
        ({}, "answers"),
    ],
)
def test_a6_a_response_without_usable_answers_is_a_typed_error(body, expect):
    """`body["answers"]` used to raise a bare `KeyError: 'answers'`, which the
    API turned into an opaque 502 and nothing recorded."""
    from src.decision_engine.system_one import EngineResponseError

    with pytest.raises(EngineResponseError, match=expect):
        score_with(body)


@pytest.mark.parametrize(
    "broken, expect",
    [
        ({"type": "score", "score": 99, "confidence": 0.9}, "trust"),
        ({"type": "score", "score": "high", "confidence": 0.9}, "trust"),
        ({"type": "score", "score": 1.0, "confidence": 5.0}, "trust"),
        ({"type": "score", "score": None, "confidence": 0.9}, "trust"),
        ({"type": "score", "confidence": 0.9}, "trust"),
    ],
)
def test_a6_an_out_of_contract_core_answer_is_a_typed_error(broken, expect):
    """A score off the scale or a confidence outside [0, 1] used to escape as a
    Pydantic `ValidationError` from deep inside the engine layer."""
    from src.decision_engine.system_one import EngineResponseError

    answers = valid_answers()
    answers["trust"] = broken

    with pytest.raises(EngineResponseError, match=expect):
        score_with({"model": "m", "answers": answers})


def test_a6_a_valid_response_still_scores_unchanged():
    """The validation must not reject what the engine actually sends."""
    scores = score_with({"model": "m", "answers": valid_answers()})

    assert scores.trust.value == 1.0
    assert scores.trust.confidence == 0.8
    assert "fraud_indicators" in scores.extra


def test_a6_the_error_is_still_a_runtime_error_for_callers_that_catch_it():
    """`EngineResponseError` is a `RuntimeError`, so the CLI's error path and the
    engine's own "unreachable" handling keep working unchanged."""
    from src.decision_engine.system_one import EngineResponseError

    assert issubclass(EngineResponseError, RuntimeError)


# --------------------------------------------------------------------------
# a failed engine call leaves a trace in the audit log
# --------------------------------------------------------------------------


class FakeLog:
    def __init__(self):
        self.rows: list = []

    def append(self, entry, *_a, **_k):
        self.rows.append(entry)

    def latest(self):
        by_id = {}
        for row in self.rows:
            by_id[row.request.request_id] = row
        return list(by_id.values())


@pytest.fixture
def fake_log():
    log = FakeLog()
    with (
        patch("src.pipeline.append_entry", side_effect=log.append),
        patch("src.pipeline.read_latest_per_request", side_effect=log.latest),
        patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key", "ESCROW_ENABLED": "false"}),
    ):
        yield log


def test_a6_a_dead_engine_is_recorded_in_the_audit_log(fake_log):
    """Measured before this: the request failed, the API returned a 5xx, and the
    log was untouched -- so a system sold on "every decision is recorded" lost
    the one event an operator most needs to see."""
    from src.decision_engine.system_one import EngineResponseError

    engine = MagicMock()
    engine.name = "jev"
    engine.score.side_effect = EngineResponseError("The engine's answer is missing or malformed.")

    with (
        patch("src.policy.rules.load_config", return_value=rules()),
        patch("src.policy.limits.load_config", return_value=rules()),
        pytest.raises(EngineResponseError),
    ):
        run_pipeline(request(), engine=engine, verbose=False)

    assert len(fake_log.rows) == 1, "the failed attempt must leave exactly one row"
    row = fake_log.rows[0]
    assert row.request.request_id == "a6-1"
    assert row.failed_attempt is not None
    assert row.failed_attempt.action == "score"
    assert "malformed" in row.failed_attempt.error
    # Nothing was decided and nothing moved, so the row must not claim either.
    assert row.payment_executed is False
    assert row.human_review is None
    assert row.decision == "error"


def test_a6_the_same_broken_request_is_not_recorded_twice(fake_log):
    """Repeating the request must not let anyone grow the hash-chained log
    without bound -- the same concern `_record_failed_attempt` already handles
    for escrow actions."""
    from src.decision_engine.system_one import EngineResponseError
    from src.pipeline import DuplicateRequestError

    engine = MagicMock()
    engine.name = "jev"
    engine.score.side_effect = EngineResponseError("same failure")

    with (
        patch("src.policy.rules.load_config", return_value=rules()),
        patch("src.policy.limits.load_config", return_value=rules()),
    ):
        with pytest.raises(EngineResponseError):
            run_pipeline(request(), engine=engine, verbose=False)
        rows_after_first = len(fake_log.rows)
        with pytest.raises((EngineResponseError, DuplicateRequestError)):
            run_pipeline(request(), engine=engine, verbose=False)

    assert len(fake_log.rows) == rows_after_first, "a repeat must not append another row"


# --------------------------------------------------------------------------
# what the two audiences see when the engine cannot answer
# --------------------------------------------------------------------------


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from src.api import app
    from tests.conftest import MANAGER_AUTH

    # `raise_server_exceptions=False` is what makes the app's own exception
    # handler run: with the default, TestClient re-raises the exception instead
    # of letting the app turn it into a response, so the handler would never be
    # exercised -- which is the whole point of these two tests.
    return TestClient(app, headers=MANAGER_AUTH, raise_server_exceptions=False)


def _failing_engine():
    from src.decision_engine.system_one import EngineResponseError

    engine = MagicMock()
    engine.name = "jev"
    engine.score.side_effect = EngineResponseError(
        "The engine's response has no usable 'answers' mapping."
    )
    return engine


def test_a6_the_manager_route_explains_an_unusable_engine_answer(api_client, fake_log):
    """The dashboard shows `detail`; it must name the real problem instead of
    an opaque "Internal Server Error"."""
    payload = {
        "request_id": "api-a6-1",
        "category": "operational_expense",
        "amount_usdc": 50.0,
        "requester": "ui-form",
        "description": "Submitted through the dashboard.",
    }

    with patch("src.pipeline.DEFAULT_ENGINE_FACTORY", return_value=_failing_engine()), patch(
        "src.policy.rules.load_config", return_value=rules()
    ), patch("src.policy.limits.load_config", return_value=rules()):
        response = api_client.post("/api/requests", json=payload)

    assert response.status_code == 502
    assert "answers" in response.json()["detail"]


def test_a6_the_requester_route_keeps_the_engine_internals_private(api_client, fake_log):
    """Requesters are unauthenticated: they get a generic message and a 503,
    never the engine's address, its response body or the question ids."""
    payload = {
        "category": "operational_expense",
        "amount_usdc": 50.0,
        "requester": "ui-form",
        "description": "Submitted through the portal.",
    }

    with patch("src.pipeline.DEFAULT_ENGINE_FACTORY", return_value=_failing_engine()), patch(
        "src.policy.rules.load_config", return_value=rules()
    ), patch("src.policy.limits.load_config", return_value=rules()):
        response = api_client.post("/api/requester/requests", json=payload)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "answers" not in detail
    assert "engine" not in detail.lower()
    assert "try again" in detail.lower()


# --------------------------------------------------------------------------
# a blank input in settings.html must not become a policy of zero
# --------------------------------------------------------------------------


def settings_source() -> str:
    return UI.read_text(encoding="utf-8")


def test_a7_the_settings_page_has_no_silent_zero_fallback():
    """`parseFloat(value) || 0` turned an EMPTY field into `0`, and `0` passes
    every server validator: `trust_min_auto_approve = 0` approves everything.
    A blank field must be reported, not coerced."""
    source = settings_source()

    assert "parseFloat(input.value) || 0" not in source
    assert "parseFloat(ta.value) || 0" not in source


def test_a7_a_blank_or_invalid_number_is_reported_instead_of_saved():
    """The parsed value must be checked before it is written into the config."""
    source = settings_source()

    # A single guarded reader, so the three call sites cannot drift apart.
    assert "function readNumberField" in source or "function parseNumberInput" in source
    assert "Number.isFinite" in source, "a NaN must be detected before it reaches the config"
    # ...and the save path must be able to refuse.
    assert "reportProblems" in source
    assert "if (!readFormIntoConfig())" in source


def test_a7_the_number_inputs_declare_their_constraints():
    """The browser should refuse an empty or out-of-range value before the
    request is even built, so the server validator is the second line."""
    source = settings_source()

    assert 'type="number"' in source
    limits_section = source.split("function renderLimits()")[1].split("function renderThresholds()")[0]
    assert 'min="0"' in limits_section, "a category ceiling must not accept a negative or blank value"
    assert "required" in limits_section
