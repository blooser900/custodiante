"""Generates a handful of realistic payment requests for the demo video.

Keeping these separate from tests/fixtures: fixtures are for automated
tests, these are curated specifically to look good and tell a clear story
on camera (one auto-approve, one reduced approval, one escalation).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import VendorRegistration, load_config, save_config

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

SCENARIOS = [
    {
        "request_id": "demo-approve",
        "category": "contractor_milestone",
        "amount_usdc": 500.0,
        "requester": "contractor:known_vendor",
        "description": "Milestone 1 of 2, verified complete, vendor has a clean payment history.",
        "due_date": "2026-10-03T00:00:00Z",
        "counterparty_wallet": "0x0000000000000000000000000000000000dEaD",
    },
    {
        "request_id": "demo-reduced",
        "category": "operational_expense",
        "amount_usdc": 450.0,
        "requester": "ops-team",
        "description": "Urgent renewal to avoid service interruption, vendor is new to the system.",
        "due_date": "2026-09-29T00:00:00Z",
        "counterparty_wallet": "0x0000000000000000000000000000000000dEaD",
    },
    {
        "request_id": "demo-escalate",
        "category": "supply_purchase",
        "amount_usdc": 5000.0,
        "requester": "unknown_requester",
        "description": "Large one-off purchase with no prior history and vague justification.",
        "due_date": None,
    },
]


# The payees the demo scenarios pay. The payee check is on by default, so a
# payee the system has never paid is escalated for a person to confirm. These two
# are registered up front, exactly as a manager would register a known vendor, so
# the demo can show an automatic approval and a reduced approval.
# `demo-escalate` names an unknown requester and no wallet on purpose: it is the
# scenario that shows the check doing its job.
DEMO_PAYEES = {
    "contractor:known_vendor": "0x0000000000000000000000000000000000dEaD",
    "ops-team": "0x0000000000000000000000000000000000dEaD",
}


def register_demo_payees() -> list[str]:
    """Add the demo payees to the vendor registry. Idempotent, and it never
    overwrites a registration a manager already made. Returns the keys it added."""
    config = load_config()
    added = []
    for key, wallet in DEMO_PAYEES.items():
        normalized = key.strip().lower()
        if normalized in config.vendor_registry:
            continue
        config.vendor_registry[normalized] = VendorRegistration(
            key=normalized,
            wallets=[wallet],
            note="demo payee registered by demo/seed_scenarios.py",
            registered_by="demo/seed_scenarios.py",
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        added.append(normalized)
    if added:
        save_config(config)
    return added


def write_scenarios() -> None:
    """Write one JSON file per scenario.

    File names are stable (demo-approve.json, ...), but each run stamps a
    fresh suffix onto the request_id inside the file: the pipeline refuses
    to process the same request_id twice, so without this the demo could
    only be run once per audit log.
    """
    run_suffix = time.strftime("%m%d-%H%M%S")
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        path = SCENARIOS_DIR / f"{scenario['request_id']}.json"
        unique = {**scenario, "request_id": f"{scenario['request_id']}-{run_suffix}"}
        path.write_text(json.dumps(unique, indent=2), encoding="utf-8")
        print(f"Wrote {path} (request_id={unique['request_id']})")


if __name__ == "__main__":
    write_scenarios()
    for key in register_demo_payees():
        print(f"Registered demo payee {key!r} in the vendor registry")
