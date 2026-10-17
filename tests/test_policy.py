"""Tests for the policy layer -- the part that decides, not just scores.

load_config() is mocked in both src.policy.rules and src.policy.limits (each
module imports it separately) so these tests never touch the real
config/rules.yaml on disk, and always run against a fixed, known set of
thresholds/limits regardless of what a user has configured.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.answers import ChoiceAnswer, NoulAnswer
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds


def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(),
        thresholds=thresholds(),
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


def make_scores_with_fraud(fraud: float | None, trust: float = 0.95, confidence: float = 0.9):
    """High-trust scores that WOULD auto-approve, plus a fraud answer."""
    extra = {} if fraud is None else {"fraud_indicators": NoulAnswer(noul=fraud)}
    return DecisionScores(
        trust=ScoreResult(value=trust, confidence=confidence),
        urgency=ScoreResult(value=0.1, confidence=confidence),
        importance=ScoreResult(value=0.1, confidence=confidence),
        extra=extra,
    )


def _nature(option: str) -> ChoiceAnswer:
    """A `natureza_transacao` answer picking one option."""
    return ChoiceAnswer(choice=option, confidence=0.9, probabilities={option: 1.0})


# --------------------------------------------------------------------------
# The fraud veto
# --------------------------------------------------------------------------


def test_a_fraud_signal_vetoes_an_otherwise_perfect_request():
    """The whole point of the veto: nothing else can buy a machine approval.

    Before this existed the README promised "a veto for fraud" while
    apply_policy never read the answer at all.
    """
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    # Sanity: without a fraud signal these scores auto-approve.
    assert apply_policy(request, make_scores_with_fraud(None)).decision == Decision.APPROVE

    for fraud in (0.7, 0.74, 0.99, 1.0):
        result = apply_policy(request, make_scores_with_fraud(fraud))
        assert result.decision == Decision.ESCALATE, f"P(fraud)={fraud} must veto"
        assert "Fraud indicators" in result.reason
        assert result.reduced_amount is None


def test_the_veto_fires_exactly_at_the_configured_threshold():
    """Boundary: the bar is inclusive, and just below it nothing changes."""
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    assert apply_policy(request, make_scores_with_fraud(0.69)).decision == Decision.APPROVE
    assert apply_policy(request, make_scores_with_fraud(0.7)).decision == Decision.ESCALATE


def test_the_veto_threshold_is_configurable():
    """It is a threshold, not a constant: a business can move it."""
    strict = fake_rules_config()
    strict.thresholds.fraud_veto_at = 0.9
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    with patch("src.policy.rules.load_config", return_value=strict):
        assert apply_policy(request, make_scores_with_fraud(0.74)).decision == Decision.APPROVE
        assert apply_policy(request, make_scores_with_fraud(0.95)).decision == Decision.ESCALATE


def test_a_missing_fraud_answer_does_not_block_a_good_request():
    """Fail-safe direction: an engine that did not answer must not stall the
    queue. The veto can only ever stop an automatic approval, never force an
    escalation on its own."""
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    # No answer at all.
    assert apply_policy(request, make_scores_with_fraud(None)).decision == Decision.APPROVE

    # An answer of the wrong type (a `choice` where a `noul` was expected).
    wrong_type = make_scores_with_fraud(None)
    wrong_type.extra["fraud_indicators"] = ChoiceAnswer(
        choice="x", confidence=0.9, probabilities={"x": 1.0}
    )
    assert apply_policy(request, wrong_type).decision == Decision.APPROVE


# --------------------------------------------------------------------------
# Auto-approval must not depend on confidence (the removed gate)
# --------------------------------------------------------------------------


def test_auto_approval_does_not_depend_on_confidence():
    """Pinned so the removed gate cannot come back by accident.

    The rule used to require `trust.confidence >= 0.7`. Calibration on 39 real
    answers showed the engine never emits a trust confidence above 0.60, so the
    clause never fired: measured on that data, removing it took the good
    scenarios from 0/12 approved to 6/12 while the bad ones stayed at 0/12
    either way. The values below include the ones that used to be blocked.
    """
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    for confidence in (0.0, 0.2185, 0.4988, 0.69, 1.0):
        result = apply_policy(
            request, make_scores(trust=0.9, urgency=0.1, importance=0.1, confidence=confidence)
        )
        assert result.decision == Decision.APPROVE, (
            f"confidence={confidence} must not block a high trust score: the "
            "engine never exceeds ~0.6, so a threshold there is a rule that "
            "never fires"
        )


def test_the_confidence_threshold_is_gone_from_the_config():
    """A knob that does nothing is worse than no knob."""
    from src.config import DEFAULT_THRESHOLDS, load_config

    assert "trust_min_confidence_for_auto_approve" not in DEFAULT_THRESHOLDS
    assert not hasattr(load_config().thresholds, "trust_min_confidence_for_auto_approve")


def test_the_nature_question_vetoes_on_its_own():
    """The second veto path, which existed in every answer and was never read.

    `natureza_transacao` offers `risco_fraude` as an explicit choice. The engine
    saying so outright must stop an automatic approval even when the separate
    probability question came back low -- otherwise the policy ignores a signal
    the model went out of its way to give.
    """
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    # Sanity: a benign nature plus a low probability approves.
    benign = make_scores_with_fraud(0.05)
    benign.extra["natureza_transacao"] = _nature("compra_padrao")
    assert apply_policy(request, benign).decision == Decision.APPROVE

    fraudulent = make_scores_with_fraud(0.05)  # the probability is LOW
    fraudulent.extra["natureza_transacao"] = _nature("risco_fraude")
    result = apply_policy(request, fraudulent)

    assert result.decision == Decision.ESCALATE
    assert "Fraud indicators" in result.reason and "risco_fraude" in result.reason


def test_the_two_veto_paths_are_independent():
    """Either one fires; neither needs the other."""
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    # Only the probability is high.
    only_probability = make_scores_with_fraud(0.95)
    only_probability.extra["natureza_transacao"] = _nature("compra_padrao")
    assert apply_policy(request, only_probability).decision == Decision.ESCALATE

    # Only the choice says fraud.
    only_choice = make_scores_with_fraud(0.05)
    only_choice.extra["natureza_transacao"] = _nature("risco_fraude")
    assert apply_policy(request, only_choice).decision == Decision.ESCALATE

    # Neither.
    neither = make_scores_with_fraud(0.05)
    neither.extra["natureza_transacao"] = _nature("compra_padrao")
    assert apply_policy(request, neither).decision == Decision.APPROVE


def test_a_missing_or_mistyped_nature_answer_does_not_veto():
    """Same fail-safe direction as the probability: absence is not evidence."""
    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE)

    # No answer to the nature question at all.
    assert apply_policy(request, make_scores_with_fraud(0.05)).decision == Decision.APPROVE

    # A `noul` where a `choice` was expected.
    wrong_type = make_scores_with_fraud(0.05)
    wrong_type.extra["natureza_transacao"] = NoulAnswer(noul=0.9)
    assert apply_policy(request, wrong_type).decision == Decision.APPROVE


def test_the_veto_does_not_deny_a_human_override():
    """It escalates, it never refuses: a person can still approve deliberately.

    This is checked through the pipeline, because that is where the promise
    could be broken -- if the veto reached apply_human_override it would make
    human review pointless.
    """
    from src.pipeline import apply_human_override
    from src.audit.logger import AuditEntry

    request = make_request(100, RequestCategory.OPERATIONAL_EXPENSE).model_copy(
        update={"counterparty_wallet": "0x" + "c" * 40}
    )
    fraudulent_scores = make_scores_with_fraud(0.99)
    escalated = AuditEntry(
        timestamp="2026-10-05T12:00:00+00:00",
        request=request,
        scores=fraudulent_scores,
        decision="escalate",
        reduced_amount=None,
        reason="Fraud indicators vetoed automatic approval.",
        payment_executed=False,
    )

    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}
    with (
        patch("src.pipeline.append_entry"),
        patch("src.pipeline.ArcPaymentClient"),
        patch("src.pipeline.read_latest_per_request", return_value=[escalated]),
        patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"}),
    ):
        entry = apply_human_override(escalated, "approve", "ana", arc_client=arc_client)

    assert entry.human_review == "approved"
    assert entry.payment_executed is True


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


# --------------------------------------------------------------------------
# The reduced amount is exact
# --------------------------------------------------------------------------


def reduced_decision(limit: float, fraction: float, amount: float):
    from tests.rules_fixtures import rules_config

    config = rules_config(
        limits=category_limits(operational_expense=limit), reduced_amount_fraction=fraction
    )
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        return apply_policy(
            make_request(amount, RequestCategory.OPERATIONAL_EXPENSE),
            make_scores(trust=0.5, urgency=0.3, importance=0.8),
        )


def test_the_reduced_amount_has_no_float_noise():
    """333.33 * 0.35 is 116.66549999999999 in floats: more places than USDC has."""
    from decimal import Decimal

    assert 333.33 * 0.35 != 116.6655
    result = reduced_decision(limit=333.33, fraction=0.35, amount=333.33)

    assert result.decision == Decision.APPROVE_REDUCED
    assert result.reduced_amount == 116.6655
    assert Decimal(repr(result.reduced_amount)) == Decimal("116.6655")


def test_the_reduced_amount_is_rounded_down_never_up():
    """100.5 * 0.33333333 = 33.499999665: rounding to nearest would authorize 33.5."""
    result = reduced_decision(limit=100.5, fraction=0.33333333, amount=100.5)
    assert result.reduced_amount == 33.499999


def test_a_request_smaller_than_the_cap_is_paid_in_full():
    result = reduced_decision(limit=500.0, fraction=0.5, amount=10.0)
    assert result.reduced_amount == 10.0


def test_a_cap_that_rounds_to_zero_escalates_instead_of_approving_nothing():
    result = reduced_decision(limit=0.000001, fraction=0.5, amount=0.000001)
    assert result.decision == Decision.ESCALATE
    assert result.reduced_amount is None
    assert "zero" in result.reason
