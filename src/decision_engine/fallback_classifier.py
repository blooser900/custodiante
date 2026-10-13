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

from src.decision_engine.criteria import build_questions, build_state
from src.decision_engine.system_one import FROM_ENV, SystemOneEngine

# Defaults match Kev's own quick-start (local server, no API key needed
# unless KEV_API_KEY was set when the server was started).
FALLBACK_API_URL = os.environ.get("FALLBACK_API_URL", "http://127.0.0.1:8009/v1/systemone")
FALLBACK_API_KEY = os.environ.get("FALLBACK_API_KEY", "local")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "kev-latest")

# Kept for backwards compatibility; the shared builders live in criteria.py.
_build_state = build_state
_build_questions = build_questions


class TypeSafeCompatibleEngine(SystemOneEngine):
    """Generic client for any server implementing the System One API.

    Works against Kev out of the box. Also works against the real Jev API
    if pointed at it (in which case you could just use JevDecisionEngine
    instead) or against a future self-hosted alternative that implements
    the same contract.
    """

    name = "kev"

    def __init__(
        self,
        api_url: str = FALLBACK_API_URL,
        api_key: str = FALLBACK_API_KEY,
        model: str = FALLBACK_MODEL,
        timeout: float | None = FROM_ENV,
    ) -> None:
        super().__init__(api_url, api_key, model, timeout)

    def _missing_key_message(self) -> str:
        return (
            "FALLBACK_API_KEY is empty. Kev's local server doesn't "
            "check it by default, but an empty Bearer token is still "
            "rejected as a malformed header before the request is sent."
        )

    def _unreachable_message(self, error: Exception) -> str:
        return (
            f"Could not reach the decision engine at {self.api_url}. "
            "Is Kev running? Start it with:\n"
            "  uv run --extra serve python -m kev.serve "
            f"--run jaredpalmer/kev-4b --port 8009\n(original error: {error})"
        )


# Backwards-compatible alias: this is the name referenced elsewhere in the
# project (README, spec doc) as "the fallback engine".
FallbackClassifierEngine = TypeSafeCompatibleEngine
