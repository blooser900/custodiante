"""Tests for the manager / requester separation.

The split is enforced by the server, so these tests call the HTTP API the
way an outsider would (no token, wrong token) rather than inspecting the UI.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.audit.logger import AuditEntry, FailedAttempt
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.public_view import PublicStatus, public_state, to_public_status
from src.requests.schema import PaymentRequest, RequestCategory
from tests.conftest import MANAGER_AUTH, MANAGER_TOKEN

anon = TestClient(app, raise_server_exceptions=False)  # an outsider: no credentials
manager = TestClient(app, headers=MANAGER_AUTH)

# Everything an anonymous visitor may reach. Anything else MUST demand the token.
PUBLIC = {
    ("GET", "/"),
    ("GET", "/manager"),  # static HTML shell: carries no data, the API behind it is guarded
    ("GET", "/manager/settings"),
    ("GET", "/ui/manager-auth.js"),
    ("GET", "/api/requester/info"),
    ("POST", "/api/requester/requests"),
    ("GET", "/api/requester/requests/{request_id}"),
}


def all_routes() -> list[tuple[str, str]]:
    schema = app.openapi()
    return [(m.upper(), path) for path, methods in schema["paths"].items() for m in methods]


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# --------------------------------------------------------------------------
# No route is left open by accident
# --------------------------------------------------------------------------


def test_the_route_inventory_is_not_empty():
    # Guards the guard: if introspection silently returned nothing, the
    # parametrized test below would pass vacuously.
    assert len(all_routes()) >= 14


@pytest.mark.parametrize(("method", "path"), [r for r in all_routes() if r not in PUBLIC])
def test_every_non_public_route_rejects_an_anonymous_caller(method, path):
    response = anon.request(method, concrete(path), json={})
    assert response.status_code == 401, f"{method} {path} is reachable without a token"


@pytest.mark.parametrize(("method", "path"), [r for r in all_routes() if r not in PUBLIC])
def test_every_non_public_route_rejects_a_wrong_token(method, path):
    response = anon.request(
        method, concrete(path), json={}, headers={"Authorization": "Bearer not-the-right-token-xx"}
    )
    assert response.status_code == 401


def test_the_public_allow_list_has_no_stale_entries():
    assert PUBLIC - {("GET", "/"), ("GET", "/manager"), ("GET", "/manager/settings"), ("GET", "/ui/manager-auth.js")} <= set(all_routes())


def test_api_docs_are_not_published():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path).status_code == 404


def test_an_anonymous_caller_cannot_change_the_rules():
    with patch("src.api.save_config") as save:
        response = anon.put("/api/config", json={"category_limits": {"other": 999999}})
    assert response.status_code == 401
    save.assert_not_called()


def test_an_anonymous_caller_cannot_approve_or_release():
    with patch("src.api.apply_human_override") as approve, patch("src.api.advance_milestone") as release:
        assert anon.post("/api/escalations/r1/decision", json={"action": "approve", "reviewer": "x"}).status_code == 401
        assert anon.post("/api/milestones/r1/release", json={}).status_code == 401
    approve.assert_not_called()
    release.assert_not_called()


# --------------------------------------------------------------------------
# Manager token handling
# --------------------------------------------------------------------------


def test_the_right_token_is_accepted():
    with patch("src.api.read_latest_per_request", return_value=[]):
        assert manager.get("/api/stats").status_code == 200


def test_it_fails_closed_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("MANAGER_TOKEN", raising=False)
    response = manager.get("/api/stats")  # even with a "valid-looking" header
    assert response.status_code == 503
    assert "MANAGER_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize("weak", ["change-me", "short", "   ", ""])
def test_a_short_or_placeholder_token_counts_as_unconfigured(monkeypatch, weak):
    monkeypatch.setenv("MANAGER_TOKEN", weak)
    assert anon.get("/api/stats", headers={"Authorization": f"Bearer {weak}"}).status_code == 503


@pytest.mark.parametrize("header", ["", "Bearer", "Basic " + MANAGER_TOKEN, MANAGER_TOKEN, "Bearer " + MANAGER_TOKEN + "x"])
def test_malformed_or_near_miss_credentials_are_rejected(header):
    assert anon.get("/api/stats", headers={"Authorization": header}).status_code in (401, 403)


def test_token_comparison_is_constant_time():
    with patch("src.auth.secrets.compare_digest", return_value=False) as compare:
        anon.get("/api/stats", headers=MANAGER_AUTH)
    compare.assert_called_once()


# --------------------------------------------------------------------------
# What a requester can see
# --------------------------------------------------------------------------


def make_entry(**overrides) -> AuditEntry:
    base = dict(
        timestamp="2026-10-07T12:00:00+00:00",
        request=PaymentRequest(
            request_id="req-abc", category=RequestCategory.CONTRACTOR_MILESTONE, amount_usdc=400.0,
            requester="acme", description="Milestone 1", counterparty_wallet="0x" + "b" * 40,
        ),
        scores=DecisionScores(
            trust=ScoreResult(value=0.5, confidence=0.9),
            urgency=ScoreResult(value=0.9, confidence=0.8),
            importance=ScoreResult(value=0.1, confidence=0.7),
        ),
        decision="approve",
        reduced_amount=None,
        reason="Amount exceeds hard limit for category (500 USDC).",
        payment_executed=False,
        engine_name="kev",
        tx_hash="0xhash",
        payment_detail="InternalError: http://localhost:8080 refused",
        escrow_job_id=42,
        escrow_status="funded",
        reviewer="ana",
        failed_attempt=FailedAttempt(action="release", error="secret internal error"),
    )
    return AuditEntry(**{**base, **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (dict(decision="escalate"), "in_review"),
        (dict(decision="escalate", human_review="denied"), "declined"),
        (dict(decision="escalate", human_review="approved"), "approved"),  # defensive: odd shape
        (dict(decision="approve"), "approved"),
        (dict(decision="approve_reduced", reduced_amount=250.0), "approved"),
        (dict(decision="approve", escrow_status="released", payment_executed=True), "paid"),
        (dict(decision="approve", payment_executed=True, escrow_job_id=None, escrow_status=None), "paid"),
        (dict(decision="approve", escrow_status="rejected"), "declined"),
        (dict(decision="approve", payment_status="failed", escrow_job_id=None, escrow_status=None), "approved"),
    ],
)
def test_public_state_mapping(overrides, expected):
    assert public_state(make_entry(**overrides)) == expected


def test_the_public_view_exposes_exactly_the_allow_listed_fields():
    assert set(PublicStatus.model_fields) == {
        "request_id", "status", "category", "amount_requested", "amount_approved", "updated_at", "tx_hash",
    }


def test_the_public_view_leaks_nothing_internal():
    dumped = str(to_public_status(make_entry()).model_dump()).lower()
    for secret in ("0.5", "0.9", "trust", "score", "confidence", "500 usdc", "hard limit", "kev",
                   "localhost", "secret internal", "ana", "42", "escrow", "0xb", "failed"):
        assert secret not in dumped, f"public view leaked {secret!r}"


def test_a_refusal_never_reveals_what_would_have_been_acceptable():
    for overrides in (dict(decision="escalate"), dict(decision="escalate", human_review="denied")):
        assert to_public_status(make_entry(**overrides)).amount_approved is None


def test_the_approved_amount_is_the_reduced_one_and_the_tx_only_appears_when_paid():
    approved = to_public_status(make_entry(decision="approve_reduced", reduced_amount=250.0))
    assert (approved.amount_requested, approved.amount_approved, approved.tx_hash) == (400.0, 250.0, None)

    paid = to_public_status(make_entry(decision="approve", escrow_status="released", payment_executed=True))
    assert paid.tx_hash == "0xhash" and paid.amount_approved == 400.0


# --------------------------------------------------------------------------
# Requester endpoints
# --------------------------------------------------------------------------

SUBMISSION = dict(category="contractor_milestone", amount_usdc=400.0, requester="acme", description="Milestone 1")


def test_a_submission_returns_only_the_public_view_with_a_server_made_id():
    entry = make_entry()
    with patch("src.api.run_pipeline", return_value=entry) as run:
        response = anon.post("/api/requester/requests", json=SUBMISSION)

    assert response.status_code == 200
    assert set(response.json()) == set(PublicStatus.model_fields)
    submitted = run.call_args.args[0]
    assert submitted.request_id.startswith("req-") and len(submitted.request_id) >= 16


def test_two_submissions_get_different_unguessable_ids():
    ids = set()
    with patch("src.api.run_pipeline", return_value=make_entry()) as run:
        for _ in range(20):
            anon.post("/api/requester/requests", json=SUBMISSION)
            ids.add(run.call_args.args[0].request_id)
    assert len(ids) == 20


@pytest.mark.parametrize("extra", [{"request_id": "req-chosen"}, {"due_date": "2030-01-01T00:00:00Z"}, {"decision": "approve"}])
def test_a_requester_cannot_supply_server_decided_fields(extra):
    with patch("src.api.run_pipeline") as run:
        response = anon.post("/api/requester/requests", json={**SUBMISSION, **extra})
    assert response.status_code == 422
    run.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [dict(amount_usdc=0), dict(amount_usdc=-5), dict(amount_usdc="NaN"), dict(requester=""),
     dict(description=""), dict(description="x" * 2001), dict(category="not-a-category")],
)
def test_invalid_submissions_are_rejected_before_the_pipeline(bad):
    with patch("src.api.run_pipeline") as run:
        response = anon.post("/api/requester/requests", json={**SUBMISSION, **bad})
    assert response.status_code == 422
    run.assert_not_called()


def test_a_pipeline_crash_does_not_leak_its_message_to_a_requester():
    boom = RuntimeError("Kev unreachable at http://10.0.0.5:8080 with key sk-live-123")
    with patch("src.api.run_pipeline", side_effect=boom):
        response = anon.post("/api/requester/requests", json=SUBMISSION)
    assert response.status_code == 503
    assert "10.0.0.5" not in response.text and "sk-live" not in response.text


def test_the_same_crash_still_shows_its_message_to_a_manager():
    boom = RuntimeError("Kev unreachable at http://10.0.0.5:8080")
    request = dict(request_id="m-1", category="other", amount_usdc=5, requester="x", description="y")
    with patch("src.api.run_pipeline", side_effect=boom):
        response = TestClient(app, headers=MANAGER_AUTH, raise_server_exceptions=False).post("/api/requests", json=request)
    assert response.status_code == 502 and "10.0.0.5" in response.json()["detail"]


def test_status_lookup_returns_the_public_view_for_a_known_id():
    with patch("src.api.read_latest_per_request", return_value=[make_entry()]):
        response = anon.get("/api/requester/requests/req-abc")
    assert response.status_code == 200
    assert set(response.json()) == set(PublicStatus.model_fields)


def test_status_lookup_404s_for_unknown_ids_and_for_ids_not_made_by_the_portal():
    internal = make_entry(request=PaymentRequest(
        request_id="demo-approve-1", category="other", amount_usdc=5, requester="x", description="y"))
    with patch("src.api.read_latest_per_request", return_value=[make_entry(), internal]):
        assert anon.get("/api/requester/requests/req-nope").status_code == 404
        assert anon.get("/api/requester/requests/demo-approve-1").status_code == 404  # exists, but not ours to show


def test_a_requester_cannot_list_other_requests():
    # There is no collection endpoint: only lookup by an unguessable id.
    assert anon.get("/api/requester/requests").status_code in (404, 405)
    assert anon.get("/api/requests/latest").status_code == 401


# --------------------------------------------------------------------------
# Optional access code for submitting
# --------------------------------------------------------------------------


def test_with_no_access_code_configured_submission_is_open():
    assert anon.get("/api/requester/info").json() == {"access_code_required": False}


def test_an_access_code_gates_submission_but_not_status_lookup(monkeypatch):
    monkeypatch.setenv("REQUESTER_ACCESS_CODE", "team-code-42")
    assert anon.get("/api/requester/info").json() == {"access_code_required": True}

    with patch("src.api.run_pipeline", return_value=make_entry()) as run:
        assert anon.post("/api/requester/requests", json=SUBMISSION).status_code == 401
        assert anon.post("/api/requester/requests", json=SUBMISSION, headers={"X-Access-Code": "wrong"}).status_code == 401
        run.assert_not_called()
        assert anon.post("/api/requester/requests", json=SUBMISSION, headers={"X-Access-Code": "team-code-42"}).status_code == 200

    with patch("src.api.read_latest_per_request", return_value=[make_entry()]):
        assert anon.get("/api/requester/requests/req-abc").status_code == 200  # the id is the capability


def test_neither_the_answers_nor_the_evidence_ever_reach_a_requester():
    """Extra answers and the model's evidence are exactly the 'reasoning'
    that must stay with managers."""
    from src.decision_engine.mock_engine import MockDecisionEngine

    entry = make_entry(scores=MockDecisionEngine().score(make_entry().request))
    assert entry.scores.extra and entry.scores.evidence  # there IS something to leak

    dumped = str(to_public_status(entry).model_dump()).lower()
    for secret in ("fraud_indicators", "description_matches_category", "natureza_transacao",
                   "evidence", "probabilities", "history", "identity_basis", "reliability"):
        assert secret not in dumped, f"public view leaked {secret!r}"
