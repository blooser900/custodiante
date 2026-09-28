"""Tests for ArcPaymentClient.

These mock Circle's SDK entirely (no real credentials or network calls),
checking our own polling logic and error handling: that we poll until a
terminal state, that COMPLETE returns the expected dict, and that a
non-COMPLETE terminal state raises instead of silently succeeding.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.payments.arc_client import ArcPaymentClient


def make_client() -> ArcPaymentClient:
    with (
        patch("src.payments.arc_client.utils.init_developer_controlled_wallets_client"),
        patch("src.payments.arc_client.dcw.TransactionsApi"),
        patch("src.payments.arc_client.dcw.WalletsApi"),
    ):
        return ArcPaymentClient()


@patch("src.payments.arc_client.time.sleep")
def test_send_payment_completes_after_polling(mock_sleep):
    client = make_client()

    transfer_response = MagicMock()
    transfer_response.data.to_dict.return_value = {"id": "tx-123", "state": "INITIATED"}
    client.transactions_api.create_developer_transaction_transfer.return_value = transfer_response

    pending_poll = MagicMock()
    pending_poll.data.to_dict.return_value = {"transaction": {"state": "PENDING"}}
    complete_poll = MagicMock()
    complete_poll.data.to_dict.return_value = {
        "transaction": {"state": "COMPLETE", "tx_hash": "0xabc"}
    }
    client.transactions_api.get_transaction.side_effect = [pending_poll, complete_poll]

    result = client.send_payment(
        destination_wallet="0xdead", amount_usdc=42.0, memo="test payment"
    )

    assert result["transaction_id"] == "tx-123"
    assert result["state"] == "COMPLETE"
    assert result["tx_hash"] == "0xabc"
    assert mock_sleep.call_count == 2  # one per poll iteration


@patch("src.payments.arc_client.time.sleep")
def test_send_payment_raises_on_denied(mock_sleep):
    client = make_client()

    transfer_response = MagicMock()
    transfer_response.data.to_dict.return_value = {"id": "tx-456", "state": "INITIATED"}
    client.transactions_api.create_developer_transaction_transfer.return_value = transfer_response

    denied_poll = MagicMock()
    denied_poll.data.to_dict.return_value = {"transaction": {"state": "DENIED"}}
    client.transactions_api.get_transaction.return_value = denied_poll

    with pytest.raises(RuntimeError, match="DENIED"):
        client.send_payment(destination_wallet="0xdead", amount_usdc=10.0, memo="will fail")


def test_get_balance_finds_usdc_entry():
    client = make_client()

    balance_response = MagicMock()
    balance_response.data.to_dict.return_value = {
        "token_balances": [
            {"amount": "123.45", "token": {"symbol": "USDC"}},
            {"amount": "1.0", "token": {"symbol": "ETH"}},
        ]
    }
    client.wallets_api.list_wallet_balance.return_value = balance_response

    assert client.get_balance() == 123.45


def test_get_balance_returns_zero_when_no_usdc():
    client = make_client()

    balance_response = MagicMock()
    balance_response.data.to_dict.return_value = {"token_balances": []}
    client.wallets_api.list_wallet_balance.return_value = balance_response

    assert client.get_balance() == 0.0
