"""Shared rule-config builders for tests.

`RulesConfig` validates its own coherence (see src/config.py): every request
category needs a ceiling, and the trust bars must not be inverted. A test that
hand-builds a partial config therefore fails validation for reasons unrelated
to what it is testing -- and did, in eight places, when those validators were
added.

These helpers keep the *rules* realistic and let each test state only the
values it actually cares about.
"""

from src.config import (
    CATEGORIES_PAID_WHOLE,
    DEFAULT_CRITERIA,
    DEFAULT_THRESHOLDS,
    CategoryLimit,
    RulesConfig,
    ScoringCriteria,
    Thresholds,
)

# The ceilings the tests were written against, kept here so a change to the
# shipped defaults does not silently rewrite what a test is asserting.
_TEST_LIMITS = {
    "contractor_milestone": 2000.0,
    "supply_purchase": 1000.0,
    "operational_expense": 500.0,
    "other": 200.0,
}


def category_limits(**overrides: float) -> dict[str, CategoryLimit]:
    """A complete, valid set of ceilings. Override just the ones under test.

    `allow_reduced` follows the same rule as the shipped defaults: a category
    whose payment cannot meaningfully be split does not allow a part payment.
    """
    amounts = dict(_TEST_LIMITS)
    amounts.update(overrides)
    return {
        name: CategoryLimit(max_usdc=amount, allow_reduced=name not in CATEGORIES_PAID_WHOLE)
        for name, amount in amounts.items()
    }


def thresholds(**overrides: float) -> Thresholds:
    """The shipped defaults, with any of them replaced."""
    values = dict(DEFAULT_THRESHOLDS)
    values.update(overrides)
    return Thresholds(**values)


def rules_config(
    limits: dict[str, CategoryLimit] | None = None,
    criteria: ScoringCriteria | None = None,
    **threshold_overrides: float,
) -> RulesConfig:
    """A valid RulesConfig: defaults everywhere except what you pass."""
    return RulesConfig(
        category_limits=limits if limits is not None else category_limits(),
        thresholds=thresholds(**threshold_overrides),
        criteria=criteria if criteria is not None else DEFAULT_CRITERIA,
    )
