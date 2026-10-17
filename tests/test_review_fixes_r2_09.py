"""A decision must carry the rules that produced it, and a rules CHANGE
must be in the log too.

The audit log could already answer "what was decided, on what scores, by which
engine". It could not answer "UNDER WHICH RULES". `load_config()` runs on every
request and `config/rules.yaml` is editable by hand, by the CLI and by the
dashboard, so reproducing a decision from three weeks ago meant finding the
rules file as it was three weeks ago -- which nobody kept. A decision whose
inputs cannot be reconstructed is not auditable, whatever the hash chain says.

The second half is the other direction: `PUT /api/config` changed the rules with
NO entry in the log at all. A log that records every payment but not the rule
change that made the payment legal is auditing the wrong thing.

Design decisions taken here:
  * the FULL normalized config is stored on each decision entry, not a hash:
    a hash proves the rules changed but not what they were;
  * a config change is its own entry in the SAME chain, so removing one is
    detected exactly like removing a payment entry.
"""

import json
from pathlib import Path

import pytest

from src.config import RulesConfig, ScoringCriteria
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.policy.rules import Decision, PolicyDecision
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds


def rules(**overrides) -> RulesConfig:
    limits = overrides.pop("limits", None)
    return RulesConfig(
        category_limits=limits or category_limits(),
        thresholds=thresholds(**overrides),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        require_known_payee=False,
    )


def scores() -> DecisionScores:
    return DecisionScores(
        trust=ScoreResult(value=0.9, confidence=0.8),
        urgency=ScoreResult(value=0.1, confidence=0.8),
        importance=ScoreResult(value=0.1, confidence=0.8),
    )


def request(request_id: str = "r2-09-1") -> PaymentRequest:
    return PaymentRequest(
        request_id=request_id,
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="t",
        description="d",
    )


# --------------------------------------------------------------------------
# The snapshot on a decision entry
# --------------------------------------------------------------------------


def test_a_decision_entry_carries_the_rules_that_were_in_force(monkeypatch):
    """The entry must let a reviewer reconstruct the decision without the file."""
    from src.audit.logger import build_entry

    config = rules(reduced_amount_fraction=0.31, trust_min_auto_approve=0.66)
    monkeypatch.setattr("src.audit.logger.load_config", lambda *a, **k: config)

    entry = build_entry(
        request=request(),
        scores=scores(),
        policy_decision=PolicyDecision(Decision.APPROVE, None, "High trust score."),
        payment_executed=False,
    )

    assert entry.config_snapshot is not None, "the entry does not say which rules applied"
    snapshot = entry.config_snapshot
    assert snapshot["thresholds"]["reduced_amount_fraction"] == 0.31
    assert snapshot["thresholds"]["trust_min_auto_approve"] == 0.66
    assert snapshot["category_limits"]["supply_purchase"]["max_usdc"] == 1000.0
    assert snapshot["require_known_payee"] is False
    assert entry.config_hash, "the entry carries no fingerprint of the rules"


def test_two_decisions_under_different_rules_are_distinguishable(monkeypatch):
    """This is what makes an old decision reproducible: the entry itself says
    which ruleset it belongs to."""
    from src.audit.logger import build_entry

    decision = PolicyDecision(Decision.APPROVE, None, "r")

    monkeypatch.setattr("src.audit.logger.load_config", lambda *a, **k: rules(trust_min_auto_approve=0.7))
    first = build_entry(request=request("a"), scores=scores(), policy_decision=decision, payment_executed=False)

    monkeypatch.setattr("src.audit.logger.load_config", lambda *a, **k: rules(trust_min_auto_approve=0.8))
    second = build_entry(request=request("b"), scores=scores(), policy_decision=decision, payment_executed=False)

    assert first.config_hash != second.config_hash
    assert first.config_snapshot["thresholds"]["trust_min_auto_approve"] == 0.7
    assert second.config_snapshot["thresholds"]["trust_min_auto_approve"] == 0.8


def test_the_snapshot_is_the_normalized_config_not_the_raw_file(monkeypatch):
    """A hand-edited file may write a bare number for a category and omit the
    optional sections. What a decision needs is the EFFECTIVE config, after
    defaults and coercion -- so the snapshot is the validated model."""
    from src.audit.logger import build_entry

    from src.config import RulesConfig as RC

    bare = RC.model_validate(
        {
            "category_limits": {
                "contractor_milestone": 2000.0,
                "supply_purchase": 1000.0,
                "operational_expense": 500.0,
                "other": 200.0,
            },
            "thresholds": thresholds().model_dump(),
            "criteria": {"trust": ["a", "b"], "urgency": ["a", "b"], "importance": ["a", "b"]},
        }
    )
    monkeypatch.setattr("src.audit.logger.load_config", lambda *a, **k: bare)

    entry = build_entry(
        request=request(),
        scores=scores(),
        policy_decision=PolicyDecision(Decision.APPROVE, None, "r"),
        payment_executed=False,
    )

    # The bare number became the full object, and the omitted sections got their
    # defaults -- which is what the policy actually read.
    assert entry.config_snapshot["category_limits"]["other"]["max_usdc"] == 200.0
    assert entry.config_snapshot["vendor_registry"] == {}
    assert entry.config_snapshot["require_known_payee"] is True


