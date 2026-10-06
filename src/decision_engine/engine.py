"""Decision engine interface.

Every implementation (Jev, a trained classifier, an LLM constrained to a JSON
schema) must conform to this interface. Nothing outside this module should
need to know which one is actually running.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from src.requests.schema import PaymentRequest


class ScoreResult(BaseModel):
    """A single calibrated score with its confidence."""

    value: float = Field(..., ge=0, le=1, description="The score itself, normalized to [0, 1].")
    confidence: float = Field(
        ..., ge=0, le=1, description="How confident the engine is in this score."
    )


class DecisionScores(BaseModel):
    """The three scores produced for a given payment request."""

    trust: ScoreResult  # how legitimate / well-formed the request appears to be
    urgency: ScoreResult  # how time-sensitive the payment is
    importance: ScoreResult  # impact on the business if this payment is not made


class DecisionEngine(ABC):
    """Common interface for any scoring backend."""

    # Short identifier recorded in every audit entry, so a reviewer can tell
    # which backend produced a given set of scores (e.g. a real engine vs.
    # the demo mock). Subclasses should override it.
    name: str = "unknown"

    @abstractmethod
    def score(self, request: PaymentRequest) -> DecisionScores:
        """Return trust, urgency, and importance scores for a payment request.

        Implementations should NOT make the final approve/hold/escalate call.
        That decision belongs to the policy layer (see src/policy/rules.py),
        which enforces limits the engine itself cannot override.
        """
        raise NotImplementedError
