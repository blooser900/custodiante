"""CLI to view and edit config/rules.yaml -- category limits, policy
thresholds, and scoring rubrics -- without touching Python code.

Category limits are bounded by the funds the wallet actually holds, and that
check FAILS OPEN: if the balance cannot be read, a limit above the funds is
accepted. `show` prints which of the two states the check is in, so the state of
this control is never a guess. See src/policy/ceiling.py.

Usage:
    python -m scripts.manage_rules show
    python -m scripts.manage_rules set-limit operational_expense 800
    python -m scripts.manage_rules set-threshold trust_min_auto_approve 0.8
    python -m scripts.manage_rules set-criteria-level importance 2 "Critical: ..."
    python -m scripts.manage_rules register-vendor "Acme Ltda" 0xabc... --note "invoice 123"
    python -m scripts.manage_rules payee-check off   # demos only; ON by default
    python -m scripts.manage_rules edit       # opens $EDITOR on the YAML file
    python -m scripts.manage_rules validate   # loads and reports errors, if any
"""

import argparse
import getpass
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from pydantic import ValidationError

from src.config import (
    CATEGORIES_PAID_WHOLE,
    CONFIG_PATH,
    CategoryLimit,
    VendorRegistration,
    load_config,
    save_config,
)
from src.policy.ceiling import (
    LimitAboveCeilingError,
    check_limits_against_balance,
    require_limits_within_balance,
)


def cmd_show(args: argparse.Namespace) -> int:
    # The balance check logs a warning when it cannot read the wallet, and this
    # command prints that state in its own section below -- with the reason. The
    # log line would be the same sentence twice, and its traceback is noise on a
    # screen meant to be read.
    logging.getLogger("src.policy.ceiling").setLevel(logging.ERROR)

    config = load_config()
    print(f"Config file: {CONFIG_PATH}\n")

    print("\nCategory limits (USDC):")
    for category, limit in config.category_limits.items():
        part = "part payment allowed" if limit.allow_reduced else "must be paid whole"
        print(f"  {category:<22} {limit.max_usdc:<10} {part}")

    # The limits above are bounded by the funds available, and that check fails
    # open when Circle cannot be reached -- so the state of the check is printed
    # rather than left for an operator to discover. See src/policy/ceiling.py.
    check = check_limits_against_balance(config)
    print("\nLimit ceiling (the funds available):")
    if check.checked:
        state = "OK" if check.ok else "VIOLATED"
        print(f"  {state} -- {check.detail}")
        if not check.ok:
            print("  Lower the limits above, or fund the wallet. Saving is refused until then.")
    else:
        print("  NOT CHECKED -- the wallet balance could not be read, so the limits above")
        print("  are unverified: a limit larger than the funds available will be accepted.")
        print(f"  Reason: {check.detail}")

    print("\nPayee check:")
    if config.require_known_payee:
        print("  ON  -- the first payment to each payee is confirmed by a person; the audit log")
        print("         then remembers the wallet, and a different wallet for a known payee escalates")
    else:
        print("  OFF -- any payee and any wallet can be auto-approved (demo mode: see `payee-check on`)")

    print("\nVendor registry:")
    if not config.vendor_registry:
        print("  (empty -- every payee is unknown until a person approves a first payment)")
    for key, registration in config.vendor_registry.items():
        wallets = ", ".join(registration.wallets)
        print(f"  {key:<22} {wallets}")
        if registration.note:
            print(f"  {'':<22} {registration.note}")

    print("\nThresholds:")
    for name, value in config.thresholds.model_dump().items():
        print(f"  {name:<40} {value}")

    print("\nScoring rubrics:")
    for question, levels in config.criteria.model_dump().items():
        print(f"  {question}:")
        for i, text in enumerate(levels):
            print(f"    [{i}] {text}")
    return 0


def cmd_set_limit(args: argparse.Namespace) -> int:
    config = load_config()
    existing = config.category_limits.get(args.category)
    if existing is None:
        print(
            f"Warning: {args.category!r} is not an existing category "
            f"({sorted(config.category_limits)}). Adding it anyway, but note "
            "that a brand-new RequestCategory still needs a matching change "
            "in src/requests/schema.py to actually be usable.",
            file=sys.stderr,
        )
    # Replace only the ceiling: whether a part payment is a valid outcome for
    # this category is a separate decision, so changing the amount must not
    # silently flip it. A brand-new category follows the shipped default.
    config.category_limits[args.category] = CategoryLimit(
        max_usdc=args.amount,
        allow_reduced=existing.allow_reduced if existing else args.category not in CATEGORIES_PAID_WHOLE,
    )
    # A ceiling above the funds available is not a limit. Same rule the
    # API enforces, and it fails open when the balance cannot be read -- see
    # src/policy/ceiling.py -- so an unreachable Circle does not make the rules
    # uneditable.
    try:
        check = require_limits_within_balance(config)
    except LimitAboveCeilingError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    if not check.checked:
        print(f"Warning: {check.detail}", file=sys.stderr)
    save_config(config)
    print(f"Set limit for {args.category!r} to {args.amount} USDC.")
    return 0


