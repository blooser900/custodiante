"""Tests for the sample-collection script."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from scripts import collect_samples as cs
from src.config import load_config
from src.decision_engine.fallback_classifier import TypeSafeCompatibleEngine
from src.decision_engine.mock_engine import MockDecisionEngine
from src.history import reliability_index
from tests.fake_engine_server import fake_engine


def read(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


@pytest.fixture
def out(tmp_path):
    return tmp_path / "samples.jsonl"


@pytest.fixture
def mock_engine():
    with patch("scripts.collect_samples.build_engine_from_env", return_value=MockDecisionEngine()):
        yield


# --------------------------------------------------------------------------
# The scenarios themselves
# --------------------------------------------------------------------------


def test_there_are_enough_scenarios_in_each_of_the_three_groups():
    groups = {g: [s for s in cs.SCENARIOS if s.expected == g] for g in cs.SCENARIO_GROUPS}
    assert len(cs.SCENARIOS) >= 10
    assert all(len(members) >= 3 for members in groups.values())
    assert {s.expected for s in cs.SCENARIOS} == set(groups)


def test_the_discordant_group_covers_both_directions():
    """The veto is calibrated here, so both costs must be represented.

    Two scenarios where trust is high but a fraud signal is present (the veto
    has to fire), and two legitimate-looking ones (the veto must not fire, which
    is the false-positive cost). Without both directions a threshold can only be
    fitted to one kind of error.
    """
    discordant = [s for s in cs.SCENARIOS if s.expected == "discordant"]
    assert len(discordant) >= 4, "the veto needs both directions represented"

    # A requester with a real, clean record is what makes the trust score high
    # in the fraud-leaning pair -- otherwise trust and fraud would agree and the
    # scenario would be just another correlated one.
    well_recorded = [s for s in discordant
                     if s.requester_history.succeeded >= 10 and s.amount_usdc >= 1000]
    assert len(well_recorded) >= 2, "need high-trust requests carrying a fraud signal"

    # And the mirror: legitimate requests whose only problem is a thin record.
    thin_but_ordinary = [s for s in discordant if s.amount_usdc <= 250]
    assert len(thin_but_ordinary) >= 2, "need legitimate requests that merely look flimsy"

    # The fraud-leaning pair must point somewhere we have never paid before or
    # that has a blemish; that is the signal the model has to weigh.
    for s in well_recorded:
        dest = s.destination_history
        assert dest is not None
        assert not dest.known or dest.failed > 0, s.id


def test_scenario_ids_are_unique():
    ids = [s.id for s in cs.SCENARIOS]
    assert len(ids) == len(set(ids))


def test_all_content_is_in_english():
    """Instructions are in English, so the content must be too (ASCII is a cheap
    guard against accented Portuguese slipping in)."""
    for s in cs.SCENARIOS:
        for text in (s.id, s.requester_name, s.description):
            assert text.isascii(), f"{s.id}: non-ASCII text {text!r}"


@pytest.mark.parametrize("scenario", cs.SCENARIOS, ids=lambda s: s.id)
def test_every_scenario_builds_a_valid_request_and_a_consistent_history(scenario):
    index = cs.SCENARIOS.index(scenario)
    request = cs.to_request(index, scenario)
    assert request.counterparty_wallet == (None if scenario.destination_history is None else f"0x{index + 1:040x}")

    for party in (scenario.requester_history, scenario.destination_history):
        if party is None:
            continue
        assert party.attempts == party.succeeded + party.failed + party.refunded + party.denied
        assert party.reliability_index == reliability_index(party.succeeded, party.attempts)
        assert party.known == (party.attempts + party.pending > 0)
        if not party.known:
            assert party.first_seen_days is None or party.first_seen_days == 0


def test_wallets_are_distinct_so_destinations_never_share_a_history():
    wallets = [cs.to_request(i, s).counterparty_wallet for i, s in enumerate(cs.SCENARIOS)]
    named = [w for w in wallets if w]
    assert len(named) == len(set(named))


def test_the_designed_groups_really_differ_in_their_records():
    """Sanity check on my own test data: bad destinations have worse records."""
    def mean_dest_index(group):
        idx = [s.destination_history.reliability_index for s in cs.SCENARIOS
               if s.expected == group and s.destination_history is not None and s.destination_history.known]
        return sum(idx) / len(idx)

    assert mean_dest_index("good") > mean_dest_index("doubtful") > mean_dest_index("bad")


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def test_a_run_writes_a_header_then_one_result_per_scenario(out, mock_engine):
    assert cs.main(["--out", str(out)]) == 0

    records = read(out)
    header, results = records[0], records[1:]
    assert header["kind"] == "header" and header["timeout"] is None and header["engine"] == "mock"
    assert set(header["questions"]) >= {"trust", "urgency", "importance", "natureza_transacao"}
    assert [r["id"] for r in results] == [s.id for s in cs.SCENARIOS]
    assert all(r["kind"] == "result" for r in results)


def test_each_result_holds_exactly_what_the_engine_was_shown_and_said(out, mock_engine):
    cs.main(["--out", str(out), "--only", "bad_troubled_destination"])
    result = read(out)[1]

    assert result["state"]["history"]["destination"]["failed"] == 3  # the history was in the state
    assert result["state"]["amount_usdc"] == 400.0 and "gift cards" in result["state"]["description"]
    assert set(result["answers"]) == set(read(out)[0]["questions"])  # every question answered, raw
    assert result["expected"] == "bad" and result["problems"] == []


def test_a_scenario_without_a_wallet_has_no_destination_history(out, mock_engine):
    cs.main(["--out", str(out), "--only", "bad_no_destination"])
    assert read(out)[1]["state"]["history"]["destination"] is None


def test_only_selects_scenarios_and_rejects_unknown_ids(out, mock_engine):
    cs.main(["--out", str(out), "--only", "good_routine_supplies,bad_denied_before"])
    assert [r["id"] for r in read(out)[1:]] == ["good_routine_supplies", "bad_denied_before"]

    with pytest.raises(SystemExit):
        cs.main(["--out", str(out), "--only", "not_a_scenario"])


def test_repeat_runs_each_scenario_that_many_times(out, mock_engine, capsys):
    cs.main(["--out", str(out), "--only", "good_routine_supplies", "--repeat", "3"])
    results = read(out)[1:]
    assert [r["run"] for r in results] == [1, 2, 3]
    assert "Run-to-run noise" in capsys.readouterr().out


def test_repeat_must_be_positive(out):
    with pytest.raises(SystemExit):
        cs.main(["--out", str(out), "--repeat", "0"])


def test_the_summary_groups_answers_by_what_the_scenario_was_designed_to_be(out, mock_engine, capsys):
    cs.main(["--out", str(out)])
    text = capsys.readouterr().out
    assert "Mean P(yes) by what the scenario was designed to be" in text
    assert all(group in text for group in ("good", "doubtful", "bad"))
    assert "Send me that file." in text


def test_the_summary_shows_the_discordant_scenarios_one_by_one(out, mock_engine, capsys):
    """A group mean would hide the whole point of these scenarios.

    They are the ones where trust and the fraud signal disagree, so they are
    printed individually next to the trust score, and the column says whether
    the live threshold would veto each one.
    """
    cs.main(["--out", str(out)])
    text = capsys.readouterr().out
    assert "Discordant scenarios" in text
    assert "would the veto block it?" in text
    # Every discordant scenario must appear by name, not just in a mean.
    for scenario in cs.SCENARIOS:
        if scenario.expected == "discordant":
            assert scenario.id in text


def test_the_mock_engine_is_flagged_as_not_a_real_sample(out, mock_engine, capsys):
    cs.main(["--out", str(out), "--only", "good_routine_supplies"])
    assert "not real samples" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Failure and interruption
# --------------------------------------------------------------------------


def flaky_engine(fail_on: set[int], exception=RuntimeError("engine hiccup")):
    """A mock whose Nth call (1-based) raises."""
    engine = MockDecisionEngine()
    real = engine.score
    calls = {"n": 0}

    def score(request, history=None):
        calls["n"] += 1
        if calls["n"] in fail_on:
            raise exception
        return real(request, history=history)

    engine.score = score
    return engine


def test_one_failed_scenario_is_recorded_and_the_rest_still_run(out):
    with patch("scripts.collect_samples.build_engine_from_env", return_value=flaky_engine({2})):
        assert cs.main(["--out", str(out)]) == 0

    records = read(out)[1:]
    assert [r["kind"] for r in records].count("error") == 1
    assert records[1]["kind"] == "error" and "engine hiccup" in records[1]["error"]
    assert len([r for r in records if r["kind"] == "result"]) == len(cs.SCENARIOS) - 1


def test_it_stops_after_several_failures_in_a_row_instead_of_grinding_on(out, capsys):
    with patch("scripts.collect_samples.build_engine_from_env", return_value=flaky_engine(set(range(1, 100)))):
        assert cs.main(["--out", str(out)]) == 1

    assert len(read(out)) == 1 + cs.MAX_CONSECUTIVE_ERRORS  # header + 3 errors, then it stopped
    assert "Is the engine running?" in capsys.readouterr().out


def test_ctrl_c_keeps_everything_already_collected(out, capsys):
    with patch("scripts.collect_samples.build_engine_from_env", return_value=flaky_engine({4}, KeyboardInterrupt())):
        assert cs.main(["--out", str(out)]) == 130

    results = [r for r in read(out) if r["kind"] == "result"]
    assert [r["id"] for r in results] == [s.id for s in cs.SCENARIOS[:3]]  # the three that finished
    assert "3 answer(s) were already saved" in capsys.readouterr().out


def test_each_answer_is_on_disk_before_the_next_request_is_made(out):
    """The file must be readable mid-run, as a killed process would leave it."""
    seen = []
    engine = MockDecisionEngine()
    real = engine.score

    def score(request, history=None):
        seen.append(len(read(out)))  # lines on disk at the moment the next call starts
        return real(request, history=history)

    engine.score = score
    with patch("scripts.collect_samples.build_engine_from_env", return_value=engine):
        cs.main(["--out", str(out), "--only", "good_routine_supplies,good_small_known_vendor"])

    assert seen == [1, 2]  # header; then header + the first result


# --------------------------------------------------------------------------
# No timeout, over real HTTP
# --------------------------------------------------------------------------


def test_the_script_switches_the_engine_timeout_off(out):
    engine = MockDecisionEngine()
    engine.timeout = 15.0
    with patch("scripts.collect_samples.build_engine_from_env", return_value=engine):
        cs.main(["--out", str(out), "--only", "good_routine_supplies"])
    assert engine.timeout is None


def test_it_waits_for_a_slow_engine_even_when_the_app_timeout_would_have_given_up(out, monkeypatch):
    """A model that takes 1.5 s while the configured timeout is 0.3 s: the
    app's engine would fail, but the script must not."""
    monkeypatch.setenv("ENGINE_TIMEOUT_SECONDS", "0.3")
    with fake_engine(delay_seconds=1.5) as (url, received):
        with patch("scripts.collect_samples.build_engine_from_env", side_effect=lambda: TypeSafeCompatibleEngine(api_url=url)):
            assert TypeSafeCompatibleEngine(api_url=url).timeout == 0.3  # the app's own engine WOULD time out
            code = cs.main(["--out", str(out), "--only", "good_routine_supplies"])

    assert code == 0
    result = read(out)[1]
    assert result["kind"] == "result" and result["seconds"] >= 1.5
    assert result["model"] == "fake-1"
    assert received[0]["state"]["history"]["destination"]["succeeded"] == 24  # real payload reached the server
    assert set(received[0]["questions"]) == set(read(out)[0]["questions"])
