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


# --------------------------------------------------------------------------
# Extra questions and history
# --------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from src.audit.logger import AuditEntry  # noqa: E402
from src.config import DEFAULT_ASSESSMENT_QUESTIONS, load_config  # noqa: E402
from src.history import compute_history  # noqa: E402

WALLET = "0x" + "b" * 40


def milestone_request(description="Milestone verified, documented, clean payment history.", amount=100.0, wallet=WALLET):
    return make_request(description=description, amount_usdc=amount, counterparty_wallet=wallet)


def past_payment(request_id, amount=100.0, **fields) -> AuditEntry:
    base = milestone_request(amount=amount)
    return AuditEntry(
        timestamp=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        request=base.model_copy(update={"request_id": request_id}),
        scores=MockDecisionEngine().score(base), decision="approve", reduced_amount=None, reason="r",
        payment_executed=fields.pop("payment_executed", True), **fields,
    )


def test_every_configured_extra_question_is_answered_in_the_real_schema():
    scores = MockDecisionEngine().score(milestone_request(), history=compute_history(milestone_request(), []))
    assert set(scores.extra) == set(DEFAULT_ASSESSMENT_QUESTIONS)
    assert scores.evidence.problems == []  # the mock's own output passed the real parser
    assert scores.evidence.model == "mock"


def test_the_mock_records_what_it_was_shown():
    request = milestone_request()
    history = compute_history(request, [])
    assert MockDecisionEngine().score(request, history=history).evidence.state["history"]["destination"]["known"] is False


def test_a_red_flag_raises_fraud_and_changes_the_nature():
    scores = MockDecisionEngine().score(milestone_request("Suspicious, unknown vendor, vague invoice."))
    assert scores.extra["fraud_indicators"].noul >= 0.5
    assert scores.extra["natureza_transacao"].choice == "risco_fraude"


def test_a_clean_documented_request_looks_safe():
    scores = MockDecisionEngine().score(milestone_request())
    assert scores.extra["fraud_indicators"].noul < 0.2
    assert scores.extra["natureza_transacao"].choice == "compra_padrao"
    assert scores.extra["description_matches_category"].noul > 0.7


def test_a_vague_description_does_not_match_its_category():
    """The `description_matches_category` signal, which the policy reads."""
    scores = MockDecisionEngine().score(milestone_request("Payment for services, vague."))
    assert scores.extra["description_matches_category"].noul < 0.3


def test_an_amount_far_above_the_usual_changes_the_nature():
    request = milestone_request(amount=900.0)
    history = compute_history(request, [past_payment(f"p{i}", amount=100.0) for i in range(3)])  # usual: 100
    scores = MockDecisionEngine().score(request, history=history)

    assert scores.extra["natureza_transacao"].choice == "transferencia_atipica"


def test_a_missing_wallet_is_a_mild_warning_not_a_veto():
    request = milestone_request(wallet=None)
    scores = MockDecisionEngine().score(request, history=compute_history(request, []))
    assert 0.1 < scores.extra["fraud_indicators"].noul < 0.5


def test_a_choice_question_uses_only_the_options_that_are_configured():
    """If someone renames the options, the mock must not invent one."""
    from unittest.mock import patch

    config = load_config().model_copy(deep=True)
    config.assessment_questions = {"natureza_transacao": config.assessment_questions["natureza_transacao"].model_copy(
        update={"criteria": {"alpha": "a", "beta": "b"}})}
    with patch("src.decision_engine.mock_engine.load_config", return_value=config):
        scores = MockDecisionEngine().score(milestone_request())
    assert scores.extra["natureza_transacao"].choice in {"alpha", "beta"} and scores.evidence.problems == []


def test_extras_can_be_switched_off():
    from unittest.mock import patch

    config = load_config().model_copy(update={"assessment_questions": {}})
    with patch("src.decision_engine.mock_engine.load_config", return_value=config):
        scores = MockDecisionEngine().score(milestone_request())
    assert scores.extra == {} and set(scores.evidence.answers) == {"trust", "urgency", "importance"}