def cmd_set_threshold(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        setattr(config.thresholds, args.name, args.value)
        config.thresholds = config.thresholds.__class__.model_validate(
            config.thresholds.model_dump()
        )
    except ValidationError as e:
        print(f"Invalid value: {e}", file=sys.stderr)
        return 1
    save_config(config)
    print(f"Set threshold {args.name!r} to {args.value}.")
    return 0


def cmd_set_criteria_level(args: argparse.Namespace) -> int:
    config = load_config()
    levels = getattr(config.criteria, args.question)
    if not (0 <= args.index < len(levels)):
        print(
            f"Index {args.index} out of range for {args.question!r} "
            f"(has {len(levels)} levels, 0..{len(levels) - 1}).",
            file=sys.stderr,
        )
        return 1
    levels[args.index] = args.text
    save_config(config)
    print(f"Updated {args.question}[{args.index}] to: {args.text}")
    return 0


def cmd_payee_check(args: argparse.Namespace) -> int:
    config = load_config()
    config.require_known_payee = args.state == "on"
    save_config(config)
    if config.require_known_payee:
        print("Payee check is ON.")
    else:
        print(
            "Payee check is OFF: any payee and any wallet can now be auto-approved. "
            "Meant for demos; turn it back on with `payee-check on`.",
            file=sys.stderr,
        )
    return 0


def cmd_register_vendor(args: argparse.Namespace) -> int:
    """Register the wallets a payee may be paid at, merging with what is there.

    Adds, never replaces: a vendor that really moved banks gets the new wallet
    alongside the old one, and removing the old one is a deliberate edit
    (`edit`), not a side effect of registering.
    """
    config = load_config()
    key = args.key.strip().lower()
    existing = config.vendor_registry.get(key)
    wallets = sorted({*(existing.wallets if existing else []), *(w.strip().lower() for w in args.wallets)})
    try:
        registration = VendorRegistration(
            key=key,
            wallets=wallets,
            note=args.note if args.note is not None else (existing.note if existing else None),
            registered_by=args.by or (existing.registered_by if existing else None) or _current_user(),
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
    except ValidationError as e:
        print(f"Invalid registration: {e}", file=sys.stderr)
        return 1
    config.vendor_registry[registration.key] = registration
    save_config(config)
    print(f"Registered {registration.key!r}: {', '.join(registration.wallets)}")
    return 0


def _current_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- no login name in some containers; the field is optional
        return None


def _default_editor() -> str:
    """The editor to open when $EDITOR is not set.

    `nano` is a reasonable default on Linux and does not exist on a stock
    Windows install, where the command failed with "not recognized" -- on the
    platform this project is actually developed on. $EDITOR still wins when it
    is set, so anyone with a preference keeps it.
    """
    return "notepad" if os.name == "nt" else "nano"


def cmd_edit(args: argparse.Namespace) -> int:
    load_config()  # ensure the file exists before opening it
    editor = os.environ.get("EDITOR") or _default_editor()
    subprocess.run([editor, str(CONFIG_PATH)])
    return cmd_validate(args)


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        load_config()
    except ValidationError as e:
        print(f"config/rules.yaml is invalid:\n{e}", file=sys.stderr)
        return 1
    print("config/rules.yaml is valid.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage business rules without editing code.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="Print the current configuration.")

    p = sub.add_parser("set-limit", help="Set the USDC limit for a category.")
    p.add_argument("category")
    p.add_argument("amount", type=float)

    p = sub.add_parser("set-threshold", help="Set a policy threshold.")
    p.add_argument("name")
    p.add_argument("value", type=float)

    p = sub.add_parser("set-criteria-level", help="Edit one level of a scoring rubric.")
    p.add_argument("question", choices=["trust", "urgency", "importance"])
    p.add_argument("index", type=int)
    p.add_argument("text")

    p = sub.add_parser(
        "payee-check",
        help="Turn the payee check on or off (it is ON by default; off is for demos).",
    )
    p.add_argument("state", choices=["on", "off"])

    p = sub.add_parser(
        "register-vendor",
        help="Register the wallet(s) a payee may be paid at. Adds to existing ones, never replaces.",
    )
    p.add_argument("key", help="The payee as it is typed on requests (case-insensitive).")
    p.add_argument("wallets", nargs="+")
    p.add_argument("--note", default=None, help="Why these wallets, for whoever reads the log later.")
    p.add_argument("--by", default=None, help="Who is registering it (defaults to the OS user).")

    sub.add_parser("edit", help="Open config/rules.yaml in $EDITOR, then validate it.")
    sub.add_parser("validate", help="Load and validate config/rules.yaml.")

    args = parser.parse_args()
    handlers = {
        "show": cmd_show,
        "set-limit": cmd_set_limit,
        "set-threshold": cmd_set_threshold,
        "set-criteria-level": cmd_set_criteria_level,
        "payee-check": cmd_payee_check,
        "register-vendor": cmd_register_vendor,
        "edit": cmd_edit,
        "validate": cmd_validate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
