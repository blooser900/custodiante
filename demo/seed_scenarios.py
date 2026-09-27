"""Generates a handful of realistic payment requests for the demo video.

Keeping these separate from tests/fixtures: fixtures are for automated
tests, these are curated specifically to look good and tell a clear story
on camera (one auto-approve, one reduced approval, one escalation).
"""

import json
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
    },
    {
        "request_id": "demo-reduced",
        "category": "operational_expense",
        "amount_usdc": 450.0,
        "requester": "ops-team",
        "description": "Urgent renewal to avoid service interruption, vendor is new to the system.",
        "due_date": "2026-09-29T00:00:00Z",
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
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        path = SCENARIOS_DIR / f"{scenario['request_id']}.json"
        path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
        print(f"Wrote {path}")


if __name__ == "__main__":
    write_scenarios()
