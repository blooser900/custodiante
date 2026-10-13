"""The demo seeds the payees its scenarios pay, because the payee check is ON."""

from unittest.mock import patch

from demo import seed_scenarios
from src.config import load_config
from src.payees import PayeeRegistry, PayeeStatus


def test_the_demo_payees_are_registered_for_their_wallets():
    added = seed_scenarios.register_demo_payees()
    registry = PayeeRegistry.from_config()

    assert set(added) == {"contractor:known_vendor", "ops-team"}
    wallet = seed_scenarios.DEMO_PAYEES["ops-team"]
    assert registry.assess("Ops-Team", wallet).status is PayeeStatus.KNOWN
    assert registry.assess("contractor:known_vendor", wallet).status is PayeeStatus.KNOWN


def test_every_scenario_that_should_be_paid_has_a_registered_payee_and_wallet():
    """If a scenario's requester or wallet drifts from DEMO_PAYEES the demo
    silently turns an auto-approval into an escalation: catch it here."""
    seed_scenarios.register_demo_payees()
    registry = PayeeRegistry.from_config()
    for scenario in seed_scenarios.SCENARIOS:
        if scenario["request_id"] == "demo-escalate":
            continue
        status = registry.assess(scenario["requester"], scenario["counterparty_wallet"]).status
        assert status is PayeeStatus.KNOWN, scenario["request_id"]


def test_the_escalation_scenario_stays_unknown_on_purpose():
    seed_scenarios.register_demo_payees()
    scenario = next(s for s in seed_scenarios.SCENARIOS if s["request_id"] == "demo-escalate")
    status = PayeeRegistry.from_config().assess(scenario["requester"], scenario.get("counterparty_wallet")).status
    assert status is PayeeStatus.UNKNOWN


def test_registering_twice_adds_nothing_and_never_overwrites_a_managers_entry():
    seed_scenarios.register_demo_payees()
    config = load_config()
    config.vendor_registry["ops-team"] = config.vendor_registry["ops-team"].model_copy(
        update={"note": "edited by a manager"}
    )
    from src.config import save_config
    save_config(config)

    assert seed_scenarios.register_demo_payees() == []
    assert load_config().vendor_registry["ops-team"].note == "edited by a manager"
