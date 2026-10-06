"""Shared building blocks for System-One decision engines.

The rubric text (trust/urgency/importance level descriptions) lives in
config/rules.yaml, loaded via src.config.load_config(). This module holds
the pieces that every System-One-compatible engine (Jev, Kev, ...) shares,
so the engines cannot drift apart: the request -> `state` mapping, the
`questions` payload, and score normalization.
"""

from src.config import ScoringCriteria
from src.requests.schema import PaymentRequest

# Order matters for display purposes (e.g. demo/inspect_scores.py).
SCORE_NAMES = ("trust", "urgency", "importance")


def normalize_score(raw_score: float, num_levels: int) -> float:
    """Map a raw System-One score in [0, num_levels - 1] to [0, 1]."""
    return raw_score / (num_levels - 1)


def build_state(request: PaymentRequest) -> dict:
    """Build the structured state the engine evaluates the questions against."""
    return {
        "category": request.category,
        "amount_usdc": request.amount_usdc,
        "requester": request.requester,
        "description": request.description,
        "due_date": request.due_date.isoformat() if request.due_date else None,
    }


def build_questions(criteria: ScoringCriteria) -> dict:
    """Build the three typed Score questions from the configured rubric."""
    return {
        "trust": {
            "type": "score",
            "instructions": "How legitimate and well-supported is this payment request?",
            "criteria": criteria.trust,
        },
        "urgency": {
            "type": "score",
            "instructions": "How time-sensitive is this payment?",
            "criteria": criteria.urgency,
        },
        "importance": {
            "type": "score",
            "instructions": "What is the impact on the business if this payment is NOT made?",
            "criteria": criteria.importance,
        },
    }
