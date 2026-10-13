"""Shared building blocks for System-One decision engines.

The rubric text (trust/urgency/importance level descriptions) lives in
config/rules.yaml, loaded via src.config.load_config(). This module holds
the pieces that every System-One-compatible engine (Jev, Kev, ...) shares,
so the engines cannot drift apart: the request -> `state` mapping, the
`questions` payload, and score normalization.
"""

from datetime import datetime, timezone

from src.config import QuestionSpec, ScoringCriteria
from src.history import HistoryFeatures
from src.requests.schema import PaymentRequest

# Order matters for display purposes (e.g. demo/inspect_scores.py).
SCORE_NAMES = ("trust", "urgency", "importance")


def normalize_score(raw_score: float, num_levels: int) -> float:
    """Map a raw System-One score in [0, num_levels - 1] to [0, 1]."""
    return raw_score / (num_levels - 1)


def _local_time_parts(submitted_at: datetime | None) -> tuple[str | None, str | None, str | None]:
    """The submission time, plus the local hour and offset where we are running.

    Returns `(iso_utc, local_hour, utc_offset)`, all None when the request
    carries no timestamp.

    The hour of day is one of the strongest fraud signals available and nothing
    else in the state records when the request happened. `submitted_at` is
    normalized to UTC first, then converted to the *server's* local zone for the
    hour, because "03:14" only means something next to the zone it is 03:14 in.
    A naive timestamp is read as UTC: the alternative, assuming it is local, is
    a guess, and a wrong guess here silently mislabels the signal.
    """
    if submitted_at is None:
        return None, None, None
    moment = submitted_at if submitted_at.tzinfo else submitted_at.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc)
    local = moment.astimezone()
    return utc.isoformat(), local.strftime("%H:%M"), local.strftime("%z")


def build_state(request: PaymentRequest, history: HistoryFeatures | None = None) -> dict:
    """Build the structured state the engine evaluates the questions against.

    `history` is what the system itself has recorded about the requester and
    the destination (see src/history.py). It is omitted entirely when not
    given, so the state is exactly the request on its own.
    """
    submitted_utc, local_hour, utc_offset = _local_time_parts(request.submitted_at)
    state = {
        "category": request.category,
        "amount_usdc": request.amount_usdc,
        "requester": request.requester,
        "description": request.description,
        "due_date": request.due_date.isoformat() if request.due_date else None,
        "submitted_at": submitted_utc,
        "local_hour": local_hour,
        "utc_offset": utc_offset,
    }
    if history is not None:
        state["history"] = history.model_dump(mode="json")
    return state


def build_questions(
    criteria: ScoringCriteria, extra: dict[str, QuestionSpec] | None = None
) -> dict:
    """Build the questions for the engine: the three core Score questions
    from the configured rubric, plus any configured extra questions."""
    questions = {
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
    for question_id, spec in (extra or {}).items():
        questions[question_id] = spec.to_api()
    return questions
