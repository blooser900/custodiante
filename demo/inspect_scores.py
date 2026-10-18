"""Debug helper: sends one payment request to the configured System-One
endpoint and prints the FULL raw answer (including probabilities) for each
question, bypassing normalization and the policy layer entirely.

Use this to investigate why a question's `confidence` looks off (e.g. 0.00)
before assuming it's a bug in our own code.

Usage:
    python -m demo.seed_scenarios        # creates demo/scenarios/*.json
    python -m demo.inspect_scores demo/scenarios/demo-approve.json
"""

import json
import sys
from pathlib import Path

# Same reason as demo/run_demo.py: support `python demo/inspect_scores.py` as
# well as `python -m demo.inspect_scores`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src.config import load_config
from src.decision_engine.criteria import build_questions, build_state
from src.decision_engine.fallback_classifier import (
    FALLBACK_API_KEY,
    FALLBACK_API_URL,
    FALLBACK_MODEL,
)
from src.requests.schema import PaymentRequest


def build_payload(request: PaymentRequest) -> dict:
    """Build the exact System-One payload the Kev engine would send."""
    config = load_config()
    return {
        "model": FALLBACK_MODEL,
        "state": build_state(request),
        "questions": build_questions(config.criteria, config.assessment_questions),
    }


def inspect(request_path: str) -> None:
    with open(request_path, encoding="utf-8") as f:
        request = PaymentRequest.model_validate(json.load(f))

    criteria = load_config().criteria
    headers = {"Authorization": f"Bearer {FALLBACK_API_KEY}", "Content-Type": "application/json"}

    response = httpx.post(
        FALLBACK_API_URL, json=build_payload(request), headers=headers, timeout=15
    )
    response.raise_for_status()
    answers = response.json()["answers"]

    print(f"=== Raw answers for {request_path} ===\n")
    for question_id, answer in answers.items():
        print(f"--- {question_id} ({answer.get('type', '?')}) ---")
        # Print whatever each type carries: a noul has only its probability of
        # yes; choice and score also have confidence and probabilities.
        for key in ("score", "choice", "noul", "confidence", "legend", "probabilities"):
            if key in answer:
                print(f"{key}: {answer[key]}")
        print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m demo.inspect_scores <path-to-request.json>")
        sys.exit(1)
    inspect(sys.argv[1])
