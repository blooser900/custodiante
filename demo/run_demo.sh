#!/usr/bin/env bash
# Linux/macOS convenience wrapper. `run_demo.py` is the canonical runner and
# works on every platform, including Windows, which is why the README points
# there: this file only exists so an existing muscle memory of
# `bash demo/run_demo.sh` keeps working.
#
# Kept as a wrapper rather than a copy of the steps, so the two cannot drift
# apart and fix scenarios in a different order.
set -euo pipefail

exec python -m demo.run_demo "$@"
