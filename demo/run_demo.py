"""Runs the demo scenarios end to end, on any platform.

`demo/run_demo.sh` did this in bash, which cannot be executed by the command
prompt or PowerShell -- on the platform this project is developed on. The steps
are the same three, and the runner needs nothing the app itself does not
already need:

    python -m demo.run_demo              # seed, run every scenario, report
    python -m demo.run_demo --no-seed    # re-run the files already on disk

Exit code is 0 only when seed, every scenario and the report all succeeded, so
it can be used in a script the way `set -e` used it before.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# `python -m demo.run_demo` puts the repository root on sys.path; running this
# file by its path (`python demo/run_demo.py`, which is the spelling people and
# demo judges actually type) puts demo/ there instead, and then
# `from demo.seed_scenarios import ...` fails with ModuleNotFoundError.
# Support both.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# The interpreter running this file, so the child processes use the same
# environment (a venv, in practice) instead of whatever "python" resolves to.
PYTHON = sys.executable


def seed() -> int:
    """Write the scenario files. Returns a process exit code."""
    from demo.seed_scenarios import register_demo_payees, write_scenarios

    write_scenarios()
    for key in register_demo_payees():
        print(f"Registered demo payee {key!r} in the vendor registry")
    return 0


def _process(scenario_path: Path) -> int:
    """Run one scenario file through the CLI. Returns a process exit code."""
    print(f"--- Processing {scenario_path} ---")
    completed = subprocess.run(
        [PYTHON, "-m", "src.main", "--input", str(scenario_path)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    print()
    return completed.returncode


def _report() -> int:
    print("--- Generating report ---")
    completed = subprocess.run(
        [PYTHON, "-m", "src.reports.generate_report"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the demo scenarios end to end.")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="skip seeding; run whatever is already in demo/scenarios/",
    )
    args = parser.parse_args(argv)

    if not args.no_seed:
        exit_code = seed()
        if exit_code != 0:
            return exit_code

    scenarios = sorted(SCENARIOS_DIR.glob("*.json"))
    if not scenarios:
        print(
            f"No scenarios in {SCENARIOS_DIR}. Run `python -m demo.seed_scenarios` first.",
            file=sys.stderr,
        )
        return 1

    for scenario_path in scenarios:
        exit_code = _process(scenario_path)
        if exit_code != 0:
            # Stop at the first failure, like `set -e` did: the remaining
            # scenarios would be decided against a log the failed one did not
            # finish writing to, and the report would be misleading.
            print(f"Stopped: {scenario_path.name} failed.", file=sys.stderr)
            return exit_code

    report_code = _report()
    return 0 if report_code == 0 else report_code


if __name__ == "__main__":
    sys.exit(main())
