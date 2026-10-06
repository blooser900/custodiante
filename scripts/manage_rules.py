"""CLI to view and edit config/rules.yaml -- category limits, policy
thresholds, and scoring rubrics -- without touching Python code.

Usage:
    python -m scripts.manage_rules show
    python -m scripts.manage_rules set-limit operational_expense 800
    python -m scripts.manage_rules set-threshold trust_min_auto_approve 0.8
    python -m scripts.manage_rules set-criteria-level importance 2 "Critical: ..."
    python -m scripts.manage_rules edit       # opens $EDITOR on the YAML file
    python -m scripts.manage_rules validate   # loads and reports errors, if any
"""

import argparse
import os
import subprocess
import sys

from pydantic import ValidationError

from src.config import CONFIG_PATH, load_config, save_config


def cmd_show(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"Config file: {CONFIG_PATH}\n")

    print("Category limits (USDC):")
    for category, limit in config.category_limits.items():
        print(f"  {category:<22} {limit}")

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
    if args.category not in config.category_limits:
        print(
            f"Warning: {args.category!r} is not an existing category "
            f"({sorted(config.category_limits)}). Adding it anyway, but note "
            "that a brand-new RequestCategory still needs a matching change "
            "in src/requests/schema.py to actually be usable.",
            file=sys.stderr,
        )
    config.category_limits[args.category] = args.amount
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


def cmd_edit(args: argparse.Namespace) -> int:
    # config/rules.yaml is versioned, so it exists in a normal checkout; the
    # editor creates it if it was deleted by hand.
    editor = os.environ.get("EDITOR", "nano")
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

    sub.add_parser("edit", help="Open config/rules.yaml in $EDITOR, then validate it.")
    sub.add_parser("validate", help="Load and validate config/rules.yaml.")

    args = parser.parse_args()
    handlers = {
        "show": cmd_show,
        "set-limit": cmd_set_limit,
        "set-threshold": cmd_set_threshold,
        "set-criteria-level": cmd_set_criteria_level,
        "edit": cmd_edit,
        "validate": cmd_validate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
