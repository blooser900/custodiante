"""Debug helper: sends one payment request to the configured System-One
endpoint and prints the FULL raw answer (including probabilities) for each
question, bypassing normalization and the policy layer entirely.

Use this to investigate why a question's `confidence` looks off (e.g. 0.00)
before assuming it's a bug in our own code.

Usage:
    python -m demo.inspect_scores demo/scenarios/demo-approve.json
"""

import json
import sys

import httpx

from src.decision_engine.criteria import IMPORTANCE_CRITERIA, TRUST_CRITERIA, URGENCY_CRITERIA
from src.decision_engine.fallback_classifier import (
    FALLBACK_API_KEY,
    FALLBACK_API_URL,
    FALLBACK_MODEL,
    _build_questions,
    _build_state,
)
from src.requests.schema import PaymentRequest


def inspect(request_path: str) -> None:
    with open(request_path, encoding="utf-8") as f:
        data = json.load(f)
    request = PaymentRequest.model_validate(data)

    payload = {
        "model": FALLBACK_MODEL,
        "state": _build_state(request),
        "questions": _build_questions(),
    }
    headers = {"Authorization": f"Bearer {FALLBACK_API_KEY}", "Content-Type": "application/json"}

    response = httpx.post(FALLBACK_API_URL, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    answers = response.json()["answers"]

    print(f"=== Raw answers for {request_path} ===\n")
    for question_id, criteria in (
        ("trust", TRUST_CRITERIA),
        ("urgency", URGENCY_CRITERIA),
        ("importance", IMPORTANCE_CRITERIA),
    ):
        answer = answers[question_id]
        print(f"--- {question_id} ---")
        print(f"criteria: {criteria}")
        print(f"raw score: {answer['score']}")
        print(f"confidence: {answer['confidence']}")
        print(f"legend: {answer.get('legend')}")
        print(f"probabilities: {answer.get('probabilities')}")
        print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m demo.inspect_scores <path-to-request.json>")
        sys.exit(1)
    inspect(sys.argv[1])
