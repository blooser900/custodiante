"""Tests for src/api.py.

read_latest_per_request and append_entry are mocked throughout: no test
here touches the real data/audit_log.jsonl file, and no test hits a real
Jev/Kev server or Circle API. load_config() is also mocked (autouse) so
no test touches the real config/rules.yaml either.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.audit.logger import AuditEntry
from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest, RequestCategory

client = TestClient(app)


def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits={"operational_expense": 500.0, "other": 200.0},
        thresholds=Thresholds(
            trust_min_auto_approve=0.75,
            trust_min_confidence_for_auto_approve=0.7,
            trust_min_to_consider=0.4,
            importance_high=0.7,
            urgency_high=0.7,
            reduced_amount_fraction=0.5,
        ),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
    )


@pytest.fixture(autouse=True)
def mock_config():
    config = fake_rules_config()
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        yield config


def make_entry(
    request_id: str,
    decision: str,
    amount: float = 100.0,
    payment_executed: bool = False,
    human_review: str | None = None,
    reviewer: str | None = None,
    reduced_amount: float | None = None,
    counterparty_wallet: str | None = None,
) -> AuditEntry:
    request = PaymentRequest(
        request_id=request_id,
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=amount,
        requester="test-requester",
        description="A test request.",
        counterparty_wallet=counterparty_wallet,
    )
    scores = DecisionScores(
        trust=ScoreResult(value=0.5, confidence=0.8),
        urgency=ScoreResult(value=0.5, confidence=0.8),
        importance=ScoreResult(value=0.5, confidence=0.8),
    )
    return AuditEntry(
        timestamp="2026-10-05T12:00:00+00:00",
        request=request,
        scores=scores,
        decision=decision,
        reduced_amount=reduced_amount,
        reason="test reason",
        payment_executed=payment_executed,
        human_review=human_review,
        reviewer=reviewer,
    )


# --------------------------------------------------------------------------
# GET endpoints
# --------------------------------------------------------------------------


@patch("src.api.read_latest_per_request")
def test_stats_counts_pending_and_paid(mock_read):
    mock_read.return_value = [
        make_entry("r1", "escalate"),  # pending
        make_entry("r2", "approve", amount=50.0, payment_executed=True),
        make_entry("r3", "approve_reduced", amount=200.0, reduced_amount=80.0, payment_executed=True),
        make_entry("r4", "escalate", human_review="denied", reviewer="ana"),  # not pending
    ]

    response = client.get("/api/stats")

    assert response.status_code == 200
    data = response.json()
    assert data["total_requests"] == 4
    assert data["pending_count"] == 1
    assert data["payments_executed"] == 2
    assert data["paid_total_usdc"] == pytest.approx(130.0)  # 50 + 80


@patch("src.api.read_latest_per_request")
def test_pending_escalations_filters_correctly(mock_read):
    mock_read.return_value = [
        make_entry("r1", "escalate"),
        make_entry("r2", "approve"),
        make_entry("r3", "escalate", human_review="approved", reviewer="joão"),
    ]

    response = client.get("/api/escalations/pending")

    assert response.status_code == 200
    ids = [e["request"]["request_id"] for e in response.json()]
    assert ids == ["r1"]


@patch("src.api.read_latest_per_request")
def test_latest_requests_returns_serialized_entries(mock_read):
    mock_read.return_value = [make_entry("r1", "approve")]

    response = client.get("/api/requests/latest")

    assert response.status_code == 200
    assert response.json()[0]["request"]["request_id"] == "r1"


# --------------------------------------------------------------------------
# POST /api/requests
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch("src.pipeline.DEFAULT_ENGINE_FACTORY")
def test_submit_request_runs_pipeline_and_returns_decision(mock_factory, mock_append):
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.95, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    mock_factory.return_value = engine

    payload = {
        "request_id": "api-test-1",
        "category": "operational_expense",
        "amount_usdc": 50.0,
        "requester": "ui-form",
        "description": "Submitted through the dashboard.",
    }

    response = client.post("/api/requests", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "approve"
    assert data["payment_executed"] is False  # no counterparty_wallet in payload
    mock_append.assert_called_once()


@patch("src.pipeline.append_entry")
@patch("src.pipeline.ArcPaymentClient")
@patch("src.pipeline.DEFAULT_ENGINE_FACTORY")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_submit_request_with_wallet_executes_payment(mock_factory, mock_arc_cls, mock_append):
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.95, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    mock_factory.return_value = engine
    mock_arc_cls.return_value.send_payment.return_value = {
        "transaction_id": "tx-1",
        "tx_hash": "0xabc",
        "state": "COMPLETE",
    }

    payload = {
        "request_id": "api-test-2",
        "category": "operational_expense",
        "amount_usdc": 50.0,
        "requester": "ui-form",
        "description": "Submitted through the dashboard.",
        "counterparty_wallet": "0x" + "b" * 40,
    }

    response = client.post("/api/requests", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["payment_executed"] is True
    assert data["tx_hash"] == "0xabc"


def test_submit_request_rejects_invalid_payload():
    response = client.post("/api/requests", json={"category": "operational_expense"})
    assert response.status_code == 422  # missing required fields


# --------------------------------------------------------------------------
# POST /api/escalations/{id}/decision
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch("src.api.read_latest_per_request")
def test_decide_escalation_deny(mock_read, mock_append):
    mock_read.return_value = [make_entry("r1", "escalate")]

    response = client.post(
        "/api/escalations/r1/decision", json={"action": "deny", "reviewer": "ana"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["human_review"] == "denied"
    assert data["reviewer"] == "ana"
    assert data["payment_executed"] is False
    mock_append.assert_called_once()


@patch("src.pipeline.append_entry")
@patch("src.pipeline.ArcPaymentClient")
@patch("src.api.read_latest_per_request")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_decide_escalation_approve_executes_payment(mock_read, mock_arc_cls, mock_append):
    mock_read.return_value = [
        make_entry("r1", "escalate", amount=5000.0, counterparty_wallet="0x" + "c" * 40)
    ]
    mock_arc_cls.return_value.send_payment.return_value = {
        "transaction_id": "tx-9",
        "tx_hash": "0xdef",
        "state": "COMPLETE",
    }

    response = client.post(
        "/api/escalations/r1/decision", json={"action": "approve", "reviewer": "joão"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["human_review"] == "approved"
    assert data["payment_executed"] is True
    assert data["tx_hash"] == "0xdef"
    mock_arc_cls.return_value.send_payment.assert_called_once()
    _, kwargs = mock_arc_cls.return_value.send_payment.call_args
    assert kwargs["amount_usdc"] == 5000.0  # full original amount, not a reduced one


@patch("src.api.read_latest_per_request")
def test_decide_escalation_not_found(mock_read):
    mock_read.return_value = []

    response = client.post(
        "/api/escalations/does-not-exist/decision", json={"action": "approve", "reviewer": "x"}
    )

    assert response.status_code == 404


@patch("src.api.read_latest_per_request")
def test_decide_escalation_wrong_state(mock_read):
    mock_read.return_value = [make_entry("r1", "approve")]

    response = client.post(
        "/api/escalations/r1/decision", json={"action": "approve", "reviewer": "x"}
    )

    assert response.status_code == 409


@patch("src.api.read_latest_per_request")
def test_decide_escalation_already_reviewed(mock_read):
    mock_read.return_value = [
        make_entry("r1", "escalate", human_review="denied", reviewer="ana")
    ]

    response = client.post(
        "/api/escalations/r1/decision", json={"action": "approve", "reviewer": "joão"}
    )

    assert response.status_code == 409
    assert "ana" in response.json()["detail"]


# --------------------------------------------------------------------------
# GET/PUT /api/config
# --------------------------------------------------------------------------


@patch("src.api.load_config")
def test_get_config_returns_current_rules(mock_load):
    mock_load.return_value = fake_rules_config()

    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["category_limits"]["operational_expense"] == 500.0
    assert data["criteria"]["trust"] == ["a", "b"]


@patch("src.api.save_config")
def test_put_config_persists_valid_update(mock_save):
    payload = fake_rules_config().model_dump()
    payload["category_limits"]["operational_expense"] = 999.0

    response = client.put("/api/config", json=payload)

    assert response.status_code == 200
    assert response.json()["category_limits"]["operational_expense"] == 999.0
    mock_save.assert_called_once()
    saved_config = mock_save.call_args[0][0]
    assert saved_config.category_limits["operational_expense"] == 999.0


def test_put_config_rejects_invalid_threshold():
    payload = fake_rules_config().model_dump()
    payload["thresholds"]["trust_min_auto_approve"] = 5.0  # out of [0, 1]

    response = client.put("/api/config", json=payload)

    assert response.status_code == 422  # FastAPI's own validation, before save_config runs


def test_put_config_rejects_too_few_rubric_levels():
    payload = fake_rules_config().model_dump()
    payload["criteria"]["trust"] = ["only one level"]

    response = client.put("/api/config", json=payload)

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Idempotency and payment-status reporting
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
def test_submit_duplicate_request_id_returns_409(mock_append):
    existing = make_entry("api-dup-1", "approve")
    payload = {
        "request_id": "api-dup-1",
        "category": "operational_expense",
        "amount_usdc": 50.0,
        "requester": "ui-form",
        "description": "Submitted twice.",
    }

    with patch("src.pipeline.read_latest_per_request", return_value=[existing]):
        response = client.post("/api/requests", json=payload)

    assert response.status_code == 409
    assert "already processed" in response.json()["detail"]
    mock_append.assert_not_called()


@patch("src.pipeline.append_entry")
@patch("src.api.read_latest_per_request")
def test_decide_escalation_lost_race_returns_409(mock_read, mock_append):
    pending = make_entry("r1", "escalate")
    mock_read.return_value = [pending]
    reviewed = make_entry("r1", "approve", human_review="approved", reviewer="ana")

    # api.py's own check sees it pending, but by the time the pipeline takes
    # the lock another reviewer has already acted.
    with patch("src.pipeline.read_latest_per_request", return_value=[reviewed]):
        response = client.post(
            "/api/escalations/r1/decision", json={"action": "approve", "reviewer": "joão"}
        )

    assert response.status_code == 409
    mock_append.assert_not_called()


@patch("src.api.read_latest_per_request")
def test_stats_counts_failed_payments(mock_read):
    failed = make_entry("r1", "approve")
    failed.payment_status = "failed"
    mock_read.return_value = [failed, make_entry("r2", "approve", payment_executed=True)]

    assert client.get("/api/stats").json()["payments_failed"] == 1
