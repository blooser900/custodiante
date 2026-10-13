"""Tests for typed answers, question configuration, and the shared engine core."""

import copy
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError

from src.config import (
    DEFAULT_ASSESSMENT_QUESTIONS,
    QuestionSpec,
    RulesConfig,
    ScoringCriteria,
    Thresholds,
    load_config,
    save_config,
)
from src.decision_engine.answers import AnswerError, ChoiceAnswer, NoulAnswer, ScoreAnswer, parse_answer
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.jev_client import JevDecisionEngine
from src.history import compute_history
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds

NOUL = QuestionSpec(type="noul", instructions="Is it fine?")
CHOICE = QuestionSpec(type="choice", instructions="Which?", criteria={"a": "first", "b": "second"})
SCORE = QuestionSpec(type="score", instructions="How much?", criteria=["low", "mid", "high"])


# --------------------------------------------------------------------------
# Typed answers
# --------------------------------------------------------------------------


def test_a_noul_answer_is_a_probability_of_yes_with_no_confidence():
    answer = parse_answer("q", NOUL, {"type": "noul", "noul": 0.7864})
    assert isinstance(answer, NoulAnswer) and answer.noul == 0.7864
    assert not hasattr(answer, "confidence") and not hasattr(answer, "probabilities")


def test_a_choice_answer_keeps_the_probabilities():
    raw = {"type": "choice", "choice": "a", "confidence": 0.29, "probabilities": {"a": 0.47, "b": 0.53}}
    answer = parse_answer("q", CHOICE, raw)
    assert isinstance(answer, ChoiceAnswer) and answer.probabilities == {"a": 0.47, "b": 0.53}


def test_a_score_answer_keeps_raw_position_legend_and_probabilities():
    raw = {"type": "score", "score": 0.4664, "legend": {"0": "low", "1": "mid", "2": "high"},
           "probabilities": {"0": 0.62, "1": 0.29, "2": 0.09}, "confidence": 0.3}
    answer = parse_answer("q", SCORE, raw)
    assert isinstance(answer, ScoreAnswer) and answer.score == 0.4664 and answer.legend["0"] == "low"


def test_an_answer_that_omits_its_type_is_accepted_because_the_question_knows_it():
    assert parse_answer("q", NOUL, {"noul": 0.3}).noul == 0.3


def test_an_answer_of_the_wrong_declared_type_is_rejected():
    with pytest.raises(AnswerError, match="expected a noul answer, got score"):
        parse_answer("q", NOUL, {"type": "score", "score": 1})


@pytest.mark.parametrize(
    ("spec", "raw", "message"),
    [
        (NOUL, None, "no answer"),
        (NOUL, {"noul": 1.7}, "malformed noul"),
        (NOUL, {"noul": -0.1}, "malformed noul"),
        (NOUL, {}, "malformed noul"),
        (CHOICE, {"choice": "a", "probabilities": {"a": 1.0}}, "malformed choice"),  # no confidence
        (CHOICE, {"choice": "zzz", "confidence": 0.5, "probabilities": {"zzz": 1.0}}, "not one of the offered"),
        (SCORE, {"score": 1.0, "confidence": 2.0, "probabilities": {}}, "malformed score"),
    ],
)
def test_bad_answers_are_rejected_with_the_question_named(spec, raw, message):
    with pytest.raises(AnswerError, match=message) as e:
        parse_answer("my_question", spec, raw)
    assert "my_question" in str(e.value)


# --------------------------------------------------------------------------
# Question configuration
# --------------------------------------------------------------------------


def test_the_default_questions_are_valid_and_leave_room_under_the_api_limit():
    config = RulesConfig(
        category_limits=category_limits(), thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
    )
    assert set(config.assessment_questions) == set(DEFAULT_ASSESSMENT_QUESTIONS)
    assert len(config.assessment_questions) + 3 <= 10


