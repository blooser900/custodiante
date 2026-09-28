"""Tests that main.process_request wires the decision engine, policy layer,
and Arc payment execution together correctly -- with everything external
mocked (no real Jev/Kev server, no real Circle credentials needed).
"""

import os
from unittest.mock import MagicMock, patch

from src.decision_engine.engine import DecisionScores, ScoreResult
from src.main import process_request
from src.requests.schema import PaymentRequest, RequestCategory


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


@patch("src.main.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_with_wallet_executes_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    process_request(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_called_once()
    _, kwargs = arc_client.send_payment.call_args
    assert kwargs["destination_wallet"] == "0xdeadbeef"
    assert kwargs["amount_usdc"] == 100.0

    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is True


@patch("src.main.append_entry")
@patch.dict(os.environ, {}, clear=True)
def test_approved_request_without_credentials_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    process_request(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False


@patch("src.main.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_without_wallet_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    process_request(make_request(None), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False


@patch("src.main.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_failed_transfer_is_logged_not_raised(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    # Should not raise -- a failed on-chain transfer is recorded, not fatal.
    process_request(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    logged_entry = mock_append_entry.call_args[0][0]
    assert logged_entry.payment_executed is False
