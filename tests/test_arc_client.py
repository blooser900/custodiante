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


# --------------------------------------------------------------------------
# Idempotency and the "unconfirmed" outcome
# --------------------------------------------------------------------------

from src.payments.arc_client import (  # noqa: E402
    POLL_TIMEOUT_SECONDS,
    PaymentUnconfirmedError,
    payment_idempotency_key,
)


def _created(client, state="INITIATED", transaction_id="tx-1"):
    response = MagicMock()
    response.data.to_dict.return_value = {"id": transaction_id, "state": state}
    client.transactions_api.create_developer_transaction_transfer.return_value = response


def _poll(state, tx_hash=None):
    poll = MagicMock()
    poll.data.to_dict.return_value = {"transaction": {"state": state, "txHash": tx_hash}}
    return poll


def test_idempotency_key_is_a_stable_uuid_per_request_and_attempt():
    import uuid

    key = payment_idempotency_key("req-1")
    assert key == payment_idempotency_key("req-1"), "same request, same key"
    assert key != payment_idempotency_key("req-2")
    assert key != payment_idempotency_key("req-1", attempt=2), "a deliberate retry gets a new key"
    assert str(uuid.UUID(key)) == key, "Circle requires a UUID"
    assert uuid.UUID(key).version == 4, "Circle documents the key as a v4 UUID"


@patch("src.payments.arc_client.time.sleep")
def test_send_payment_passes_the_key_and_ref_id_to_circle(_sleep):
    client = make_client()
    _created(client)
    client.transactions_api.get_transaction.return_value = _poll("COMPLETE", "0xabc")

    client.send_payment("0x" + "d" * 40, 5.0, "m", idempotency_key="k-1", ref_id="req-1")

    (sent,), _ = client.transactions_api.create_developer_transaction_transfer.call_args
    assert sent.idempotency_key == "k-1"
    assert sent.ref_id == "req-1"


@patch("src.payments.arc_client.time.sleep")
def test_a_poll_timeout_is_unconfirmed_and_carries_the_transaction_id(_sleep):
    client = make_client()
    _created(client, transaction_id="tx-slow")
    client.transactions_api.get_transaction.return_value = _poll("SENT")

    with pytest.raises(PaymentUnconfirmedError) as excinfo:
        client.send_payment("0x" + "d" * 40, 5.0, "m")

    assert excinfo.value.transaction_id == "tx-slow"
    assert excinfo.value.state == "SENT"
    assert str(POLL_TIMEOUT_SECONDS) in str(excinfo.value)


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "DENIED"])
@patch("src.payments.arc_client.time.sleep")
def test_a_terminal_failure_is_a_plain_failure_not_unconfirmed(_sleep, state):
    client = make_client()
    _created(client)
    client.transactions_api.get_transaction.return_value = _poll(state)

    with pytest.raises(RuntimeError) as excinfo:
        client.send_payment("0x" + "d" * 40, 5.0, "m")

    assert not isinstance(excinfo.value, PaymentUnconfirmedError)


@patch("src.payments.arc_client.time.sleep")
def test_a_poll_call_that_blows_up_is_unconfirmed_not_failed(_sleep):
    client = make_client()
    _created(client, transaction_id="tx-9")
    client.transactions_api.get_transaction.side_effect = ConnectionError("network cut")

    with pytest.raises(PaymentUnconfirmedError) as excinfo:
        client.send_payment("0x" + "d" * 40, 5.0, "m")
    assert excinfo.value.transaction_id == "tx-9"


def test_a_lost_answer_on_create_is_unconfirmed_without_an_id():
    client = make_client()
    client.transactions_api.create_developer_transaction_transfer.side_effect = TimeoutError("read timed out")

    with pytest.raises(PaymentUnconfirmedError) as excinfo:
        client.send_payment("0x" + "d" * 40, 5.0, "m")
    assert excinfo.value.transaction_id is None


def test_an_api_refusal_on_create_is_a_definite_failure():
    from src.payments.arc_client import dcw

    client = make_client()
    client.transactions_api.create_developer_transaction_transfer.side_effect = dcw.ApiException(status=400, reason="bad")

    with pytest.raises(RuntimeError) as excinfo:
        client.send_payment("0x" + "d" * 40, 5.0, "m")
    assert not isinstance(excinfo.value, PaymentUnconfirmedError)


@patch("src.payments.arc_client.time.sleep")
def test_a_transfer_already_complete_on_create_still_reports_its_tx_hash(_sleep):
    client = make_client()
    _created(client, state="COMPLETE", transaction_id="tx-fast")
    client.transactions_api.get_transaction.return_value = _poll("COMPLETE", "0xfeed")

    result = client.send_payment("0x" + "d" * 40, 5.0, "m")
    assert result["tx_hash"] == "0xfeed"


