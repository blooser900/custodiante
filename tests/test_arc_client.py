"""Tests for ArcPaymentClient.

These mock Circle's SDK entirely (no real credentials or network calls),
checking our own polling logic and error handling: that we poll until a
terminal state, that COMPLETE returns the expected dict, and that a
non-COMPLETE terminal state raises instead of silently succeeding.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.payments.arc_client import ArcPaymentClient, _validate_destination


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
        "transaction": {"state": "COMPLETE", "txHash": "0xabc"}
    }
    client.transactions_api.get_transaction.side_effect = [pending_poll, complete_poll]

    result = client.send_payment(
        destination_wallet="0x" + "d" * 40, amount_usdc=42.0, memo="test payment"
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
        client.send_payment(destination_wallet="0x" + "d" * 40, amount_usdc=10.0, memo="will fail")


def test_get_balance_finds_usdc_entry():
    client = make_client()

    balance_response = MagicMock()
    balance_response.data.to_dict.return_value = {
        "tokenBalances": [
            {"amount": "123.45", "token": {"symbol": "USDC"}},
            {"amount": "1.0", "token": {"symbol": "ETH"}},
        ]
    }
    client.wallets_api.list_wallet_balance.return_value = balance_response

    assert client.get_balance() == 123.45


def test_validate_destination_rejects_malformed_address():
    with pytest.raises(ValueError, match="not a well-formed EVM address"):
        _validate_destination("not-an-address", source_wallet="0x" + "1" * 40)


def test_validate_destination_rejects_wrong_length():
    with pytest.raises(ValueError, match="not a well-formed EVM address"):
        _validate_destination("0x1234", source_wallet="0x" + "1" * 40)


def test_validate_destination_rejects_self_transfer():
    address = "0x" + "a" * 40
    with pytest.raises(ValueError, match="same as the source wallet"):
        _validate_destination(address, source_wallet=address)


def test_validate_destination_self_transfer_check_is_case_insensitive():
    with pytest.raises(ValueError, match="same as the source wallet"):
        _validate_destination("0x" + "A" * 40, source_wallet="0x" + "a" * 40)


def test_validate_destination_allows_well_formed_different_address():
    _validate_destination("0x" + "b" * 40, source_wallet="0x" + "a" * 40)  # no raise


@patch("src.payments.arc_client.time.sleep")
def test_send_payment_rejects_self_transfer_before_calling_api(mock_sleep):
    client = make_client()
    client.wallet_address = "0x" + "c" * 40

    with pytest.raises(ValueError, match="same as the source wallet"):
        client.send_payment(
            destination_wallet=client.wallet_address, amount_usdc=5.0, memo="self-pay test"
        )

    client.transactions_api.create_developer_transaction_transfer.assert_not_called()


def test_get_balance_returns_zero_when_no_usdc():
    client = make_client()

    balance_response = MagicMock()
    balance_response.data.to_dict.return_value = {"tokenBalances": []}
    client.wallets_api.list_wallet_balance.return_value = balance_response

    assert client.get_balance() == 0.0
