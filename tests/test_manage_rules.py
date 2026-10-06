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
    assert config.category_limits["operational_expense"] == 777.0


def test_set_limit_warns_on_unknown_category(capsys):
    manage_rules.cmd_set_limit(ns(category="travel_reimbursement", amount=50.0))
    err = capsys.readouterr().err
    assert "not an existing category" in err
    config = manage_rules.load_config()
    assert config.category_limits["travel_reimbursement"] == 50.0


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
