"""Generates a handful of realistic payment requests for the demo video.

Keeping these separate from tests/fixtures: fixtures are for automated
tests, these are curated specifically to look good and tell a clear story
on camera (one auto-approve, one reduced approval, one escalation).
"""

import json
import time
from pathlib import Path

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
