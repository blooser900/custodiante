"""CLI entrypoint: runs one payment request through the pipeline.

Usage:
    python -m src.main --input path/to/request.json

The actual logic lives in src/pipeline.py, shared with the HTTP API
(src/api.py) so the two never drift apart.
"""

import argparse
import json
import sys

from src.pipeline import run_pipeline
from src.requests.schema import PaymentRequest


def process_request(request: PaymentRequest, engine=None, arc_client=None) -> None:
    """Thin CLI wrapper -- kept for backwards compatibility with existing
    tests and call sites. Prefer src.pipeline.run_pipeline for new code.
    """
    run_pipeline(request, engine=engine, arc_client=arc_client, verbose=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Process a payment request.")
    parser.add_argument("--input", required=True, help="Path to a JSON payment request.")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    request = PaymentRequest.model_validate(data)
    try:
        process_request(request)
    except RuntimeError as e:
        # A clear, short message beats a 40-line traceback, especially
        # mid-demo. RuntimeErrors raised inside the pipeline (decision
        # engine unreachable, Arc/Circle misconfigured, etc.) already carry
        # an actionable message -- just print it, not everything above it.
        print(f"\nFailed to process {args.input}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
