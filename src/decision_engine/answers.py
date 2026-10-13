"""Typed answers from a System One engine (Jev, Kev): noul, choice, score.

Each question type has its own answer shape:

    noul    {"noul": 0.79}                     probability of "yes". There is
            no confidence or probabilities: 0.5 is an even split between yes
            and no, not "medium". For a spectrum, use a score question.
    choice  {"choice": "x", "confidence", "probabilities": {...}}
    score   {"score": 1.15, "confidence", "probabilities": {...}, "legend": {...}}

For choice and score, `confidence` reflects how concentrated the probability
distribution is, not whether the answer is correct: a spread-out distribution
over several plausible options gives a low confidence even when the top
option is right. `probabilities` says what the model was torn between, and is
the better thing to threshold on.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, ValidationError

from src.config import QuestionSpec


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    noul: float = Field(ge=0, le=1)


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float]


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float = Field(ge=0)  # raw: position on the 0..(levels-1) scale
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float]
    legend: dict[str, str] | None = None


Answer = Annotated[Union[NoulAnswer, ChoiceAnswer, ScoreAnswer], Field(discriminator="type")]

_BY_TYPE = {"noul": NoulAnswer, "choice": ChoiceAnswer, "score": ScoreAnswer}


class AnswerError(ValueError):
    """An engine answer was missing, of the wrong type, or malformed."""


def parse_answer(question_id: str, spec: QuestionSpec, raw: dict | None) -> NoulAnswer | ChoiceAnswer | ScoreAnswer:
    """Validate one raw answer against the question that was asked.

    The question's type is the source of truth: an answer that declares a
    different type is rejected, and one that declares none is accepted.
    A choice answer must pick one of the options we offered.
    """
    if raw is None:
        raise AnswerError(f"{question_id}: the engine returned no answer")
    declared = raw.get("type")
    if declared is not None and declared != spec.type:
        raise AnswerError(f"{question_id}: expected a {spec.type} answer, got {declared}")
    try:
        answer = _BY_TYPE[spec.type].model_validate({**raw, "type": spec.type})
    except ValidationError as e:
        raise AnswerError(f"{question_id}: malformed {spec.type} answer ({e.errors()[0]['msg']})") from e
    if isinstance(answer, ChoiceAnswer) and answer.choice not in spec.criteria:
        raise AnswerError(f"{question_id}: {answer.choice!r} is not one of the offered options")
    return answer
