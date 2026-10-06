"""Fallback decision engine: any self-hosted TypeSafe-System-One-compatible
server, used if the real Jev API is unavailable (waitlist, outage, cost).

Kev (https://github.com/jaredpalmer/kev) is the recommended backend here:
unlike other open Jev-style projects, it implements the exact same
POST /v1/systemone contract as the real API (same request/response shape,
Noul/Choice/Score, confidence, legend), and it is trained on general
decision-making data (support tickets, NLI, policy questions) rather than
a narrow domain like game-playing. That means this class works unmodified
against Kev, and would also work against the real Jev API or any other
server that speaks the same protocol -- it only needs a base_url and a
model name.

Two Jev-adjacent projects were evaluated and are NOT used here:
  - NanoJev (github.com/TianyuCodings/NanoJev) is trained only on game
    tasks (ViZDoom, maze, Snake) and, by its own documentation, does not
    yet implement the real API's `noul` type or return confidence/legend
    for Score -- it would not produce a meaningful trust/urgency/importance
    score for a payment request.
  - SemIf / OpenJev (github.com/TheoLeeCJ/SemIf) is a solid, more general
    research project, but exposes a one-question-at-a-time CLI tool with
    its own input shape, not an HTTP server matching the System One
    contract. Wiring it in would need a different, non-generic adapter.

Run Kev locally, e.g.:
    uv run --extra serve python -m kev.serve --run jaredpalmer/kev-4b --port 8009

Then point this client at it via FALLBACK_API_URL / FALLBACK_MODEL below.

The rubric text and number of levels for each scored question come from
config/rules.yaml (see src/config.py), same as jev_client.py -- the two
engines must stay interchangeable behind the DecisionEngine interface.
"""

import os

import httpx

from src.config import ScoringCriteria, load_config
from src.decision_engine._http import post_with_retry
from src.decision_engine.criteria import normalize_score
from src.decision_engine.engine import DecisionEngine, DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest

# Defaults match Kev's own quick-start (local server, no API key needed
# unless KEV_API_KEY was set when the server was started).
FALLBACK_API_URL = os.environ.get("FALLBACK_API_URL", "http://127.0.0.1:8009/v1/systemone")
FALLBACK_API_KEY = os.environ.get("FALLBACK_API_KEY", "local")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "kev-latest")


def _build_state(request: PaymentRequest) -> dict:
    """Build the structured state the engine evaluates the questions against."""
    return {
        "category": request.category,
        "amount_usdc": request.amount_usdc,
        "requester": request.requester,
        "description": request.description,
        "due_date": request.due_date.isoformat() if request.due_date else None,
    }


def _build_questions(criteria: ScoringCriteria) -> dict:
    # Identical shape to jev_client.py's questions on purpose: this engine
    # and the real Jev engine must be interchangeable behind the same
    # interface, and both read the same configured rubric.
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


class TypeSafeCompatibleEngine(DecisionEngine):
    """Generic client for any server implementing the System One API.

    Works against Kev out of the box. Also works against the real Jev API
    if pointed at it (in which case you could just use JevDecisionEngine
    instead) or against a future self-hosted alternative that implements
    the same contract.
    """

    def __init__(
        self,
        api_url: str = FALLBACK_API_URL,
        api_key: str = FALLBACK_API_KEY,
        model: str = FALLBACK_MODEL,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    def score(self, request: PaymentRequest) -> DecisionScores:
        if not self.api_key:
            raise RuntimeError(
                "FALLBACK_API_KEY is empty. Kev's local server doesn't "
                "check it by default, but an empty Bearer token is still "
                "rejected as a malformed header before the request is sent."
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
                f"Could not reach the decision engine at {self.api_url}. "
                "Is Kev running? Start it with:\n"
                "  uv run --extra serve python -m kev.serve "
                f"--run jaredpalmer/kev-4b --port 8009\n(original error: {e})"
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


# Backwards-compatible alias: this is the name referenced elsewhere in the
# project (README, spec doc) as "the fallback engine".
FallbackClassifierEngine = TypeSafeCompatibleEngine
