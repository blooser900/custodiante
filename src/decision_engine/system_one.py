"""Shared client for any server speaking the System One protocol.

The real Jev API and a self-hosted Kev server accept the same request and
return the same answer shapes, so one implementation serves both. The
subclasses (jev_client.py, fallback_classifier.py) only supply where to call,
which credentials and model to use, and what to tell a person when it fails.

One request carries every question: the three core Score questions the
policy reads (trust, urgency, importance) plus the configured extra
questions (noul / choice / score). Extra answers that come back missing or
malformed are left out and listed in `evidence.problems`; they never block a
payment, because nothing decides on them yet. A missing or malformed CORE
answer is an error: the policy cannot decide without it.
"""

import os

import httpx

from src.config import load_config
from src.decision_engine._http import post_with_retry
from src.decision_engine.answers import AnswerError, parse_answer
from src.decision_engine.criteria import build_questions, build_state, normalize_score
from src.decision_engine.engine import DecisionEngine, DecisionScores, Evidence, ScoreResult
from src.history import HistoryFeatures
from src.requests.schema import PaymentRequest


DEFAULT_TIMEOUT_SECONDS = 15.0
_NO_TIMEOUT = ("none", "off", "no", "0")

# Sentinel: "read ENGINE_TIMEOUT_SECONDS". Distinct from None, which means
# "no timeout at all".
FROM_ENV = object()


def timeout_from_env() -> float | None:
    """The engine's HTTP timeout in seconds, from ENGINE_TIMEOUT_SECONDS.

    Unset gives 15 s, fine for a hosted API. A self-hosted model on a CPU can
    be much slower, so "none" (or "off", "no", "0") disables the timeout.
    """
    raw = os.environ.get("ENGINE_TIMEOUT_SECONDS", "").strip().lower()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    if raw in _NO_TIMEOUT:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        raise RuntimeError(
            f"ENGINE_TIMEOUT_SECONDS must be a number of seconds or 'none', got {raw!r}."
        ) from None
    if seconds <= 0:
        raise RuntimeError("ENGINE_TIMEOUT_SECONDS must be positive (or 'none' for no timeout).")
    return seconds


class SystemOneEngine(DecisionEngine):
    api_url: str
    api_key: str
    model: str
    timeout: float | None  # seconds to wait for an answer; None waits forever

    def __init__(self, api_url: str, api_key: str, model: str, timeout=FROM_ENV) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout_from_env() if timeout is FROM_ENV else timeout

    # -- what differs between backends ----------------------------------

    def _missing_key_message(self) -> str:
        raise NotImplementedError

    def _unreachable_message(self, error: Exception) -> str:
        raise NotImplementedError

    # -- the shared protocol ------------------------------------------------

    def score(
        self, request: PaymentRequest, history: HistoryFeatures | None = None
    ) -> DecisionScores:
        if not self.api_key:
            # Fail with a clear message here, rather than letting httpx reject
            # an "Authorization: Bearer " header with an empty token as a raw
            # protocol error further down.
            raise RuntimeError(self._missing_key_message())

        config = load_config()
        criteria = config.criteria
        state = build_state(request, history)
        payload = {
            "model": self.model,
            "state": state,
            "questions": build_questions(criteria, config.assessment_questions),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = post_with_retry(self.api_url, payload, headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(self._unreachable_message(e)) from e

        body = response.json()
        answers = body["answers"]

        def core(question_id: str, levels: list[str]) -> ScoreResult:
            try:
                answer = answers[question_id]
                return ScoreResult(
                    value=normalize_score(answer["score"], len(levels)),
                    confidence=answer["confidence"],
                )
            except (KeyError, TypeError) as e:
                raise RuntimeError(
                    f"The engine's answer for the core question {question_id!r} is missing or malformed."
                ) from e

        extra, problems = {}, []
        for question_id, spec in config.assessment_questions.items():
            try:
                extra[question_id] = parse_answer(question_id, spec, answers.get(question_id))
            except AnswerError as e:
                problems.append(str(e))

        return DecisionScores(
            trust=core("trust", criteria.trust),
            urgency=core("urgency", criteria.urgency),
            importance=core("importance", criteria.importance),
            extra=extra,
            evidence=Evidence(
                model=body.get("model", self.model),
                state=state,
                answers=answers,
                latency_ms=body.get("latency_ms"),
                problems=problems,
            ),
        )
