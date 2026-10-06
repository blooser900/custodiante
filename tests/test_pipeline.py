"""Tests that pipeline.run_pipeline wires the decision engine, policy layer,
and Arc payment execution together correctly -- with everything external
mocked (no real Jev/Kev server, no real Circle credentials needed).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.pipeline import run_pipeline
from src.requests.schema import PaymentRequest, RequestCategory


def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits={"operational_expense": 500.0, "other": 200.0},
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


def make_request(counterparty_wallet: str | None) -> PaymentRequest:
    return PaymentRequest(
        request_id="main-test-1",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="test-suite",
        description="A request used to test main.py wiring.",
        counterparty_wallet=counterparty_wallet,
    )


def make_high_trust_engine() -> MagicMock:
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.95, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    return engine


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_with_wallet_executes_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {
        "transaction_id": "tx-123",
        "state": "COMPLETE",
        "tx_hash": "0xabc123",
        "destination_wallet": "0xdeadbeef",
        "amount_usdc": 100.0,
        "memo": "payment for request main-test-1",
    }

    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_called_once()
    _, kwargs = arc_client.send_payment.call_args
    assert kwargs["destination_wallet"] == "0xdeadbeef"
    assert kwargs["amount_usdc"] == 100.0

    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is True
    # The whole point of this fix: transaction_id/tx_hash must actually
    # reach the audit log, not just the console print.
    assert logged_entry.transaction_id == "tx-123"
    assert logged_entry.tx_hash == "0xabc123"


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {}, clear=True)
def test_approved_request_without_credentials_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False
    assert logged_entry.transaction_id is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_without_wallet_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    run_pipeline(make_request(None), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_failed_transfer_is_logged_not_raised(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    # Should not raise -- a failed on-chain transfer is recorded, not fatal.
    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False
    assert logged_entry.transaction_id is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_rejected_destination_address_is_logged_not_raised(mock_append_entry):
    """A malformed address or a self-transfer must not crash the pipeline --
    ArcPaymentClient raises ValueError for these (see _validate_destination),
    and main.py must catch that the same way it catches a failed transfer."""
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = ValueError("not a well-formed EVM address")

    run_pipeline(make_request("not-a-real-address"), engine=engine, arc_client=arc_client)

    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False


# --------------------------------------------------------------------------
# Payment outcome is recorded in the audit entry (not just printed)
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_executed_payment_is_marked_executed(mock_append_entry):
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=arc_client)

    entry = mock_append_entry.call_args[0][0]
    assert entry.payment_status == "executed"
    assert entry.payment_detail is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_failed_payment_records_status_and_reason(mock_append_entry):
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=arc_client)

    entry = mock_append_entry.call_args[0][0]
    assert entry.payment_status == "failed"
    assert "transfer DENIED" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch("src.pipeline.ArcPaymentClient", side_effect=Exception("bad credentials"))
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_arc_client_construction_failure_is_recorded_not_raised(_mock_cls, mock_append_entry):
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine())

    entry = mock_append_entry.call_args[0][0]
    assert entry.payment_status == "failed"
    assert "bad credentials" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_skipped_payment_says_why_for_missing_wallet(mock_append_entry):
    run_pipeline(make_request(None), engine=make_high_trust_engine(), arc_client=MagicMock())

    entry = mock_append_entry.call_args[0][0]
    assert entry.payment_status == "skipped"
    assert "counterparty_wallet" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {}, clear=True)
def test_skipped_payment_says_why_for_missing_api_key(mock_append_entry):
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=MagicMock())

    entry = mock_append_entry.call_args[0][0]
    assert entry.payment_status == "skipped"
    assert "CIRCLE_API_KEY" in entry.payment_detail


@patch("src.pipeline.append_entry")
def test_escalated_request_has_no_payment_applicable(mock_append_entry):
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.1, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )

    run_pipeline(make_request("0xdeadbeef"), engine=engine)

    entry = mock_append_entry.call_args[0][0]
    assert entry.decision == "escalate"
    assert entry.payment_status == "not_applicable"


@patch("src.pipeline.append_entry")
def test_engine_name_is_recorded(mock_append_entry):
    engine = make_high_trust_engine()
    engine.name = "kev"

    run_pipeline(make_request(None), engine=engine)

    assert mock_append_entry.call_args[0][0].engine_name == "kev"


# --------------------------------------------------------------------------
# Idempotency: a request is decided and paid at most once
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_resubmitting_a_processed_request_id_is_rejected_before_any_payment(mock_append_entry):
    from src.pipeline import DuplicateRequestError

    engine = make_high_trust_engine()
    arc_client = MagicMock()
    existing = MagicMock()
    existing.request.request_id = "main-test-1"

    with patch("src.pipeline.read_latest_per_request", return_value=[existing]):
        with pytest.raises(DuplicateRequestError):
            run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    engine.score.assert_not_called()
    arc_client.send_payment.assert_not_called()
    mock_append_entry.assert_not_called()


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_concurrent_submissions_of_the_same_request_pay_once(mock_append_entry):
    """Simulates a double click: the real log is stood in for by a list that
    append_entry writes to, so the second thread sees the first one's entry
    only if the per-request lock really serializes check -> pay -> append.
    """
    import threading
    import time

    from src.pipeline import DuplicateRequestError

    log: list = []
    mock_append_entry.side_effect = log.append

    arc_client = MagicMock()

    def slow_payment(**_kwargs):
        time.sleep(0.2)  # wide window for the second thread to slip through
        return {"transaction_id": "tx-1", "tx_hash": "0x1"}

    arc_client.send_payment.side_effect = slow_payment

    results: list[str] = []

    def submit():
        try:
            run_pipeline(
                make_request("0xdeadbeef"),
                engine=make_high_trust_engine(),
                arc_client=arc_client,
                verbose=False,
            )
            results.append("ok")
        except DuplicateRequestError:
            results.append("duplicate")

    with patch("src.pipeline.read_latest_per_request", side_effect=lambda: list(log)):
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == ["duplicate", "ok"]
    assert arc_client.send_payment.call_count == 1


@patch("src.pipeline.append_entry")
def test_human_override_rejects_a_request_already_reviewed(mock_append_entry):
    from src.audit.logger import AuditEntry
    from src.pipeline import AlreadyReviewedError, apply_human_override

    scores = DecisionScores(
        trust=ScoreResult(value=0.1, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    base = dict(
        timestamp="2026-10-06T12:00:00+00:00",
        request=make_request("0xdeadbeef"),
        scores=scores,
        decision="escalate",
        reduced_amount=None,
        reason="r",
        payment_executed=False,
    )
    pending = AuditEntry(**base)
    already_reviewed = AuditEntry(**base, human_review="approved", reviewer="ana")

    # The caller holds a stale `pending` copy, but the log already shows a review.
    with patch("src.pipeline.read_latest_per_request", return_value=[already_reviewed]):
        with pytest.raises(AlreadyReviewedError):
            apply_human_override(pending, "approve", "joão", arc_client=MagicMock())

    mock_append_entry.assert_not_called()
