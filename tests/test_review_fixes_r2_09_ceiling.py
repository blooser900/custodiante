"""A category limit may not exceed the money available.

A category limit only had a floor (`max_usdc > 0`). Nothing refused a ceiling
larger than the treasury holds -- reachable through `PUT /api/config`, through
the dashboard, and through `manage_rules.py`'s `set-limit`. "Auto-approve under
the limit" stops being a control when the limit is above the funds.

Ceiling = the wallet's USDC balance (`get_balance()`, a method that had been
written and never called), with a floor of 100,000 USDC so a well-funded wallet
does not turn a policy decision into a mirror of its balance: the ceiling is the
wallet's total balance, or `10**5` when `get_balance()` returns more than that.

What is NOT asserted here: that an unreadable balance blocks a save. It must
not. Reading a balance needs credentials and the network, and a rules screen that
refuses to save because Circle is unreachable is uneditable exactly when someone
needs it. Fails open, by design.
"""

import pytest

from src.config import RulesConfig, ScoringCriteria
from src.policy.ceiling import (
    MAX_LIMIT_USDC,
    MIN_FUNDED_USDC,
    CeilingCheck,
    LimitAboveCeilingError,
    ceiling_for,
    check_limits_against_balance,
    require_limits_within_balance,
)
from tests.rules_fixtures import category_limits, thresholds


def rules(**overrides) -> RulesConfig:
    limits = overrides.pop("limits", None)
    return RulesConfig(
        category_limits=limits or category_limits(),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
    )


def balance_of(value):
    return lambda: value


# --------------------------------------------------------------------------
# The ceiling itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "balance, expected",
    [
        (0.0, MIN_FUNDED_USDC),  # a wallet that has not been funded yet
        (10.0, MIN_FUNDED_USDC),
        (MIN_FUNDED_USDC - 1, MIN_FUNDED_USDC),
        (MIN_FUNDED_USDC, MIN_FUNDED_USDC),
        (5_000.0, 5_000.0),  # the middle follows the balance
        (50_000.0, 50_000.0),
        (MAX_LIMIT_USDC, MAX_LIMIT_USDC),
        (MAX_LIMIT_USDC + 1, MAX_LIMIT_USDC),  # the top is pinned
        (9_000_000.0, MAX_LIMIT_USDC),
    ],
)
def test_the_ceiling_follows_the_balance_between_two_pinned_ends(balance, expected):
    assert ceiling_for(balance) == expected


def test_an_unreadable_balance_does_not_block_anything():
    """Fail-open: the check is a sanity guard, not a security boundary."""

    def exploding_balance():
        raise ConnectionError("Circle is unreachable")

    result = check_limits_against_balance(rules(), exploding_balance)

    assert isinstance(result, CeilingCheck)
    assert result.checked is False
    assert result.ok is True, "an unreadable balance must not be reported as a violation"
    assert result.ceiling_usdc is None
    assert "could not read the wallet balance" in result.detail.lower()


def test_a_limit_above_a_small_balance_is_flagged():
    """The defect: a ceiling larger than the treasury holds used to be accepted.
    5,000 USDC of milestone authority against a 1,000 USDC balance is authority
    the wallet cannot honour -- the other categories are inside it, so only the
    offending one is named."""
    config = rules(
        limits=category_limits(contractor_milestone=5_000.0, supply_purchase=800.0, other=100.0)
    )

    result = check_limits_against_balance(config, balance_of(1_000.0))

    assert result.checked is True
    assert result.ok is False
    assert result.over == ("contractor_milestone",)
    assert "5,000" in result.detail and "1,000" in result.detail
    # Named, but the two categories inside the ceiling are not.
    assert "supply_purchase" not in result.detail


def test_a_funded_treasury_keeps_the_limit_a_policy_decision():
    """At nine million USDC the ceiling stops following the balance, so the same
    50,000 limit passes with plenty of room to spare."""
    config = rules(limits=category_limits(contractor_milestone=50_000.0, other=100.0))

    result = check_limits_against_balance(config, balance_of(9_000_000.0))

    assert result.ceiling_usdc == MAX_LIMIT_USDC
    assert result.ok is True


def test_an_unfunded_wallet_can_still_configure_rules():
    """A fresh deployment has a zero balance and must still be able to write its
    rules before the first deposit -- that is what the floor is for."""
    config = rules(limits=category_limits(contractor_milestone=900.0, other=100.0))

    result = check_limits_against_balance(config, balance_of(0.0))

    assert result.ok is True
    assert result.ceiling_usdc == MIN_FUNDED_USDC


def test_the_shipped_defaults_are_refused_by_a_barely_funded_wallet():
    """Not a bug: it is exactly what the guard exists to catch, and the honest
    cost of the rule -- fund the wallet, or lower the limit.

    With 500 USDC in the wallet the ceiling is the 1,000 not-yet-funded floor,
    and only `contractor_milestone` (2,000) is above it. `supply_purchase`
    (1,000) sits exactly on the ceiling and `operational_expense` (500) is
    inside it, so neither is flagged."""
    result = check_limits_against_balance(rules(), balance_of(500.0))

    assert result.ceiling_usdc == MIN_FUNDED_USDC
    assert result.ok is False
    assert result.over == ("contractor_milestone",)


def test_the_shipped_defaults_pass_once_the_wallet_is_funded():
    """2,000 USDC of milestone authority needs at least 2,000 USDC behind it."""
    result = check_limits_against_balance(rules(), balance_of(2_000.0))

    assert result.ok is True
    assert result.ceiling_usdc == 2_000.0