def test_an_entry_written_before_this_field_still_loads():
    """The chain is computed over the raw line, so old rows must keep verifying
    and keep loading: the two fields are optional."""
    from src.audit.logger import AuditEntry

    legacy = AuditEntry.model_validate(
        {
            "timestamp": "2026-10-06T12:00:00+00:00",
            "request": request().model_dump(mode="json"),
            "scores": scores().model_dump(mode="json"),
            "decision": "approve",
            "reduced_amount": None,
            "reason": "r",
            "payment_executed": False,
        }
    )

    assert legacy.config_snapshot is None
    assert legacy.config_hash is None


# --------------------------------------------------------------------------
# Recording a config CHANGE
# --------------------------------------------------------------------------


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    """The log this test writes to, with every default-path caller pointed at it.

    Patching `get_audit_log_path` rather than the environment variable keeps the
    writer and the reader on the SAME path: `record_config_change` writes through
    `append_entry`, and `read_all_entries` reads, and both resolve the default
    through this function. (A `conftest` fixture already does this globally; it
    is repeated here so the path is visible next to the assertions.)
    """
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("src.audit.store.get_audit_log_path", lambda: str(path))
    return path


def read_all_entries_safe(path=None) -> list:
    """Read the log through the same resolved path the writers used."""
    from src.audit.store import read_all_entries

    return read_all_entries(path)


def test_a_config_change_is_recorded_with_before_and_after(audit_file):
    """`PUT /api/config` used to change the rules with no entry in the log."""
    from src.audit.logger import record_config_change
    from src.audit.store import read_all_entries

    before = rules(trust_min_auto_approve=0.75)
    after = rules(trust_min_auto_approve=0.9)

    record_config_change(before, after, actor="ana", note="tightened after the incident")

    entries = read_all_entries(str(audit_file))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.decision == "config_change"
    assert entry.reviewer == "ana"
    assert entry.config_snapshot == after.model_dump(mode="json")
    assert entry.previous_config_snapshot == before.model_dump(mode="json")
    assert "tightened after the incident" in entry.reason


def test_a_config_change_stays_in_the_same_hash_chain(audit_file):
    """Same chain, so removing the change breaks the chain like removing a
    payment would. A separate file would be a second thing to forget to check."""
    from src.audit.logger import record_config_change
    from src.audit.store import read_all_entries
    from src.audit.verify import verify_chain

    for i in range(3):
        record_config_change(rules(trust_min_auto_approve=0.5 + i / 10), rules(trust_min_auto_approve=0.6 + i / 10), actor=f"u{i}")

    result = verify_chain(str(audit_file))
    assert result.ok is True

    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # Every line participates: each links to the previous one's hash.
    records = [json.loads(line) for line in lines]
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert records[2]["prev_hash"] == records[1]["entry_hash"]


def test_a_config_change_is_not_a_payment_and_not_an_escalation(audit_file):
    """It must not show up in the escalation queue or in the payment counters."""
    from src.audit.logger import record_config_change
    from src.audit.store import read_latest_per_request

    record_config_change(rules(), rules(trust_min_auto_approve=0.9), actor="ana")

    entries = read_latest_per_request(str(audit_file))
    assert len(entries) == 1
    assert entries[0].decision == "config_change"
    # The two filters the API uses for its counters and its queue.
    assert [e for e in entries if e.decision == "escalate" and e.human_review is None] == []
    assert [e for e in entries if e.payment_executed] == []
    assert not entries[0].payment_executed


def test_recording_a_change_does_not_require_the_config_to_have_changed(audit_file):
    """A no-op save is still a fact worth recording: someone pressed save."""
    from src.audit.logger import record_config_change
    from src.audit.store import read_all_entries

    same = rules()
    record_config_change(same, same, actor="ana")

    entry = read_all_entries(str(audit_file))[0]
    assert entry.config_hash == entry.previous_config_hash
    assert "no effective change" in entry.reason.lower()


