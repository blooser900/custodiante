"""Decision engine backed by the real Jev API (TypeSafe AI).

Jev's System One endpoint evaluates a `state` against typed `questions`
(Noul, Choice, or Score) and returns calibrated answers -- never free text.

We model trust / urgency / importance as three Score questions. The rubric
text and number of levels for each come from config/rules.yaml (see
src/config.py), not from a hardcoded constant, so a business can tune them
without touching code. The raw score for an N-level rubric lands in
[0, N-1] (and can be fractional), so we normalize it to [0, 1] before
handing it to the policy layer, which expects normalized values.

Official API reference: https://docs.typesafe.ai/api
"""

import os

import httpx

from src.config import ScoringCriteria, load_config
from src.decision_engine._http import post_with_retry
from src.decision_engine.criteria import normalize_score
from src.decision_engine.engine import DecisionEngine, DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest

JEV_API_URL = os.environ.get("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_API_KEY = os.environ.get("JEV_API_KEY", "")
JEV_MODEL = "jev-latest"


def _build_state(request: PaymentRequest) -> dict:
    """Build the structured state Jev evaluates the questions against."""
    return {
        "category": request.category,
        "amount_usdc": request.amount_usdc,
        "requester": request.requester,
        "description": request.description,
        "due_date": request.due_date.isoformat() if request.due_date else None,
    }


def _build_questions(criteria: ScoringCriteria) -> dict:
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


class JevDecisionEngine(DecisionEngine):
    """Calls the real Jev System One API to score a payment request."""

    def __init__(
        self,
        api_key: str = JEV_API_KEY,
        api_url: str = JEV_API_URL,
        model: str = JEV_MODEL,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.model = model

    def score(self, request: PaymentRequest) -> DecisionScores:
        if not self.api_key:
            # Fail with a clear message here, rather than letting httpx
            # reject an "Authorization: Bearer " header with an empty
            # token as a raw protocol error further down.
            raise RuntimeError(
                "JEV_API_KEY is not set. Add it to .env, or switch "
                "DEFAULT_ENGINE_FACTORY in src/pipeline.py to the Kev "
                "fallback engine."
            )

        criteria = load_config().criteria
        payload = {
            "model": self.model,
            "state": _build_state(request),
            "questions": _build_questions(criteria),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = post_with_retry(self.api_url, payload, headers)
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"Could not reach the Jev API at {self.api_url}. Check your "
                f"network connection and JEV_API_KEY. (original error: {e})"
            ) from e
        answers = response.json()["answers"]

        return DecisionScores(
            trust=ScoreResult(
                value=normalize_score(answers["trust"]["score"], len(criteria.trust)),
                confidence=answers["trust"]["confidence"],
            ),
            urgency=ScoreResult(
                value=normalize_score(answers["urgency"]["score"], len(criteria.urgency)),
                confidence=answers["urgency"]["confidence"],
            ),
            importance=ScoreResult(
                value=normalize_score(answers["importance"]["score"], len(criteria.importance)),
                confidence=answers["importance"]["confidence"],
            ),
        )
