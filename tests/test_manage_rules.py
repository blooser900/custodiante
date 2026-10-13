"""Tests for scripts/manage_rules.py.

load_config/save_config are patched inside scripts.manage_rules to always
operate against a tmp_path file, since their real default (src.config.
CONFIG_PATH) is bound at src/config.py import time and can't be redirected
by monkeypatching that attribute afterwards.
"""

import argparse
from functools import partial
from unittest.mock import patch

import pytest

from src.config import load_config as real_load_config
from src.config import save_config as real_save_config
from scripts import manage_rules


@pytest.fixture(autouse=True)
def redirect_config_io(tmp_path):
    config_path = tmp_path / "rules.yaml"
    with (
        patch.object(manage_rules, "load_config", partial(real_load_config, config_path)),
        patch.object(manage_rules, "save_config", partial(real_save_config, path=config_path)),
        patch.object(manage_rules, "CONFIG_PATH", config_path),
    ):
        yield config_path


def ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_show_prints_defaults_on_first_run(capsys):
    exit_code = manage_rules.cmd_show(ns())
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "operational_expense" in out
    assert "trust_min_auto_approve" in out
    assert "[0]" in out  # a rubric level is printed


def test_set_limit_persists(capsys):
    manage_rules.cmd_set_limit(ns(category="operational_expense", amount=777.0))
    config = manage_rules.load_config()
    assert config.category_limits["operational_expense"].max_usdc == 777.0


def test_set_limit_warns_on_unknown_category(capsys):
    manage_rules.cmd_set_limit(ns(category="travel_reimbursement", amount=50.0))
    err = capsys.readouterr().err
    assert "not an existing category" in err
    config = manage_rules.load_config()
    assert config.category_limits["travel_reimbursement"].max_usdc == 50.0


def test_set_threshold_persists_valid_value():
    exit_code = manage_rules.cmd_set_threshold(ns(name="trust_min_auto_approve", value=0.9))
    config = manage_rules.load_config()

    assert exit_code == 0
    assert config.thresholds.trust_min_auto_approve == 0.9


def test_set_threshold_rejects_out_of_range_value(capsys):
    exit_code = manage_rules.cmd_set_threshold(ns(name="trust_min_auto_approve", value=5.0))
    config = manage_rules.load_config()

    assert exit_code == 1
    assert "Invalid value" in capsys.readouterr().err
    # Unchanged -- the bad value must not have been persisted.
    assert config.thresholds.trust_min_auto_approve == 0.75


def test_set_criteria_level_persists():
    exit_code = manage_rules.cmd_set_criteria_level(
        ns(question="importance", index=2, text="Critical impact.")
    )
    config = manage_rules.load_config()

    assert exit_code == 0
    assert config.criteria.importance[2] == "Critical impact."


def test_set_criteria_level_rejects_out_of_range_index(capsys):
    exit_code = manage_rules.cmd_set_criteria_level(
        ns(question="importance", index=99, text="x")
    )
    assert exit_code == 1
    assert "out of range" in capsys.readouterr().err


def test_validate_reports_ok_on_default_config(capsys):
    exit_code = manage_rules.cmd_validate(ns())
    assert exit_code == 0
    assert "valid" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The payee check: on by default, switchable, and a way to register vendors
# --------------------------------------------------------------------------


def test_show_says_the_payee_check_is_on_and_what_an_empty_registry_means(capsys):
    manage_rules.cmd_show(ns())
    out = capsys.readouterr().out

    assert "Payee check:" in out and "ON" in out
    assert "every payee is unknown" in out
    assert "cannot fire" not in out, "the old text claimed an empty registry disables the check"


def test_payee_check_can_be_switched_off_and_on_and_is_loud_about_off(capsys):
    assert manage_rules.cmd_payee_check(ns(state="off")) == 0
    assert manage_rules.load_config().require_known_payee is False
    assert "OFF" in capsys.readouterr().err

    manage_rules.cmd_show(ns())
    assert "OFF" in capsys.readouterr().out

    assert manage_rules.cmd_payee_check(ns(state="on")) == 0
    assert manage_rules.load_config().require_known_payee is True


def test_register_vendor_normalizes_and_records_who_and_when():
    code = manage_rules.cmd_register_vendor(
        ns(key="  Acme LTDA ", wallets=["0xABCDEF"], note="invoice 1", by="ana")
    )
    registration = manage_rules.load_config().vendor_registry["acme ltda"]

    assert code == 0
    assert registration.wallets == ["0xabcdef"]
    assert registration.note == "invoice 1" and registration.registered_by == "ana"
    assert registration.registered_at is not None


def test_register_vendor_adds_wallets_it_never_replaces_them():
    manage_rules.cmd_register_vendor(ns(key="acme", wallets=["0xaaa"], note="first", by="ana"))
    manage_rules.cmd_register_vendor(ns(key="Acme", wallets=["0xbbb"], note=None, by=None))
    registration = manage_rules.load_config().vendor_registry["acme"]

    assert registration.wallets == ["0xaaa", "0xbbb"]
    assert registration.note == "first", "registering again must not erase the earlier note"
    assert registration.registered_by == "ana"


def test_register_vendor_rejects_a_blank_wallet(capsys):
    code = manage_rules.cmd_register_vendor(ns(key="acme", wallets=["   "], note=None, by=None))

    assert code == 1
    assert "acme" not in manage_rules.load_config().vendor_registry
    assert "Invalid registration" in capsys.readouterr().err