def test_default_questions_follow_the_one_factor_per_question_advice():
    """Each configured extra asks one thing, in terms of situations, not degrees.

    Checked against every configured question rather than a hardcoded id, so the
    test survives the question set being reshaped for calibration.
    """
    for question_id, raw in DEFAULT_ASSESSMENT_QUESTIONS.items():
        spec = load_config.__globals__["QuestionSpec"].model_validate(raw)
        if spec.type == "choice":
            assert "other" in spec.criteria or len(spec.criteria) >= 4, question_id
        if spec.type == "score":
            # Degrees ("low/medium/high") give the model nothing to anchor on;
            # a description of a situation does.
            assert all(len(text.split()) >= 6 for text in spec.criteria), question_id


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (dict(type="noul", instructions="x", criteria=["a"]), "takes no criteria"),
        (dict(type="choice", instructions="x", criteria={"only": "one"}), "2\\+ options"),
        (dict(type="choice", instructions="x", criteria=["a", "b"]), "2\\+ options"),
        (dict(type="choice", instructions="x"), "needs criteria"),
        (dict(type="score", instructions="x", criteria=["one"]), "2 to 10"),
        (dict(type="score", instructions="x", criteria=[str(i) for i in range(11)]), "2 to 10"),
        (dict(type="score", instructions="x", criteria={"a": "b"}), "2 to 10"),
        (dict(type="noul", instructions=""), "at least 1"),
        (dict(type="free_text", instructions="x"), "Input should be"),
    ],
)
def test_malformed_question_specs_fail_at_load_time_not_at_the_api(raw, message):
    with pytest.raises(ValidationError, match=message):
        QuestionSpec.model_validate(raw)


def base_config(**extra) -> dict:
    return dict(
        category_limits={k: v.model_dump() for k, v in category_limits().items()},
        thresholds=thresholds().model_dump(),
        criteria=dict(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]), **extra,
    )


@pytest.mark.parametrize("bad_id", ["trust", "urgency", "importance", "Has Caps", "1st", "with-dash", ""])
def test_question_ids_cannot_shadow_core_questions_or_be_malformed(bad_id):
    with pytest.raises(ValidationError):
        RulesConfig.model_validate(base_config(assessment_questions={bad_id: dict(type="noul", instructions="x")}))


def test_more_questions_than_the_api_accepts_are_refused_up_front():
    seven = {f"q{i}": dict(type="noul", instructions="x") for i in range(7)}
    RulesConfig.model_validate(base_config(assessment_questions=seven))  # 3 core + 7 = 10: ok
    with pytest.raises(ValidationError, match="at most 7 extra"):
        RulesConfig.model_validate(base_config(assessment_questions={**seven, "q7": dict(type="noul", instructions="x")}))


def test_an_older_config_file_without_the_section_gets_the_defaults(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(base_config()))
    assert set(load_config(path).assessment_questions) == set(DEFAULT_ASSESSMENT_QUESTIONS)


def test_an_empty_section_turns_the_extras_off(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(base_config(assessment_questions={})))
    assert load_config(path).assessment_questions == {}


def test_questions_survive_a_save_and_load_round_trip(tmp_path):
    path = tmp_path / "rules.yaml"
    config = RulesConfig.model_validate(base_config(assessment_questions={"my_q": dict(type="choice", instructions="x", criteria={"a": "1", "b": "2"})}))
    save_config(config, path)
    assert load_config(path) == config


# --------------------------------------------------------------------------
# The shared engine core, exercised through both backends
# --------------------------------------------------------------------------


def rules(**extra) -> RulesConfig:
    return RulesConfig.model_validate(base_config(**extra))


def request(wallet="0x" + "b" * 40) -> PaymentRequest:
    return PaymentRequest(request_id="r", category=RequestCategory.OPERATIONAL_EXPENSE, amount_usdc=450.0,
                          requester="Setor de TI", description="Compra de insumos", counterparty_wallet=wallet)


def core_answers() -> dict:
    return {k: {"type": "score", "score": 1.0, "confidence": 0.8, "probabilities": {"0": .1, "1": .8}}
            for k in ("trust", "urgency", "importance")}


