"""Tests for src/config.py. Every test uses tmp_path for the config file --
none of them touch the real config/rules.yaml.
"""

import pytest
from pydantic import ValidationError

from src.config import (
    RulesConfig,
    ScoringCriteria,
    Thresholds,
    load_config,
    save_config,
)


def test_load_config_creates_file_with_defaults_on_first_run(tmp_path):
    config_path = tmp_path / "rules.yaml"
    assert not config_path.exists()

    config = load_config(config_path)

    assert config_path.exists()
    assert config.category_limits["operational_expense"] == 500.0
    assert len(config.criteria.trust) == 3


def test_load_config_reads_back_what_was_saved(tmp_path):
    config_path = tmp_path / "rules.yaml"
    original = RulesConfig(
        category_limits={"other": 999.0},
        thresholds=Thresholds(
            trust_min_auto_approve=0.6,
            trust_min_confidence_for_auto_approve=0.5,
            trust_min_to_consider=0.3,
            importance_high=0.8,
            urgency_high=0.8,
            reduced_amount_fraction=0.4,
        ),
        criteria=ScoringCriteria(trust=["x", "y"], urgency=["x", "y"], importance=["x", "y"]),
    )

    save_config(original, config_path)
    loaded = load_config(config_path)

    assert loaded.category_limits == {"other": 999.0}
    assert loaded.thresholds.trust_min_auto_approve == 0.6
    assert loaded.criteria.trust == ["x", "y"]


def test_threshold_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Thresholds(
            trust_min_auto_approve=1.5,  # out of [0, 1]
            trust_min_confidence_for_auto_approve=0.5,
            trust_min_to_consider=0.3,
            importance_high=0.8,
            urgency_high=0.8,
            reduced_amount_fraction=0.4,
        )


def test_criteria_rejects_too_few_levels():
    with pytest.raises(ValidationError, match="between 2 and 10 levels"):
        ScoringCriteria(trust=["only one level"], urgency=["a", "b"], importance=["a", "b"])


def test_criteria_rejects_too_many_levels():
    with pytest.raises(ValidationError, match="between 2 and 10 levels"):
        ScoringCriteria(
            trust=[f"level {i}" for i in range(11)],
            urgency=["a", "b"],
            importance=["a", "b"],
        )


def test_criteria_allows_boundary_level_counts():
    ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"])  # 2, ok
    ScoringCriteria(
        trust=[f"l{i}" for i in range(10)], urgency=["a", "b"], importance=["a", "b"]
    )  # 10, ok


def test_load_config_rejects_malformed_yaml_file(tmp_path):
    config_path = tmp_path / "rules.yaml"
    config_path.write_text("category_limits: not_a_dict\nthresholds: {}\ncriteria: {}\n")

    with pytest.raises(ValidationError):
        load_config(config_path)
