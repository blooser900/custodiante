"""Regressions for A10-A13: the demo narrative and the small stumbles around it.

These are the findings that do not move money by themselves, but they are what a
reviewer or a judge actually reads and runs. A10 in particular is about telling
the truth on screen: the escalation scenario used to be decided by the FRAUD
veto while every document described it as a trust decision.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import RulesConfig, ScoringCriteria
from src.decision_engine.mock_engine import MockDecisionEngine
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


def rules() -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        require_known_payee=False,
    )


def scenario(name: str) -> dict:
    from demo.seed_scenarios import SCENARIOS

    return next(s for s in SCENARIOS if s["request_id"] == name)


def score_scenario(name: str):
    """Score a demo scenario the way `run_pipeline` does.

    `history` matters: with a destination-less request it sets
    `history.destination = None`, which is the `no_destination` bonus inside the
    mock's fraud score. Scoring without it would measure a different (lower)
    number than the demo ever sees, so the strictest path is the one asserted.
    """
    from src.history import compute_history

    request = PaymentRequest.model_validate(
        {**scenario(name), "request_id": f"{name}-a10"}
    )
    history = compute_history(request, [])
    return request, MockDecisionEngine().score(request, history=history)


# --------------------------------------------------------------------------
# A10 -- the escalation scenario must be decided by the reason we describe
# --------------------------------------------------------------------------


def test_a10_the_escalation_scenario_is_not_decided_by_the_fraud_veto():
    """Measured before this: the mock scored P(fraud)=0.74 -- at or above the
    0.70 veto -- so the dashboard and the audit log explained the escalation as
    "Fraud indicators vetoed automatic approval". Every document (and the
    scenario's own name) said it escalates because the requester has no
    history. The outcome was right and the story told on screen was wrong:
    the veto firing announces suspicion of fraud, which is a much stronger
    claim than "nobody has seen this requester before".
    """
    _, scores = score_scenario("demo-escalate")
    fraud = scores.extra["fraud_indicators"].noul

    assert fraud < 0.7, (
        f"the fraud veto fires at {fraud}, so the demo's escalation reason is the "
        "fraud veto and not the missing history the scenario is built to show"
    )


def test_a10_the_escalation_scenario_still_escalates_for_a_documented_reason():
    """The fix must not change the OUTCOME, only the reason -- and the reason
    must be one the documents actually name.

    Measured after the fix: the scenario escalates because 5000 USDC is above
    the 1000 USDC ceiling for `supply_purchase`, which is checked before any
    payee or trust consideration. That is a documented rule, so it is a fine
    reason to show on screen; what matters is that it is no longer the fraud
    veto, which was a much stronger claim than the scenario can support.
    """
    from src.policy.rules import Decision, apply_policy

    request, scores = score_scenario("demo-escalate")

    decision = apply_policy(request, scores)
    assert decision.decision is Decision.ESCALATE
    assert "Fraud" not in decision.reason, (
        "the escalation must not be explained by the fraud veto any more"
    )
    assert decision.reason, "the escalation needs a reason a reviewer can read"


def test_a10_every_demo_scenario_avoids_the_fraud_veto():
    """None of the three stories is about fraud, so none of them may be decided
    by the fraud veto: the reason on screen would contradict the scenario."""
    for name in ("demo-approve", "demo-reduced", "demo-escalate"):
        _, scores = score_scenario(name)
        fraud = scores.extra["fraud_indicators"].noul
        assert fraud < 0.7, f"{name} lands on the fraud veto with P(fraud)={fraud}"


def test_a10_the_two_approval_scenarios_keep_their_outcomes():
    """demo-approve auto-approves and demo-reduced is capped: the two stories
    the demo opens with must survive the change."""
    from src.policy.rules import Decision, apply_policy

    for name, expected in (("demo-approve", Decision.APPROVE), ("demo-reduced", Decision.APPROVE_REDUCED)):
        request, scores = score_scenario(name)
        assert apply_policy(request, scores).decision is expected, name


def test_a10_the_mock_still_flags_an_explicit_red_flag_as_fraud():
    """A real red flag must still reach the veto: the change must not defang it."""
    request = PaymentRequest(
        request_id="a10-red",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="ops-team",
        description="Suspicious, unknown vendor, vague invoice.",
    )
    scores = MockDecisionEngine().score(request)

    assert scores.extra["fraud_indicators"].noul >= 0.5
    assert scores.extra["natureza_transacao"].choice == "risco_fraude"


def test_a10_the_readme_and_the_scenario_agree_on_the_reason():
    """The docs describe demo-escalate; whatever they claim must be what the
    engine actually does."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    _, scores = score_scenario("demo-escalate")

    if "unknown requester" in readme.lower() or "no prior history" in readme.lower():
        assert scores.trust.value < 0.4, "the docs say low trust; it must be low trust"


# --------------------------------------------------------------------------
# A11 -- the README names the three scores the policy actually uses
# --------------------------------------------------------------------------


def test_a11_the_readme_opens_by_naming_the_score_the_policy_reads():
    """The opening paragraph listed `confidence, urgency, importance`. The policy
    reads `trust, urgency, importance`; `confidence` is a separate attribute of
    each score, and on this engine it is not even consulted (see the note in
    `src/config.py` about the auto-approve rule it used to gate).

    The check is on structure, not on prose: the sentence that lists the scores
    must name the one the code reads and must not present confidence as a fourth
    score. Confidence may -- and does -- appear elsewhere in the README, where it
    is discussed as the attribute it is.
    """
    from src.config import Thresholds

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # The opening paragraph only: everything before the first blank line after
    # the title. (A `split("See [", 1)[0]` looked equivalent and was not -- that
    # marker appears further down, so it silently returned the whole file.)
    paragraphs = [p for p in readme.split("\n\n") if p.strip()]
    opening = "\n".join(paragraphs[:2])
    assert "trust" in opening.lower(), "the opening must name the score the policy reads"
    assert "**confidence**" not in opening.lower(), (
        "the opening still lists confidence as one of the scores"
    )
    # The thresholds the policy actually branches on are all documented.
    for threshold in Thresholds.model_fields:
        assert threshold in readme, f"the README does not mention the {threshold!r} threshold"


# --------------------------------------------------------------------------
# A12 -- the editor opens on the platform the operator is on
# --------------------------------------------------------------------------


def test_a12_the_editor_is_chosen_per_platform(monkeypatch):
    """`os.environ.get("EDITOR", "nano")` opens nano by default, and nano does
    not exist on a stock Windows install -- so `manage_rules.py edit` failed for
    the platform this project is developed on."""
    from scripts import manage_rules

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(manage_rules.os, "name", "nt")
    assert manage_rules._default_editor() == "notepad"

    monkeypatch.setattr(manage_rules.os, "name", "posix")
    assert manage_rules._default_editor() == "nano"

    monkeypatch.setenv("EDITOR", "vim")
    assert (
        manage_rules._default_editor() in {"nano", "notepad"}
    ), "the default must not depend on the ambient $EDITOR"


def test_a12_edit_uses_the_resolved_editor(monkeypatch, tmp_path):
    """The helper must actually be what `edit` runs."""
    from scripts import manage_rules

    called: list[list[str]] = []
    monkeypatch.setattr(manage_rules, "load_config", lambda *a, **k: rules())
    monkeypatch.setattr(manage_rules.subprocess, "run", lambda cmd: called.append(cmd))
    monkeypatch.setattr(manage_rules, "cmd_validate", lambda args: 0)
    monkeypatch.setenv("EDITOR", "my-editor")

    manage_rules.cmd_edit(None)

    assert called and called[0][0] == "my-editor"


# --------------------------------------------------------------------------
# A13 -- a cross-platform demo runner, and unique request ids
# --------------------------------------------------------------------------


def test_a13_the_documents_point_at_a_runner_this_platform_can_execute():
    """`bash demo/run_demo.sh` cannot run on Windows, which is where this project
    is developed and demonstrated. A Python runner works everywhere the app
    itself does."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "demo.run_demo" in readme, "the README must point at the cross-platform runner"
    assert (DEMO / "run_demo.py").exists()

    setup = (ROOT / "scripts" / "setup_arc_wallet.py").read_text(encoding="utf-8")
    assert "demo.run_demo" in setup

    # The bash wrapper stays, but only as a wrapper: if it still listed the steps
    # itself, the two would drift.
    shell = (DEMO / "run_demo.sh").read_text(encoding="utf-8")
    assert "run_demo" in shell


def test_a13_the_new_scenario_ids_cannot_collide_with_one_already_on_disk(capsys, tmp_path, monkeypatch):
    """The suffix was `%m%d-%H%M%S`, so two runs in the same second produced the
    SAME request_ids -- and the pipeline refuses a request_id it has already
    decided, so the second run of the demo died with DuplicateRequestError."""
    from demo import seed_scenarios

    monkeypatch.setattr(seed_scenarios, "SCENARIOS_DIR", tmp_path)
    seed_scenarios.write_scenarios()
    first = {p.name: json.loads(p.read_text(encoding="utf-8"))["request_id"] for p in tmp_path.glob("*.json")}

    # Freeze the clock so the timestamp part is identical on both runs.
    monkeypatch.setattr(seed_scenarios.time, "strftime", lambda _fmt: "0101-000000")
    seed_scenarios.write_scenarios()
    second = {p.name: json.loads(p.read_text(encoding="utf-8"))["request_id"] for p in tmp_path.glob("*.json")}

    assert set(first) == set(second), "the same three scenario files are written"
    for name in first:
        assert first[name] != second[name], (
            f"{name} got the same request_id twice, so the second demo run would be refused"
        )


def test_a13_the_runner_processes_every_scenario(monkeypatch, tmp_path):
    """The runner must do what the shell script did: seed, then run each file.

    The stubs return an explicit int: `list.append` returns None, and a stub
    that returns None makes the runner stop on the first scenario (it compares
    the code against 0), which is correct behaviour and would look like a
    failure of the runner rather than of the test.
    """
    from demo import run_demo

    seen: list[str] = []

    def fake_process(path):
        seen.append(path.name)
        return 0

    monkeypatch.setattr(run_demo, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(run_demo, "seed", lambda: 0)
    monkeypatch.setattr(run_demo, "_process", fake_process)
    monkeypatch.setattr(run_demo, "_report", lambda: 0)
    (tmp_path / "demo-approve.json").write_text("{}", encoding="utf-8")
    (tmp_path / "demo-escalate.json").write_text("{}", encoding="utf-8")

    exit_code = run_demo.main([])

    assert exit_code == 0
    assert sorted(seen) == ["demo-approve.json", "demo-escalate.json"]


def test_a13_the_runner_reports_a_failed_scenario(monkeypatch, tmp_path):
    """A scenario that fails must make the run fail, like `set -e` did."""
    from demo import run_demo

    monkeypatch.setattr(run_demo, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(run_demo, "seed", lambda: 0)
    monkeypatch.setattr(run_demo, "_process", lambda path: 1)
    (tmp_path / "demo-approve.json").write_text("{}", encoding="utf-8")

    assert run_demo.main([]) == 1


def test_a13_an_empty_scenarios_directory_is_an_error_not_a_silent_success(monkeypatch, tmp_path):
    """Nothing to run is not a successful demo."""
    from demo import run_demo

    monkeypatch.setattr(run_demo, "SCENARIOS_DIR", tmp_path)
    monkeypatch.setattr(run_demo, "seed", lambda: 0)

    assert run_demo.main([]) == 1