# --------------------------------------------------------------------------
# find_transfer: read-only lookup used by reconciliation
# --------------------------------------------------------------------------


def _tx(id="tx-1", state="COMPLETE", ref="req-1", tx_hash="0xh", reason=None):
    return {"id": id, "state": state, "refId": ref, "txHash": tx_hash, "errorReason": reason}


def _listing(client, transactions):
    response = MagicMock()
    response.data.to_dict.return_value = {"transactions": transactions}
    client.transactions_api.list_transactions.return_value = response


def test_find_transfer_by_id_is_an_exact_lookup_and_maps_the_camel_case_fields():
    client = make_client()
    response = MagicMock()
    response.data.to_dict.return_value = {"transaction": _tx(state="FAILED", reason="INSUFFICIENT_TOKEN", tx_hash=None)}
    client.transactions_api.get_transaction.return_value = response

    found = client.find_transfer("req-1", transaction_id="tx-1")

    client.transactions_api.get_transaction.assert_called_once_with(id="tx-1")
    client.transactions_api.list_transactions.assert_not_called()
    assert found == {"transaction_id": "tx-1", "state": "FAILED", "tx_hash": None, "error_reason": "INSUFFICIENT_TOKEN"}


def test_find_transfer_without_an_id_searches_our_wallet_and_matches_the_ref_id():
    client = make_client()
    _listing(client, [_tx("tx-a", ref="someone-else"), _tx("tx-b", ref="req-1", tx_hash="0xb")])

    found = client.find_transfer("req-1", destination_wallet="0x" + "d" * 40, since="2026-10-13T10:00:00+00:00")

    kwargs = client.transactions_api.list_transactions.call_args.kwargs
    assert kwargs["wallet_ids"] == client.wallet_id
    assert kwargs["destination_address"] == "0x" + "d" * 40
    assert kwargs["var_from"].isoformat() == "2026-10-13T09:50:00+00:00", "10 minutes of margin before the attempt"
    assert found["transaction_id"] == "tx-b" and found["tx_hash"] == "0xb"


def test_find_transfer_returns_none_when_nothing_matches():
    client = make_client()
    _listing(client, [_tx("tx-a", ref="someone-else")])
    assert client.find_transfer("req-1") is None


def test_find_transfer_refuses_to_conclude_from_a_full_page_without_a_match():
    from src.payments.arc_client import _SEARCH_PAGE_SIZE

    client = make_client()
    _listing(client, [_tx(f"tx-{i}", ref=f"other-{i}") for i in range(_SEARCH_PAGE_SIZE)])

    with pytest.raises(RuntimeError, match="next page"):
        client.find_transfer("req-1")


def test_find_transfer_refuses_an_ambiguous_match():
    client = make_client()
    _listing(client, [_tx("tx-a"), _tx("tx-b")])

    with pytest.raises(RuntimeError, match="share ref id"):
        client.find_transfer("req-1")


def test_find_transfer_turns_an_api_error_into_a_runtime_error():
    from src.payments.arc_client import dcw

    client = make_client()
    client.transactions_api.list_transactions.side_effect = dcw.ApiException(status=500, reason="boom")

    with pytest.raises(RuntimeError, match="Could not query Circle"):
        client.find_transfer("req-1")


def test_find_transfer_never_creates_anything():
    client = make_client()
    _listing(client, [_tx()])
    client.find_transfer("req-1")
    client.transactions_api.create_developer_transaction_transfer.assert_not_called()


# --------------------------------------------------------------------------
# The amount on the wire is rendered from a Decimal
# --------------------------------------------------------------------------


def _sent_amounts(client):
    (sent,), _ = client.transactions_api.create_developer_transaction_transfer.call_args
    return sent.amounts


@pytest.mark.parametrize(("amount", "text"), [
    (100.0, "100"),
    (0.000001, "0.000001"),   # str(float) would send '1e-06'
    (116.6655, "116.6655"),
    (1234.5, "1234.5"),
    (5, "5"),
])
@patch("src.payments.arc_client.time.sleep")
def test_the_amount_sent_to_circle_is_plain_decimal_text(_sleep, amount, text):
    client = make_client()
    _created(client, state="COMPLETE")
    client.transactions_api.get_transaction.return_value = _poll("COMPLETE", "0xabc")

    client.send_payment("0x" + "d" * 40, amount, "m")

    assert _sent_amounts(client) == [text]


@pytest.mark.parametrize("bad", [0.1 + 0.2, 1.0000001, 0, -5, float("nan"), float("inf")])
def test_an_amount_usdc_cannot_represent_is_refused_before_any_api_call(bad):
    client = make_client()

    with pytest.raises(ValueError):
        client.send_payment("0x" + "d" * 40, bad, "m")

    client.transactions_api.create_developer_transaction_transfer.assert_not_called()
