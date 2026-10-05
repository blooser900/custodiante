"""Tests for scripts/setup_arc_wallet.py.

The Circle SDK is fully mocked: no credentials or network needed. We check
the parts that can hurt the user if wrong -- .env handling (never overwrite a
secret, no duplicate keys) and skip-if-already-done behavior.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from scripts import setup_arc_wallet as setup


def test_append_env_fills_empty_placeholder(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CIRCLE_API_KEY=abc\nCIRCLE_ENTITY_SECRET=\nARC_WALLET_ID=\n")

    setup.append_env("CIRCLE_ENTITY_SECRET", "s3cret", env)

    text = env.read_text()
    assert text.count("CIRCLE_ENTITY_SECRET=") == 1  # no duplicate key
    assert "CIRCLE_ENTITY_SECRET=s3cret" in text
    assert "ARC_WALLET_ID=\n" in text  # other placeholders untouched


def test_append_env_appends_when_key_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CIRCLE_API_KEY=abc\n")

    setup.append_env("ARC_WALLET_ID", "w-1", env)

    assert "ARC_WALLET_ID=w-1" in env.read_text()


def test_append_env_refuses_to_overwrite_existing_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CIRCLE_ENTITY_SECRET=already-set\n")

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        setup.append_env("CIRCLE_ENTITY_SECRET", "new", env)

    assert "already-set" in env.read_text()


def test_ensure_entity_secret_skips_when_present(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CIRCLE_ENTITY_SECRET=existing\n")

    with (
        patch.dict(os.environ, {"CIRCLE_ENTITY_SECRET": "existing"}),
        patch.object(setup.utils, "register_entity_secret_ciphertext") as register,
    ):
        result = setup.ensure_entity_secret("key", env)

    assert result == "existing"
    register.assert_not_called()


def test_ensure_entity_secret_registers_and_saves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so ./recovery is created in the temp dir
    env = tmp_path / ".env"
    env.write_text("CIRCLE_API_KEY=key\nCIRCLE_ENTITY_SECRET=\n")

    with patch.object(setup.utils, "register_entity_secret_ciphertext") as register:
        secret = setup.ensure_entity_secret("key", env)

    register.assert_called_once()
    assert len(secret) == 64  # 32 bytes, hex encoded
    assert f"CIRCLE_ENTITY_SECRET={secret}" in env.read_text()


def test_ensure_wallet_skips_when_present(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ARC_WALLET_ID=w-1\nARC_WALLET_ADDRESS=0xabc\n")

    with patch.dict(os.environ, {"ARC_WALLET_ID": "w-1", "ARC_WALLET_ADDRESS": "0xabc"}):
        wallet_id, address = setup.ensure_wallet(MagicMock(), env)

    assert (wallet_id, address) == ("w-1", "0xabc")


def test_ensure_wallet_creates_and_saves(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ARC_WALLET_ID=\nARC_WALLET_ADDRESS=\n")

    set_response = MagicMock()
    set_response.data.to_dict.return_value = {"walletSet": {"id": "set-1"}}
    wallet_response = MagicMock()
    wallet_response.data.to_dict.return_value = {
        "wallets": [{"id": "w-9", "address": "0xfeed"}]
    }

    with (
        patch.object(setup.dcw, "WalletSetsApi") as sets_api,
        patch.object(setup.dcw, "WalletsApi") as wallets_api,
    ):
        sets_api.return_value.create_wallet_set.return_value = set_response
        wallets_api.return_value.create_wallet.return_value = wallet_response
        wallet_id, address = setup.ensure_wallet(MagicMock(), env)

    assert (wallet_id, address) == ("w-9", "0xfeed")
    text = env.read_text()
    assert "ARC_WALLET_ID=w-9" in text
    assert "ARC_WALLET_ADDRESS=0xfeed" in text


def test_get_usdc_balance_reads_usdc_entry():
    response = MagicMock()
    response.data.to_dict.return_value = {
        "tokenBalances": [{"amount": "20.0", "token": {"symbol": "USDC"}}]
    }
    with patch.object(setup.dcw, "WalletsApi") as wallets_api:
        wallets_api.return_value.list_wallet_balance.return_value = response
        assert setup.get_usdc_balance(MagicMock(), "w-1") == 20.0


@patch("scripts.setup_arc_wallet.time.sleep")
def test_wait_for_funds_times_out_without_balance(mock_sleep):
    with patch.object(setup, "get_usdc_balance", return_value=0.0):
        assert setup.wait_for_funds(MagicMock(), "w-1", timeout_seconds=20, interval_seconds=10) is False


@patch("scripts.setup_arc_wallet.time.sleep")
def test_wait_for_funds_returns_true_once_funded(mock_sleep):
    with patch.object(setup, "get_usdc_balance", side_effect=[0.0, 20.0]):
        assert setup.wait_for_funds(MagicMock(), "w-1", timeout_seconds=60, interval_seconds=10) is True
