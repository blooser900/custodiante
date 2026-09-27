#!/usr/bin/env bash
# Runs the three demo scenarios end to end, for recording the 3-minute video.
set -euo pipefail

python -m demo.seed_scenarios

for scenario in demo/scenarios/*.json; do
  echo "--- Processing $scenario ---"
  python -m src.main --input "$scenario"
  echo ""
done

echo "--- Generating report ---"
python -m src.reports.generate_report
