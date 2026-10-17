"""Tests for src/config.py. Every test uses tmp_path for the config file --
none of them touch the real config/rules.yaml.
"""

import pytest
from pydantic import ValidationError

from src.config import (
    QuestionSpec,
    RulesConfig,
    ScoringCriteria,
    Thresholds,
    _default_config,
    load_config,
    save_config,
)
from src.requests.schema import RequestCategory
from tests.rules_fixtures import category_limits, rules_config, thresholds


def test_load_config_creates_file_with_defaults_on_first_run(tmp_path):
    config_path = tmp_path / "rules.yaml"
    assert not config_path.exists()

    config = load_config(config_path)

    assert config_path.exists()
    assert config.category_limits["operational_expense"].max_usdc == 500.0
    assert len(config.criteria.trust) == 3


def test_load_config_reads_back_what_was_saved(tmp_path):
    config_path = tmp_path / "rules.yaml"
    limits = category_limits(other=999.0)
    original = RulesConfig(
        category_limits=limits,
        thresholds=thresholds(
            trust_min_auto_approve=0.6,
            trust_min_to_consider=0.3,
            importance_high=0.8,
            urgency_high=0.8,
            reduced_amount_fraction=0.4,
        ),
        criteria=ScoringCriteria(trust=["x", "y"], urgency=["x", "y"], importance=["x", "y"]),
    )

    save_config(original, config_path)
    loaded = load_config(config_path)

    assert loaded.category_limits == limits
    assert loaded.thresholds.trust_min_auto_approve == 0.6
    assert loaded.criteria.trust == ["x", "y"]


def test_threshold_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Thresholds(
            trust_min_auto_approve=1.5,  # out of [0, 1]
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


# --------------------------------------------------------------------------
# The hard limits cannot be silently disabled
# --------------------------------------------------------------------------


def test_an_empty_category_limits_is_refused():
    """`{}` used to load and drop every ceiling to the 200 USDC fallback.

    `policy/limits.py` does `limits.get(category, limits.get("other", 200.0))`,
    so an empty mapping silently turns a 2000 USDC milestone ceiling into 200.
    `PUT /api/config` accepted `{"category_limits": {}}` and persisted it.
    """
    with pytest.raises(ValidationError, match="cannot be empty"):
        rules_config(limits={})


def test_a_missing_category_ceiling_is_refused():
    """A partial file is the other way to lose a limit by accident."""
    partial = category_limits()
    del partial["contractor_milestone"]

    with pytest.raises(ValidationError, match="missing a ceiling for contractor_milestone"):
        rules_config(limits=partial)


@pytest.mark.parametrize("bad_limit", [0.0, -1.0, -5000.0])
def test_a_non_positive_ceiling_is_refused(bad_limit):
    """A non-positive ceiling escalates every request in that category, which
    looks like a policy decision but is really a typo."""
    with pytest.raises(ValidationError, match="must be positive"):
        rules_config(limits=category_limits(operational_expense=bad_limit))


def test_the_shipped_defaults_satisfy_the_validators():
    """The validators must not reject the project's own defaults."""
    config = rules_config()
    assert set(config.category_limits) == {c.value for c in RequestCategory}
    assert config.thresholds.fraud_veto_at == 0.7


# --------------------------------------------------------------------------
# The trust bars must not contradict each other
# --------------------------------------------------------------------------


def test_an_inverted_trust_bar_is_refused():
    """`consider > auto_approve` used to load, leaving a policy where a low
    trust escalates while a high trust auto-approves -- a self-contradiction
    an operator could save from the dashboard with no warning."""
    with pytest.raises(ValidationError, match="trust_min_to_consider must be <="):
        rules_config(trust_min_to_consider=0.9, trust_min_auto_approve=0.5)


def test_equal_trust_bars_are_allowed():
    """The boundary is inclusive: equal bars are coherent (approve above the
    line, escalate below), just very strict."""
    config = rules_config(trust_min_to_consider=0.7, trust_min_auto_approve=0.7)
    assert config.thresholds.trust_min_to_consider == 0.7


def test_importance_and_urgency_bars_may_exceed_the_consider_bar():
    """Deliberately NOT an invariant.

    The shipped defaults have importance_high=0.7 above trust_min_to_consider
    =0.4, and that is coherent: the consider bar is a hard stop applied first,
    so a request reaching the reduced-approval branch already cleared it.
    """
    config = rules_config(importance_high=0.9, urgency_high=0.9, trust_min_to_consider=0.4)
    assert config.thresholds.importance_high == 0.9


def test_the_fraud_veto_question_must_stay_a_noul():
    """Retyping it would silently disable a safety control."""
    misconfigured = {
        "fraud_indicators": QuestionSpec(
            type="score", instructions="How risky is this?", criteria=["a", "b"]
        )
    }
    with pytest.raises(ValidationError, match="fraud_indicators"):
        RulesConfig(
            category_limits=category_limits(),
            thresholds=thresholds(),
            criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
            assessment_questions=misconfigured,
        )


# --------------------------------------------------------------------------
# require_known_payee: on unless explicitly switched off
# --------------------------------------------------------------------------


def test_a_config_file_written_before_the_payee_check_existed_stays_protected(tmp_path):
    """An older rules.yaml has no `require_known_payee` key. Missing must mean ON,
    never off: an upgrade must not quietly disable a safety check."""
    import yaml

    path = tmp_path / "rules.yaml"
    older = _default_config().model_dump()
    del older["require_known_payee"]
    path.write_text(yaml.safe_dump(older), encoding="utf-8")

    assert load_config(path).require_known_payee is True


def test_the_payee_check_switch_survives_a_save_and_load(tmp_path):
    path = tmp_path / "rules.yaml"
    config = _default_config()
    config.require_known_payee = False
    save_config(config, path)

    assert load_config(path).require_known_payee is False
    assert "require_known_payee" in config.model_dump(), "the dashboard round-trips the whole config"
