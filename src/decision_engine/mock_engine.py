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

import math
from datetime import datetime, timezone

from src.config import QuestionSpec, load_config
from src.decision_engine.answers import parse_answer
from src.decision_engine.criteria import build_state
from src.decision_engine.engine import DecisionEngine, DecisionScores, Evidence, ScoreResult
from src.history import HistoryFeatures
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


def _concentration(probabilities: list[float]) -> float:
    """Confidence as the real engine derives it: from the distribution's shape
    (1.0 when one option has everything, 0 when all are equally likely)."""
    n = len(probabilities)
    return round(max(0.0, (n * max(probabilities) - 1) / (n - 1)), 4)


class MockDecisionEngine(DecisionEngine):
    """Keyword heuristics standing in for a calibrated decision model.

    Answers every configured question in the real schema, and runs its own
    output through the same parser the real engines use, so the mock cannot
    drift from the contract the rest of the system relies on.
    """

    name = "mock"

    def score(
        self, request: PaymentRequest, history: HistoryFeatures | None = None
    ) -> DecisionScores:
        config = load_config()
        text = f"{request.requester} {request.description}".lower()
        trust = self._trust(text)
        urgency = self._urgency(text, request)
        importance = self._importance(text)

        raw: dict = {}
        for question_id, result, levels in (
            ("trust", trust, config.criteria.trust),
            ("urgency", urgency, config.criteria.urgency),
            ("importance", importance, config.criteria.importance),
        ):
            raw[question_id] = {
                "type": "score",
                "score": round(result.value * (len(levels) - 1), 4),
                "confidence": result.confidence,
            }
        raw.update(self._extra_answers(text, history, config.assessment_questions))

        extra, problems = {}, []
        for question_id, spec in config.assessment_questions.items():
            try:
                extra[question_id] = parse_answer(question_id, spec, raw.get(question_id))
            except ValueError as e:
                problems.append(str(e))

        return DecisionScores(
            trust=trust,
            urgency=urgency,
            importance=importance,
            extra=extra,
            evidence=Evidence(
                model="mock",
                state=build_state(request, history),
                answers=raw,
                latency_ms=0.0,
                problems=problems,
            ),
        )

    # -- extra questions ---------------------------------------------------

    def _extra_answers(
        self, text: str, history: HistoryFeatures | None, questions: dict[str, QuestionSpec]
    ) -> dict:
        ratio = None
        bad = attempts = 0
        no_destination = history is not None and history.destination is None
        if history is not None:
            ratio = history.amount_vs_destination_avg or history.amount_vs_requester_avg
            for party in (history.requester, history.destination):
                if party is not None:
                    bad += party.failed + party.refunded + party.denied
                    attempts += party.attempts

        fraud = 0.04
        if any(k in text for k in _TRUST_NEGATIVE):
            fraud += 0.7
        if no_destination:
            fraud += 0.15
        fraud += 0.1 * min(3, bad)
        if ratio is not None and ratio > 5:
            fraud += 0.2
        fraud = round(min(fraud, 0.98), 4)

        matches = 0.2 if "vague" in text else 0.85 if len(text) >= 60 else 0.45

        answers: dict = {}
        for question_id, spec in questions.items():
            if spec.type == "noul":
                value = {
                    "fraud_indicators": fraud,
                    "description_matches_category": matches,
                }.get(question_id, 0.5)
                answers[question_id] = {"type": "noul", "noul": value}
            elif spec.type == "choice":
                # The option names below are the configured ones; the
                # `in options` guards keep the mock honest if someone renames
                # them (it then falls through instead of inventing one).
                options = list(spec.criteria)
                if fraud >= 0.5 and "risco_fraude" in options:
                    pick = "risco_fraude"
                elif any(k in text for k in ("monthly", "subscription", "recurring")) and "assinatura_recorrente" in options:
                    pick = "assinatura_recorrente"
                elif ratio is not None and ratio > 3 and "transferencia_atipica" in options:
                    pick = "transferencia_atipica"
                else:
                    pick = "compra_padrao" if "compra_padrao" in options else options[0]
                rest = 0.3 / (len(options) - 1)
                probabilities = {o: round(0.7 if o == pick else rest, 4) for o in options}
                answers[question_id] = {
                    "type": "choice",
                    "choice": pick,
                    "confidence": _concentration(list(probabilities.values())),
                    "probabilities": probabilities,
                }
            else:  # score
                levels = len(spec.criteria)
                position = (levels - 1) * (0.5 if attempts == 0 else min(1.0, 2 * bad / attempts))
                weights = [math.exp(-((i - position) ** 2)) for i in range(levels)]
                total = sum(weights)
                probabilities = [round(w / total, 4) for w in weights]
                answers[question_id] = {
                    "type": "score",
                    "score": round(position, 4),
                    "confidence": _concentration(probabilities),
                    "probabilities": {str(i): p for i, p in enumerate(probabilities)},
                    "legend": {str(i): label for i, label in enumerate(spec.criteria)},
                }
        return answers

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