def default_extra_answers() -> dict:
    return {
        "natureza_transacao": {"type": "choice", "choice": "compra_padrao", "confidence": 0.29,
                               "probabilities": {"compra_padrao": .47, "assinatura_recorrente": .09,
                                                 "transferencia_atipica": .27, "risco_fraude": .17}},
        "fraud_indicators": {"type": "noul", "noul": 0.05},
        "description_matches_category": {"type": "noul", "noul": 0.9},
    }


def run(engine_cls, answers, config=None, history=None, extra_body=None):
    response = MagicMock(status_code=200)
    response.json.return_value = {"model": "jev-1.13.0", "answers": answers, "latency_ms": 224.0, **(extra_body or {})}
    with patch("src.decision_engine.system_one.load_config", return_value=config or rules()), \
         patch("src.decision_engine._http.httpx.post", return_value=response) as post:
        scores = engine_cls(api_key="k").score(request(), history=history)
    return scores, post.call_args.kwargs["json"]


BACKENDS = [JevDecisionEngine, TypeSafeCompatibleEngine]


@pytest.mark.parametrize("engine_cls", BACKENDS)
def test_one_request_carries_the_core_and_the_extra_questions(engine_cls):
    _, payload = run(engine_cls, {**core_answers(), **default_extra_answers()})

    assert set(payload["questions"]) == {"trust", "urgency", "importance", *DEFAULT_ASSESSMENT_QUESTIONS}
    assert payload["questions"]["fraud_indicators"] == {"type": "noul", "instructions": DEFAULT_ASSESSMENT_QUESTIONS["fraud_indicators"]["instructions"]}
    assert "criteria" not in payload["questions"]["fraud_indicators"]  # noul takes none
    assert isinstance(payload["questions"]["natureza_transacao"]["criteria"], dict)
    assert isinstance(payload["questions"]["trust"]["criteria"], list)


@pytest.mark.parametrize("engine_cls", BACKENDS)
def test_extras_are_parsed_into_typed_answers_and_core_scores_are_unchanged(engine_cls):
    scores, _ = run(engine_cls, {**core_answers(), **default_extra_answers()})

    # The test rubric has 2 levels, so a raw 1.0 on the 0..1 scale normalizes to 1.0.
    assert scores.trust.value == 1.0 and scores.trust.confidence == 0.8
    assert scores.extra["fraud_indicators"].noul == 0.05
    assert scores.extra["natureza_transacao"].choice == "compra_padrao"
    assert scores.extra["description_matches_category"].noul == 0.9


@pytest.mark.parametrize("engine_cls", BACKENDS)
def test_the_evidence_records_what_the_model_saw_and_said(engine_cls):
    history = compute_history(request(), [], None)
    scores, payload = run(engine_cls, {**core_answers(), **default_extra_answers()}, history=history)

    assert scores.evidence.state == payload["state"]  # exactly what was sent
    assert scores.evidence.state["history"]["destination"]["identity_basis"] == "wallet_address"
    assert scores.evidence.answers["natureza_transacao"]["probabilities"]["risco_fraude"] == 0.17  # raw, probabilities kept
    assert scores.evidence.model == "jev-1.13.0" and scores.evidence.latency_ms == 224.0
    assert scores.evidence.problems == []


def test_without_history_the_state_is_just_the_request():
    _, payload = run(JevDecisionEngine, {**core_answers(), **default_extra_answers()})
    assert "history" not in payload["state"]


def test_the_model_name_falls_back_to_the_one_we_asked_for():
    response = MagicMock(status_code=200)
    response.json.return_value = {"answers": core_answers()}
    with patch("src.decision_engine.system_one.load_config", return_value=rules(assessment_questions={})), \
         patch("src.decision_engine._http.httpx.post", return_value=response):
        scores = JevDecisionEngine(api_key="k", model="jev-1.13.0").score(request())
    assert scores.evidence.model == "jev-1.13.0"