def test_every_offending_category_is_named_not_just_the_first():
    """An operator fixing one at a time should not discover the next on the
    following attempt."""
    config = rules(
        limits=category_limits(contractor_milestone=900_000.0, supply_purchase=800_000.0, other=100.0)
    )

    result = check_limits_against_balance(config, balance_of(1_000.0))

    assert result.over == ("contractor_milestone", "supply_purchase")


def test_the_strict_variant_raises_with_the_explanation():
    with pytest.raises(LimitAboveCeilingError, match="contractor_milestone"):
        require_limits_within_balance(
            rules(limits=category_limits(contractor_milestone=900_000.0)), balance_of(10.0)
        )


def test_the_strict_variant_passes_a_config_within_the_ceiling():
    """1,000 USDC of operational-expense authority against a 5,000 USDC wallet."""
    result = require_limits_within_balance(
        rules(limits=category_limits(contractor_milestone=900.0, other=100.0)),
        balance_of(5_000.0),
    )

    assert result.ok is True


# --------------------------------------------------------------------------
# The CLI that also persists rules
# --------------------------------------------------------------------------


def test_set_limit_refuses_a_ceiling_above_the_funds_available(monkeypatch, capsys):
    """`manage_rules.py set-limit` is the other way rules get persisted, so the
    same ceiling applies there. It used to accept any positive number."""
    from scripts import manage_rules

    config = rules(limits=category_limits(contractor_milestone=900.0, other=100.0))
    persisted: list = []
    monkeypatch.setattr(manage_rules, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(manage_rules, "save_config", lambda c, *a, **k: persisted.append(c))
    monkeypatch.setattr(
        "src.payments.arc_client.ArcPaymentClient",
        type("Fake", (), {"get_balance": lambda self: 1_000.0}),
    )

    args = type("Args", (), {"category": "contractor_milestone", "amount": 90_000.0})()
    code = manage_rules.cmd_set_limit(args)

    assert code == 1, "an over-ceiling limit must be refused"
    assert "contractor_milestone" in capsys.readouterr().err
    assert persisted == [], "a refused limit must not be written"


def test_set_limit_accepts_a_ceiling_the_wallet_can_back(monkeypatch, capsys):
    from scripts import manage_rules

    config = rules(limits=category_limits(contractor_milestone=900.0, other=100.0))
    persisted: list = []
    monkeypatch.setattr(manage_rules, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(manage_rules, "save_config", lambda c, *a, **k: persisted.append(c))
    monkeypatch.setattr(
        "src.payments.arc_client.ArcPaymentClient",
        type("Fake", (), {"get_balance": lambda self: 5_000.0}),
    )

    args = type("Args", (), {"category": "contractor_milestone", "amount": 4_000.0})()
    code = manage_rules.cmd_set_limit(args)

    assert code == 0
    assert len(persisted) == 1
    assert config.category_limits["contractor_milestone"].max_usdc == 4_000.0


def test_set_limit_warns_but_proceeds_when_the_balance_cannot_be_read(monkeypatch, capsys):
    """Fail-open, same as the API: an unreachable Circle must not make the rules
    uneditable, but the operator is told the ceiling was not checked."""
    from scripts import manage_rules

    config = rules()
    persisted: list = []
    monkeypatch.setattr(manage_rules, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(manage_rules, "save_config", lambda c, *a, **k: persisted.append(c))

    def unreachable(self):
        raise ConnectionError("Circle is unreachable")

    monkeypatch.setattr(
        "src.payments.arc_client.ArcPaymentClient",
        type("Fake", (), {"get_balance": unreachable}),
    )

    args = type("Args", (), {"category": "other", "amount": 150.0})()
    code = manage_rules.cmd_set_limit(args)

    assert code == 0, "an unreadable balance must not block the edit"
    assert len(persisted) == 1
    assert "not checked against the funds" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The fail-open behaviour is a documented limitation, not a surprise
# --------------------------------------------------------------------------


def test_the_docs_state_that_the_check_fails_open():
    """The author accepted failing open ONLY on the condition that it is written
    down and explicit. This test is what keeps that true: if the disclosure is
    removed from the docs an operator reads, this fails."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    readme_lower = readme.lower()
    assert "fails open" in readme_lower, "the README no longer discloses the fail-open behaviour"
    # The consequence, not just the fact: with no balance, any limit passes.
    assert "any" in readme_lower and "limit is accepted" in readme_lower

    module_doc = Path("src/policy/ceiling.py").read_text(encoding="utf-8").lower()
    assert "fails open" in module_doc
    assert "any limit is accepted" in module_doc


def test_show_prints_whether_the_ceiling_was_actually_checked(monkeypatch, capsys):
    """`show` is where an operator reads the rules, so the state of this control
    belongs there -- not left to be discovered."""
    from scripts import manage_rules

    monkeypatch.setattr(manage_rules, "load_config", lambda *a, **k: rules(limits=category_limits(contractor_milestone=900.0, other=100.0)))

    monkeypatch.setattr(
        "src.payments.arc_client.ArcPaymentClient",
        type("Fake", (), {"get_balance": lambda self: 5_000.0}),
    )
    manage_rules.cmd_show(None)
    out = capsys.readouterr().out
    assert "Limit ceiling" in out and "OK" in out

    def unreachable(self):
        raise ConnectionError("Circle is unreachable")

    monkeypatch.setattr(
        "src.payments.arc_client.ArcPaymentClient",
        type("Fake", (), {"get_balance": unreachable}),
    )
    manage_rules.cmd_show(None)
    out = capsys.readouterr().out
    assert "NOT CHECKED" in out, "an operator must be able to see the check is not active"
    assert "will be accepted" in out, "the consequence of fail-open must be on screen"