def test_the_snapshot_survives_a_round_trip_through_the_log(audit_file):
    """What a reviewer reads back must be the same config object, not a string
    that has to be re-parsed by hand."""
    from src.audit.logger import record_config_change
    from src.audit.store import read_all_entries

    after = rules(reduced_amount_fraction=0.25)
    record_config_change(rules(), after, actor="ana")

    stored = read_all_entries(str(audit_file))[0].config_snapshot
    assert RulesConfig.model_validate(stored).thresholds.reduced_amount_fraction == 0.25


# --------------------------------------------------------------------------
# The route that changes the rules
# --------------------------------------------------------------------------


@pytest.fixture
def api_client(audit_file):
    from fastapi.testclient import TestClient

    from src.api import app
    from tests.conftest import MANAGER_AUTH

    return TestClient(app, headers=MANAGER_AUTH)


@pytest.fixture
def patch_config_io(monkeypatch):
    """Keep the route off the real config file and off the real Circle API."""
    saved: list = []
    monkeypatch.setattr("src.api.load_config", lambda *a, **k: rules())
    monkeypatch.setattr("src.api.save_config", lambda config, *a, **k: saved.append(config))
    return saved


def fake_balance_client(monkeypatch, balance: float | Exception):
    """Replace the whole payment client with one that only knows its balance.

    Patching just `get_balance` is not enough: constructing `ArcPaymentClient`
    already talks to the Circle SDK, so a broken or absent SDK makes the
    constructor raise and the ceiling check fall open -- which would make these
    tests measure the environment instead of the route. The fake replaces the
    class, so nothing under test touches Circle at all.
    """

    def get_balance(self):
        if isinstance(balance, Exception):
            raise balance
        return balance

    fake = type("FakeArcPaymentClient", (), {"get_balance": get_balance})
    monkeypatch.setattr("src.payments.arc_client.ArcPaymentClient", fake)


def test_put_config_records_the_change_in_the_audit_log(api_client, audit_file, monkeypatch):
    """The route used to rewrite the rules with nothing in the log."""
    from src.audit.store import read_all_entries

    monkeypatch.setattr("src.api.load_config", lambda *a, **k: rules(trust_min_auto_approve=0.75))
    monkeypatch.setattr("src.api.save_config", lambda config, *a, **k: None)
    fake_balance_client(monkeypatch, 1_000_000.0)

    body = rules(trust_min_auto_approve=0.9).model_dump(mode="json")
    response = api_client.put("/api/config", json=body, headers={"X-Operator": "ana"})

    assert response.status_code == 200
    entries = read_all_entries(str(audit_file))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.decision == "config_change"
    assert entry.reviewer == "ana"
    assert entry.previous_config_snapshot["thresholds"]["trust_min_auto_approve"] == 0.75
    assert entry.config_snapshot["thresholds"]["trust_min_auto_approve"] == 0.9


def test_put_config_refuses_a_limit_above_the_funds_available(api_client, audit_file, monkeypatch):
    """The ceiling is enforced where rules are persisted."""
    monkeypatch.setattr("src.api.load_config", lambda *a, **k: rules())
    saved: list = []
    monkeypatch.setattr("src.api.save_config", lambda config, *a, **k: saved.append(config))
    fake_balance_client(monkeypatch, 1_000.0)

    body = rules(
        limits=category_limits(contractor_milestone=5_000.0, supply_purchase=800.0, other=100.0)
    ).model_dump(mode="json")
    response = api_client.put("/api/config", json=body)

    assert response.status_code == 422
    assert "contractor_milestone" in response.json()["detail"]
    assert saved == [], "a refused config must not be persisted"
    assert read_all_entries_safe(str(audit_file)) == [], "a refused config must not be logged as applied"


def test_put_config_still_saves_when_the_balance_cannot_be_read(api_client, audit_file, monkeypatch):
    """Fail-open: an unreachable Circle must not make the rules uneditable."""
    monkeypatch.setattr("src.api.load_config", lambda *a, **k: rules())
    saved: list = []
    monkeypatch.setattr("src.api.save_config", lambda config, *a, **k: saved.append(config))
    fake_balance_client(monkeypatch, ConnectionError("Circle is unreachable"))

    response = api_client.put("/api/config", json=rules().model_dump(mode="json"))

    assert response.status_code == 200
    assert len(saved) == 1, "the config should have been saved"


def test_a_config_change_does_not_show_up_as_a_request_or_an_escalation(api_client, audit_file, monkeypatch):
    """The dashboard's totals and its escalation queue must not count a rules
    change as a payment request."""
    from src.audit.logger import record_config_change

    record_config_change(rules(), rules(trust_min_auto_approve=0.9), actor="ana")

    stats = api_client.get("/api/stats").json()
    assert stats["total_requests"] == 0, "a config change was counted as a request"
    assert stats["pending_count"] == 0
    assert api_client.get("/api/escalations/pending").json() == []
    assert api_client.get("/api/requests/latest").json() == []