@pytest.mark.parametrize("engine_cls", BACKENDS)
def test_a_missing_or_malformed_extra_never_blocks_the_decision(engine_cls):
    answers = {**core_answers(), **default_extra_answers()}
    del answers["fraud_indicators"]  # the engine skipped one
    answers["description_matches_category"] = {"type": "noul", "noul": 1.7}  # and garbled another

    scores, _ = run(engine_cls, answers)

    assert scores.trust.value == 1.0 and scores.trust.confidence == 0.8  # the core decision input is intact
    # The one well-formed extra survives; the other two are dropped, not fatal.
    assert set(scores.extra) == {"natureza_transacao"}
    problems = " | ".join(scores.evidence.problems)
    assert "fraud_indicators" in problems
    assert "description_matches_category" in problems


@pytest.mark.parametrize("missing", ["trust", "urgency", "importance"])
def test_a_missing_core_answer_is_an_error_because_the_policy_cannot_decide_without_it(missing):
    answers = {**core_answers(), **default_extra_answers()}
    del answers[missing]
    with pytest.raises(RuntimeError, match=f"core question '{missing}'"):
        run(JevDecisionEngine, answers)


def test_a_malformed_core_answer_is_an_error_too():
    answers = {**core_answers(), **default_extra_answers()}
    answers["trust"] = {"type": "score", "confidence": 0.5}  # no score
    with pytest.raises(RuntimeError, match="trust"):
        run(JevDecisionEngine, answers)


def test_with_extras_switched_off_only_the_core_questions_are_asked():
    scores, payload = run(JevDecisionEngine, core_answers(), config=rules(assessment_questions={}))
    assert set(payload["questions"]) == {"trust", "urgency", "importance"} and scores.extra == {}


def test_the_exact_response_seen_in_manual_testing_parses_with_its_own_question_ids():
    """The payload and response shapes below are the ones captured against
    Kev while exploring the new design: Portuguese ids, all three types."""
    questions = {
        "natureza_transacao": dict(type="choice", instructions="Qual é a natureza desta transação?", criteria={
            "compra_padrao": "Transação comum.", "assinatura_recorrente": "Serviço contínuo.",
            "transferencia_atipica": "Movimentação incomum.", "risco_fraude": "Forte indício de fraude."}),
        "risco_historico": dict(type="score", instructions="Qual o risco pelo histórico?",
                                criteria=["Baixo risco.", "Risco moderado.", "Alto risco."]),
        "aprovacao_segura": dict(type="noul", instructions="É seguro aprovar autonomamente?"),
    }
    seen = {
        "natureza_transacao": {"type": "choice", "choice": "compra_padrao", "confidence": 0.2948,
                               "probabilities": {"compra_padrao": 0.4711, "assinatura_recorrente": 0.0874, "transferencia_atipica": 0.2721, "risco_fraude": 0.1694}},
        "risco_historico": {"type": "score", "score": 0.4664, "legend": {"0": "baixo_risco", "1": "risco_moderado", "2": "alto_risco"},
                            "probabilities": {"0": 0.6232, "1": 0.2871, "2": 0.0897}, "confidence": 0.3004},
        "aprovacao_segura": {"type": "noul", "noul": 0.7864},
    }
    scores, payload = run(TypeSafeCompatibleEngine, {**core_answers(), **seen}, config=rules(assessment_questions=questions),
                          extra_body={"model": "kev-latest", "usage": {"input_tokens": 327, "output_tokens": 225}})

    assert scores.evidence.problems == []
    assert scores.extra["aprovacao_segura"].noul == 0.7864
    assert scores.extra["natureza_transacao"].confidence == 0.2948  # low confidence, plausible answer
    assert scores.extra["risco_historico"].probabilities["0"] == 0.6232
    assert scores.evidence.model == "kev-latest"
    assert payload["questions"]["aprovacao_segura"] == {"type": "noul", "instructions": "É seguro aprovar autonomamente?"}


def test_the_defaults_are_not_mutated_by_building_a_config():
    before = copy.deepcopy(DEFAULT_ASSESSMENT_QUESTIONS)
    rules().assessment_questions["fraud_indicators"].instructions  # touch
    RulesConfig.model_validate(base_config(assessment_questions={"x": dict(type="noul", instructions="y")}))
    assert DEFAULT_ASSESSMENT_QUESTIONS == before
