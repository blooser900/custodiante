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

That error is `EngineResponseError`, raised for a response that is not JSON,
has no usable `answers` mapping, or carries a core score/confidence that is
missing, non-numeric or outside its range. It is a `RuntimeError`, so callers
that already handled an unreachable engine keep working, and it is also the
signal `src/pipeline.py` uses to leave a trace of the failed attempt in the
audit log instead of losing it.
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


class EngineResponseError(RuntimeError):
    """The engine answered, and its answer cannot be used.

    A `RuntimeError` on purpose: an unusable answer and an unreachable engine
    are the same thing to every caller up the stack (no decision can be made),
    and the CLI's error path already handles `RuntimeError`. What this type adds
    is that the failure is a *typed, explained* one instead of a bare `KeyError`
    from a dict lookup or a Pydantic `ValidationError` from deep inside this
    layer, and that `run_pipeline` can recognise it and leave a trace in the
    audit log.

    It is raised for a response that is not JSON, has no usable `answers`
    mapping, or carries a core answer whose score/confidence is missing or
    outside its range. A malformed EXTRA answer is not an error: those never
    decide anything, so they are dropped into `evidence.problems`.
    """


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

        try:
            body = response.json()
        except ValueError as e:  # includes json.JSONDecodeError
            raise EngineResponseError(
                f"The engine's response at {self.api_url} is not JSON ({type(e).__name__})."
            ) from e

        if not isinstance(body, dict):
            raise EngineResponseError(
                f"The engine's response at {self.api_url} is a {type(body).__name__}, not an object."
            )
        answers = body.get("answers")
        if not isinstance(answers, dict):
            # A bare `body["answers"]` here raised `KeyError: 'answers'`, which
            # says nothing about why the answer is unusable.
            raise EngineResponseError(
                f"The engine's response has no usable 'answers' mapping (got "
                f"{type(answers).__name__}). Nothing can be decided from it."
            )

        def core(question_id: str, levels: list[str]) -> ScoreResult:
            """One core score, validated against the contract before it is used.

            The engine cannot decide trust/urgency/importance outside their
            ranges, so an answer that does is not a small problem to clamp: it
            means the response is not something this layer can interpret. It is
            refused rather than rounded, because rounding an out-of-range score
            would invent a decision the engine never made.
            """
            answer = answers.get(question_id)
            try:
                raw_score = answer["score"]
                confidence = answer["confidence"]
            except (KeyError, TypeError) as e:
                raise EngineResponseError(
                    f"The engine's answer for the core question {question_id!r} is missing "
                    f"or malformed (no usable score/confidence)."
                ) from e
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise EngineResponseError(
                    f"The engine's score for the core question {question_id!r} is "
                    f"{raw_score!r}, which is not a number."
                )
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise EngineResponseError(
                    f"The engine's confidence for the core question {question_id!r} is "
                    f"{confidence!r}, which is not a number."
                )
            if not (0 <= raw_score <= len(levels) - 1):
                raise EngineResponseError(
                    f"The engine's score for the core question {question_id!r} is {raw_score}, "
                    f"outside the 0..{len(levels) - 1} range the rubric defines."
                )
            if not (0 <= confidence <= 1):
                raise EngineResponseError(
                    f"The engine's confidence for the core question {question_id!r} is "
                    f"{confidence}, outside 0..1."
                )
            return ScoreResult(
                value=normalize_score(raw_score, len(levels)), confidence=confidence
            )

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
