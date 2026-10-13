"""Decision engine backed by the real Jev API (TypeSafe AI).

Jev's System One endpoint evaluates a `state` against typed `questions`
(Noul, Choice, or Score) and returns calibrated answers -- never free text.

We model trust / urgency / importance as three Score questions, and ask
the configured extra questions (noul / choice / score) in the same request. The rubric
text and number of levels for each come from config/rules.yaml (see
src/config.py), not from a hardcoded constant, so a business can tune them
without touching code. The raw score for an N-level rubric lands in
[0, N-1] (and can be fractional), so we normalize it to [0, 1] before
handing it to the policy layer, which expects normalized values.

Official API reference: https://docs.typesafe.ai/api
"""

import os

from src.decision_engine.criteria import build_questions, build_state
from src.decision_engine.system_one import FROM_ENV, SystemOneEngine

JEV_API_URL = os.environ.get("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_API_KEY = os.environ.get("JEV_API_KEY", "")
# "jev-latest" moves when TypeSafe releases a new version, which can change
# scores and confidence values. For decisions that must be reproducible, pin
# a version (e.g. JEV_MODEL=jev-1.13.0); the model that answered is recorded
# in every audit entry.
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

# Kept for backwards compatibility; the shared builders live in criteria.py.
_build_state = build_state
_build_questions = build_questions


class JevDecisionEngine(SystemOneEngine):
    """Calls the real Jev System One API to score a payment request."""

    name = "jev"

    def __init__(
        self,
        api_key: str = JEV_API_KEY,
        api_url: str = JEV_API_URL,
        model: str = JEV_MODEL,
        timeout: float | None = FROM_ENV,
    ) -> None:
        super().__init__(api_url, api_key, model, timeout)

    def _missing_key_message(self) -> str:
        return (
            "JEV_API_KEY is not set. Add it to .env, or set DECISION_ENGINE=kev "
            "to use the self-hosted engine instead."
        )

    def _unreachable_message(self, error: Exception) -> str:
        return (
            f"Could not reach the Jev API at {self.api_url}. Check your "
            f"network connection and JEV_API_KEY. (original error: {error})"
        )
