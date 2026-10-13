"""Decision engine interface.

Every implementation (Jev, a trained classifier, an LLM constrained to a JSON
schema) must conform to this interface. Nothing outside this module should
need to know which one is actually running.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from src.decision_engine.answers import Answer
from src.history import HistoryFeatures
from src.requests.schema import PaymentRequest


class ScoreResult(BaseModel):
    """A single calibrated score with its confidence."""

    value: float = Field(..., ge=0, le=1, description="The score itself, normalized to [0, 1].")
    confidence: float = Field(
        ..., ge=0, le=1, description="How confident the engine is in this score."
    )


class Evidence(BaseModel):
    """Exactly what the engine was shown and what it answered.

    Stored in the audit log with every decision. The history fed to the
    engine changes over time, so without this a decision could not be
    explained later: the log would say what was decided but not what the
    model saw.
    """

    model: str | None = None  # the model that answered (e.g. "jev-1.13.0")
    state: dict  # the state sent to the engine
    answers: dict  # the engine's raw answers, probabilities included
    latency_ms: float | None = None
    # Extra answers that were missing or malformed and therefore left out.
    problems: list[str] = Field(default_factory=list)


class DecisionScores(BaseModel):
    """The three core scores for a payment request, plus what else the
    engine said about it."""

    trust: ScoreResult  # how legitimate / well-formed the request appears to be
    urgency: ScoreResult  # how time-sensitive the payment is
    importance: ScoreResult  # impact on the business if this payment is not made
    # Answers to the configured extra questions, by question id. Recorded and
    # shown to managers; the policy does not read them yet.
    extra: dict[str, Answer] = Field(default_factory=dict)
    evidence: Evidence | None = None


class DecisionEngine(ABC):
    """Common interface for any scoring backend."""

    # Short identifier recorded in every audit entry, so a reviewer can tell
    # which backend produced a given set of scores (e.g. a real engine vs.
    # the demo mock). Subclasses should override it.
    name: str = "unknown"

    @abstractmethod
    def score(
        self, request: PaymentRequest, history: HistoryFeatures | None = None
    ) -> DecisionScores:
        """Return trust, urgency, and importance scores for a payment request.

        `history` is what the system has recorded about the requester and the
        destination; engines that can use it should put it in the state.

        Implementations should NOT make the final approve/hold/escalate call.
        That decision belongs to the policy layer (see src/policy/rules.py),
        which enforces limits the engine itself cannot override.
        """
        raise NotImplementedError
