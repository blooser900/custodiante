"""Deterministic keyword-based decision engine for demos and offline work.

This is NOT a model. It exists so the full pipeline (policy, audit log,
dashboard) can run end to end with no external service: no Jev API key, no
local Kev server. Every audit entry it produces is tagged `engine_name =
"mock"`, so it can never be mistaken for a real calibrated decision.

Select it with DECISION_ENGINE=mock (see src/pipeline.py).

Scores snap to the same discrete levels a 3-level System-One rubric yields
(0.0 / 0.5 / 1.0), and confidence is high only when a strong signal was
found, which mimics how the real engines behave closely enough for the
policy layer's thresholds to be exercised.
"""

from datetime import datetime, timezone

from src.decision_engine.engine import DecisionEngine, DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest

# Keyword lists are matched case-insensitively against requester + description.
_TRUST_POSITIVE = ("verified", "clean payment history", "clean history", "known", "documented", "invoice")
_TRUST_NEGATIVE = ("unknown", "vague", "no prior", "no history", "suspicious", "unverified")
_TRUST_CAUTION = ("new to the system", "first time", "new vendor")

_URGENCY_KEYWORDS = ("urgent", "asap", "deadline", "interruption", "immediately", "overdue", "expires")
_IMPORTANCE_KEYWORDS = ("outage", "interruption", "critical", "compliance", "legal", "payroll", "security")

_STRONG_CONFIDENCE = 0.9
_WEAK_CONFIDENCE = 0.6


def _snap(value: float) -> float:
    """Snap a raw value to the nearest of 0.0 / 0.5 / 1.0."""
    return min((0.0, 0.5, 1.0), key=lambda level: abs(level - value))


def _count_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


class MockDecisionEngine(DecisionEngine):
    """Keyword heuristics standing in for a calibrated decision model."""

    name = "mock"

    def score(self, request: PaymentRequest) -> DecisionScores:
        text = f"{request.requester} {request.description}".lower()
        return DecisionScores(
            trust=self._trust(text),
            urgency=self._urgency(text, request),
            importance=self._importance(text),
        )

    def _trust(self, text: str) -> ScoreResult:
        positive = _count_hits(text, _TRUST_POSITIVE)
        negative = _count_hits(text, _TRUST_NEGATIVE)
        caution = _count_hits(text, _TRUST_CAUTION)

        if negative:
            # Negative evidence wins: one red flag outweighs any amount of praise.
            return ScoreResult(value=0.0, confidence=_STRONG_CONFIDENCE)
        if caution:
            return ScoreResult(value=0.5, confidence=_STRONG_CONFIDENCE)
        if positive >= 2:
            return ScoreResult(value=1.0, confidence=_STRONG_CONFIDENCE)
        if positive == 1:
            return ScoreResult(value=_snap(0.75), confidence=_WEAK_CONFIDENCE)
        return ScoreResult(value=0.5, confidence=_WEAK_CONFIDENCE)

    def _urgency(self, text: str, request: PaymentRequest) -> ScoreResult:
        hits = _count_hits(text, _URGENCY_KEYWORDS)
        days_left = self._days_until_due(request)
        if days_left is not None and days_left <= 3:
            hits += 1  # An imminent (or already passed) due date is a signal too.

        if hits >= 2:
            return ScoreResult(value=1.0, confidence=_STRONG_CONFIDENCE)
        if hits == 1:
            return ScoreResult(value=0.5, confidence=_WEAK_CONFIDENCE)
        return ScoreResult(value=0.0, confidence=_WEAK_CONFIDENCE)

    def _importance(self, text: str) -> ScoreResult:
        hits = _count_hits(text, _IMPORTANCE_KEYWORDS)
        if hits >= 1:
            return ScoreResult(value=1.0, confidence=_STRONG_CONFIDENCE)
        return ScoreResult(value=0.0, confidence=_WEAK_CONFIDENCE)

    @staticmethod
    def _days_until_due(request: PaymentRequest) -> float | None:
        if request.due_date is None:
            return None
        due = request.due_date
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return (due - datetime.now(timezone.utc)).total_seconds() / 86400
