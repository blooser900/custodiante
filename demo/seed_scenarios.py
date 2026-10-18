"""Generates a handful of realistic payment requests for the demo video.

Keeping these separate from tests/fixtures: fixtures are for automated
tests, these are curated specifically to look good and tell a clear story
on camera (one auto-approve, one reduced approval, one escalation).
"""

import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Same reason as demo/run_demo.py: support `python demo/seed_scenarios.py` as
# well as `python -m demo.seed_scenarios`, which is what the README documents.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# Must run before the wallet below is resolved. This module is imported by
# run_demo.py and by tests without the pipeline, which is what usually loads
# .env; without it here, ESCROW_PROVIDER_WALLET_ADDRESS would be invisible.
load_dotenv()

from src.config import VendorRegistration, load_config, save_config

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# The wallet the demo pays. `.env` is the primary source: whoever clones this
# points ESCROW_PROVIDER_WALLET_ADDRESS at a wallet they control, and the demo
# pays there instead. The fallback is the conventional burn address -- well
# formed, and owned by nobody, so an unconfigured demo cannot send testnet USDC
# to a stranger by accident.
#
# Resolved once, and everything derives from this single value: the scenarios'
# counterparty_wallet and the payee registration MUST agree, or the payee check
# would escalate a scenario the demo means to approve.
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"
DEMO_WALLET = os.environ.get("ESCROW_PROVIDER_WALLET_ADDRESS", "").strip() or BURN_ADDRESS

SCENARIOS = [
    {
        "request_id": "demo-approve",
        "category": "contractor_milestone",
        "amount_usdc": 500.0,
        "requester": "contractor:known_vendor",
        "description": "Milestone 1 of 2, verified complete, vendor has a clean payment history.",
        "due_date": "2026-10-03T00:00:00Z",
        "counterparty_wallet": DEMO_WALLET,
    },
    {
        "request_id": "demo-reduced",
        "category": "operational_expense",
        "amount_usdc": 450.0,
        "requester": "ops-team",
        "description": "Urgent renewal to avoid service interruption, vendor is new to the system.",
        "due_date": "2026-09-29T00:00:00Z",
        "counterparty_wallet": DEMO_WALLET,
    },
    {
        "request_id": "demo-escalate",
        "category": "supply_purchase",
        "amount_usdc": 5000.0,
        "requester": "unknown_requester",
        # Deliberately free of the mock's `_TRUST_NEGATIVE` keywords. It used to
        # read "no prior history and vague justification", and that -- together
        # with the missing wallet -- pushed the mock's P(fraud) to 0.89, past the
        # 0.70 veto, so the reason shown on screen was "Fraud indicators vetoed
        # automatic approval" while every document described this scenario as a
        # request with no history. Naming fraud is a far stronger claim than the
        # scenario makes, and the reason is the whole point of showing it.
        #
        # What actually escalates it is the amount: 5000 USDC is above the 1000
        # USDC ceiling for `supply_purchase`, checked before trust or payee. That
        # is a documented rule and a good thing to put on camera.
        "description": (
            "Large one-off purchase from a supplier this company has not paid before, "
            "with a brief justification."
        ),
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
    "contractor:known_vendor": DEMO_WALLET,
    "ops-team": DEMO_WALLET,
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


def _unique_run_suffix() -> str:
    """A suffix that cannot collide with a run already sitting on disk.

    It used to be `time.strftime("%m%d-%H%M%S")`, which has one-second
    resolution: two runs in the same second -- which is what happens when
    `run_demo` is re-run immediately after a failure, or when a script drives
    it twice -- produced the SAME request_ids. The pipeline refuses a request_id
    it has already decided, so the second run died with DuplicateRequestError and
    the audit log (which is append-only and never pruned) kept the first one.

    A random tag removes the practical risk; the loop removes it entirely by
    checking the files this directory already holds, so a collision is
    impossible rather than merely unlikely.
    """
    stamp = time.strftime("%m%d-%H%M%S")
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    for tag in [secrets.token_hex(3), *(f"{n:02d}" for n in range(100))]:
        suffix = f"{stamp}-{tag}"
        if not any(f"-{suffix}" in p.read_text(encoding="utf-8") for p in SCENARIOS_DIR.glob("*.json")):
            return suffix
    raise RuntimeError(
        f"Could not find an unused run suffix in {SCENARIOS_DIR}; remove stale scenario files."
    )


def write_scenarios() -> None:
    """Write one JSON file per scenario.

    File names are stable (demo-approve.json, ...), but each run stamps a
    fresh suffix onto the request_id inside the file: the pipeline refuses
    to process the same request_id twice, so without this the demo could
    only be run once per audit log.
    """
    run_suffix = _unique_run_suffix()
    for scenario in SCENARIOS:
        path = SCENARIOS_DIR / f"{scenario['request_id']}.json"
        unique = {**scenario, "request_id": f"{scenario['request_id']}-{run_suffix}"}
        path.write_text(json.dumps(unique, indent=2), encoding="utf-8")
        print(f"Wrote {path} (request_id={unique['request_id']})")


if __name__ == "__main__":
    write_scenarios()
    for key in register_demo_payees():
        print(f"Registered demo payee {key!r} in the vendor registry")
