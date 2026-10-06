"""Tests for src/api.py.

read_latest_per_request and append_entry are mocked throughout: no test
here touches the real data/audit_log.jsonl file, and no test hits a real
Jev/Kev server or Circle API.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.audit.logger import AuditEntry
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest, RequestCategory

client = TestClient(app)


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
