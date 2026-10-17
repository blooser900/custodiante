"""Tests for milestone escrow wired into the pipeline and the API.

The escrow client is replaced by a recording fake and the audit log by an
in-memory list, so these tests assert exactly which on-chain steps would run
and exactly what would be written to the log.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import MANAGER_AUTH
from src.api import app
from src.audit.logger import AuditEntry, FailedAttempt
from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.payments.escrow import EscrowBudgetMismatchError, EscrowError
from src.pipeline import (
    MilestoneError,
    MilestoneNotFoundError,
    advance_milestone,
    apply_human_override,
    run_pipeline,
)
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds


client = TestClient(app, headers=MANAGER_AUTH)
CONTRACTOR = "0x" + "b" * 40


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def rules():
    config = RulesConfig(
        category_limits=category_limits(contractor_milestone=500.0, operational_expense=500.0),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        # Payment-mechanics tests: an unseen payee would be escalated before the code under test runs.
        require_known_payee=False,
    )
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        yield


@pytest.fixture
def log():
    """In-memory audit log: append_entry writes to it, reads read from it."""
    entries: list[AuditEntry] = []

    def latest():
        by_id = {e.request.request_id: e for e in entries}
        return list(by_id.values())

    with (
        patch("src.pipeline.append_entry", side_effect=entries.append),
        patch("src.pipeline.read_latest_per_request", side_effect=latest),
        patch("src.api.read_latest_per_request", side_effect=latest),
    ):
        yield entries


class FakeEscrow:
    """Records every on-chain step and returns canned transactions."""

    def __init__(self, job_id: int = 42):
        self.job_id = job_id
        self.calls: list[tuple] = []

    @staticmethod
    def _tx(name: str) -> dict:
        return {"transaction_id": f"id-{name}", "tx_hash": f"0x{name}", "state": "COMPLETE"}

    def create_job(self, provider, description, **_):
        self.calls.append(("create_job", provider, description))
        return self.job_id, self._tx("create")

    def set_budget(self, job_id, amount, provider_wallet):
        self.calls.append(("set_budget", job_id, amount, provider_wallet))
        return self._tx("budget")

    def fund(self, job_id, amount):
        self.calls.append(("fund", job_id, amount))
        return {**self._tx("fund"), "approve_tx_hash": "0xapprove"}

    def submit(self, job_id, deliverable, provider_wallet):
        self.calls.append(("submit", job_id, provider_wallet))
        return self._tx("submit")

    def complete(self, job_id, reason):
        self.calls.append(("complete", job_id))
        return self._tx("complete")

    def reject(self, job_id, reason):
        self.calls.append(("reject", job_id))
        return self._tx("reject")

    @property
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def engine_with(trust=0.95, urgency=0.1, importance=0.1) -> MagicMock:
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=trust, confidence=0.9),
        urgency=ScoreResult(value=urgency, confidence=0.9),
        importance=ScoreResult(value=importance, confidence=0.9),
    )
    return engine


def milestone(request_id="m-1", amount=100.0, wallet=CONTRACTOR, category=RequestCategory.CONTRACTOR_MILESTONE):
    return PaymentRequest(
        request_id=request_id,
        category=category,
        amount_usdc=amount,
        requester="contractor:acme",
        description="Milestone 1 delivered.",
        counterparty_wallet=wallet,
    )


def entry_in_state(escrow_status: str, request_id="m-1", reduced=None) -> AuditEntry:
    """An already-logged milestone sitting in the given escrow state."""
    return AuditEntry(
        timestamp="2026-10-06T12:00:00+00:00",
        request=milestone(request_id),
        scores=engine_with().score.return_value,
        decision="approve" if reduced is None else "approve_reduced",
        reduced_amount=reduced,
        reason="r",
        payment_executed=False,
        payment_status="escrow_open" if escrow_status == "open" else "escrowed",
        escrow_job_id=42,
        escrow_status=escrow_status,
    )


WITH_CIRCLE = patch.dict(os.environ, {"CIRCLE_API_KEY": "k"}, clear=True)
PROVIDER_IS_OURS = patch.dict(
    os.environ, {"CIRCLE_API_KEY": "k", "ESCROW_PROVIDER_WALLET_ADDRESS": CONTRACTOR}, clear=True
)


# --------------------------------------------------------------------------
# Opening the escrow when the policy approves
# --------------------------------------------------------------------------


@WITH_CIRCLE
def test_approved_milestone_opens_a_job_instead_of_paying_directly(log):
    escrow, arc = FakeEscrow(), MagicMock()

    entry = run_pipeline(milestone(), engine=engine_with(), arc_client=arc, escrow=escrow, verbose=False)

    arc.send_payment.assert_not_called()  # no direct transfer
    assert escrow.calls == [("create_job", CONTRACTOR, "custodiante:m-1")]  # only the id goes on-chain
    assert (entry.escrow_job_id, entry.escrow_status) == (42, "open")
    assert entry.payment_status == "escrow_open"
    assert entry.payment_executed is False  # nothing has been paid
    assert entry.tx_hash == "0xcreate"


@PROVIDER_IS_OURS
def test_when_we_control_the_contractor_wallet_the_escrow_is_funded_right_away(log):
    escrow = FakeEscrow()

    entry = run_pipeline(milestone(), engine=engine_with(), escrow=escrow, verbose=False)

    assert escrow.names == ["create_job", "set_budget", "fund"]
    assert escrow.calls[1] == ("set_budget", 42, 100.0, CONTRACTOR)
    assert (entry.escrow_status, entry.payment_status) == ("funded", "escrowed")
    assert entry.payment_executed is False  # locked is not paid
    assert entry.tx_hash == "0xfund"


@PROVIDER_IS_OURS
def test_a_reduced_approval_escrows_the_reduced_amount(log):
    """When the category DOES allow a part payment, the escrow gets the fraction.

    A contractor milestone does not, by default -- see the next test. This one
    overrides the category so it is about the escrow amount, not the rule.
    """
    escrow = FakeEscrow()
    permissive = RulesConfig(
        category_limits=category_limits(contractor_milestone=500.0, operational_expense=500.0),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        require_known_payee=False,
    )
    permissive.category_limits["contractor_milestone"] = permissive.category_limits[
        "contractor_milestone"
    ].model_copy(update={"allow_reduced": True})

    # Medium trust + high urgency => approve_reduced, capped at
    # limit x fraction = 500 x 0.5 = 250 (see policy/rules.py).
    with (
        patch("src.policy.rules.load_config", return_value=permissive),
        patch("src.policy.limits.load_config", return_value=permissive),
    ):
        entry = run_pipeline(
            milestone(amount=400.0), engine=engine_with(trust=0.5, urgency=0.9, importance=0.9),
            escrow=escrow, verbose=False,
        )

    assert entry.decision == "approve_reduced" and entry.reduced_amount == 250.0
    assert ("set_budget", 42, 250.0, CONTRACTOR) in escrow.calls
    assert ("fund", 42, 250.0) in escrow.calls  # never the 400 that was requested


def test_a_milestone_is_never_paid_in_part(log):
    """Half an agreed contract price is not half a delivery.

    The reduced-approval branch pays a fraction of the ceiling, which is fine
    for a supply purchase and meaningless for a milestone: it would pay for work
    that was never accepted. With the shipped default (`allow_reduced: false`)
    the policy must escalate instead, and no escrow may be opened.
    """
    escrow = FakeEscrow()

    entry = run_pipeline(
        milestone(amount=400.0), engine=engine_with(trust=0.5, urgency=0.9, importance=0.9),
        escrow=escrow, verbose=False,
    )

    assert entry.decision == "escalate"
    assert entry.reduced_amount is None
    assert escrow.calls == []  # nothing was opened, nothing was funded
    assert "partial payment is not a valid outcome" in entry.reason


@WITH_CIRCLE
def test_non_milestone_categories_still_pay_directly(log):
    escrow, arc = FakeEscrow(), MagicMock()
    arc.send_payment.return_value = {"transaction_id": "t", "tx_hash": "0xdirect"}
    request = milestone(category=RequestCategory.OPERATIONAL_EXPENSE)

    entry = run_pipeline(request, engine=engine_with(), arc_client=arc, escrow=escrow, verbose=False)

    assert escrow.calls == []
    arc.send_payment.assert_called_once()
    assert entry.payment_executed is True and entry.escrow_job_id is None


@patch.dict(os.environ, {"CIRCLE_API_KEY": "k", "ESCROW_ENABLED": "false"}, clear=True)
def test_escrow_can_be_switched_off(log):
    escrow, arc = FakeEscrow(), MagicMock()
    arc.send_payment.return_value = {"transaction_id": "t", "tx_hash": "0xdirect"}

    run_pipeline(milestone(), engine=engine_with(), arc_client=arc, escrow=escrow, verbose=False)

    assert escrow.calls == []
    arc.send_payment.assert_called_once()


@patch.dict(os.environ, {}, clear=True)
def test_milestone_without_credentials_is_skipped_not_escrowed(log):
    escrow = FakeEscrow()
    entry = run_pipeline(milestone(), engine=engine_with(), escrow=escrow, verbose=False)
    assert escrow.calls == [] and entry.payment_status == "skipped"


@PROVIDER_IS_OURS
def test_a_failure_after_the_job_exists_keeps_the_job_id(log):
    """The job is already on-chain: losing its id would orphan it."""
    escrow = FakeEscrow()
    escrow.set_budget = MagicMock(side_effect=EscrowError("set budget failed"))

    entry = run_pipeline(milestone(), engine=engine_with(), escrow=escrow, verbose=False)

    assert entry.payment_status == "failed"
    assert "set budget failed" in entry.payment_detail
    assert (entry.escrow_job_id, entry.escrow_status) == (42, "open")  # operator can retry or reject


@WITH_CIRCLE
def test_failure_before_the_job_exists_records_no_job(log):
    escrow = FakeEscrow()
    escrow.create_job = MagicMock(side_effect=EscrowError("circle down"))

    entry = run_pipeline(milestone(), engine=engine_with(), escrow=escrow, verbose=False)

    assert entry.payment_status == "failed"
    assert entry.escrow_job_id is None and entry.escrow_status is None


@WITH_CIRCLE
def test_a_human_approved_escalation_also_goes_through_escrow(log):
    """Overriding the policy's limits must not also bypass the escrow hold."""
    escrow, arc = FakeEscrow(), MagicMock()
    escalated = run_pipeline(
        milestone(amount=5000.0), engine=engine_with(), arc_client=arc, escrow=escrow, verbose=False
    )
    assert escalated.decision == "escalate" and escrow.calls == []  # over the limit

    entry = apply_human_override(escalated, "approve", "ana", arc_client=arc, escrow=escrow)

    arc.send_payment.assert_not_called()
    assert escrow.names == ["create_job"]
    assert entry.escrow_status == "open" and entry.human_review == "approved"


# --------------------------------------------------------------------------
# Advancing the milestone
# --------------------------------------------------------------------------


def test_fund_locks_the_approved_amount_and_logs_it(log):
    log.append(entry_in_state("open", reduced=50.0))
    escrow = FakeEscrow()

    entry = advance_milestone("m-1", "fund", reviewer="ana", escrow=escrow)

    assert escrow.calls == [("fund", 42, 50.0)]  # the reduced amount, not the requested one
    assert (entry.escrow_status, entry.payment_status) == ("funded", "escrowed")
    assert entry.tx_hash == "0xfund" and log[-1] is entry


@PROVIDER_IS_OURS
def test_deliver_is_signed_by_the_contractor_wallet_we_control(log):
    log.append(entry_in_state("funded"))
    escrow = FakeEscrow()

    entry = advance_milestone("m-1", "deliver", escrow=escrow)

    assert escrow.calls == [("submit", 42, CONTRACTOR)]
    assert entry.escrow_status == "submitted"


@WITH_CIRCLE
def test_deliver_is_refused_for_a_real_contractor(log):
    log.append(entry_in_state("funded"))
    escrow = FakeEscrow()
    with pytest.raises(MilestoneError, match="must submit themselves"):
        advance_milestone("m-1", "deliver", escrow=escrow)
    assert escrow.calls == [] and len(log) == 1


@pytest.mark.parametrize("start", ["funded", "submitted"])
def test_release_pays_the_contractor_and_marks_the_payment_executed(log, start):
    log.append(entry_in_state(start))
    escrow = FakeEscrow()

    entry = advance_milestone("m-1", "release", reviewer="ana", escrow=escrow)

    assert escrow.calls == [("complete", 42)]
    assert entry.escrow_status == "released"
    assert (entry.payment_executed, entry.payment_status) == (True, "executed")
    assert entry.reviewer == "ana" and "ana" in entry.reason
    assert entry.tx_hash == "0xcomplete"


def test_reject_refunds_and_does_not_count_as_paid(log):
    log.append(entry_in_state("submitted"))
    escrow = FakeEscrow()

    entry = advance_milestone("m-1", "reject", reviewer="ana", reason="not delivered", escrow=escrow)

    assert escrow.calls == [("reject", 42)]
    assert (entry.escrow_status, entry.payment_status) == ("rejected", "refunded")
    assert entry.payment_executed is False


@pytest.mark.parametrize(
    ("status", "action"),
    [("released", "release"), ("rejected", "release"), ("released", "reject"), ("funded", "fund"), ("open", "release")],
)
def test_actions_are_refused_from_the_wrong_state(log, status, action):
    log.append(entry_in_state(status))
    escrow = FakeEscrow()
    with pytest.raises(MilestoneError, match="escrow status"):
        advance_milestone("m-1", action, escrow=escrow)
    assert escrow.calls == []


def test_unknown_request_and_non_escrow_request_are_refused(log):
    with pytest.raises(MilestoneNotFoundError):
        advance_milestone("ghost", "release", escrow=FakeEscrow())

    plain = entry_in_state("open")
    plain.escrow_job_id = None  # paid directly, never escrowed
    log.append(plain)
    with pytest.raises(MilestoneError):
        advance_milestone("m-1", "release", escrow=FakeEscrow())


def test_double_release_is_refused_the_second_time(log):
    log.append(entry_in_state("submitted"))
    escrow = FakeEscrow()
    advance_milestone("m-1", "release", escrow=escrow)
    with pytest.raises(MilestoneError):
        advance_milestone("m-1", "release", escrow=escrow)
    assert escrow.names.count("complete") == 1


# --------------------------------------------------------------------------
# Failed attempts are recorded
# --------------------------------------------------------------------------


def failing_release(error: Exception) -> FakeEscrow:
    escrow = FakeEscrow()
    escrow.complete = MagicMock(side_effect=error)
    return escrow


def test_a_failed_release_is_logged_and_still_raised(log):
    log.append(entry_in_state("submitted"))
    error = EscrowError("Failed to complete job: transaction t9 ended as FAILED.", transaction_id="t9", tx_hash="0xbad")

    with pytest.raises(EscrowError) as raised:
        advance_milestone("m-1", "release", reviewer="ana", escrow=failing_release(error))

    assert raised.value is error  # the caller still sees the original error
    assert len(log) == 2
    failed = log[-1]
    assert failed.failed_attempt.action == "release"
    assert failed.failed_attempt.error == str(error)
    assert (failed.failed_attempt.transaction_id, failed.failed_attempt.tx_hash) == ("t9", "0xbad")
    assert failed.reviewer == "ana" and "failed; escrow unchanged" in failed.reason


def test_a_failed_attempt_leaves_the_escrow_state_untouched(log):
    """The funds did not move, so the log must not say they did, nor that they vanished."""
    before = entry_in_state("submitted")
    log.append(before)

    with pytest.raises(EscrowError):
        advance_milestone("m-1", "release", escrow=failing_release(EscrowError("reverted")))

    after = log[-1]
    assert after.escrow_status == before.escrow_status == "submitted"
    assert after.payment_status == before.payment_status
    assert after.payment_executed is False  # not paid
    assert (after.tx_hash, after.transaction_id) == (before.tx_hash, before.transaction_id)  # last SUCCESSFUL tx
    assert after.escrow_job_id == 42


def test_a_failure_with_no_transaction_records_none(log):
    log.append(entry_in_state("open"))
    escrow = FakeEscrow()
    escrow.fund = MagicMock(side_effect=EscrowError("Circle rejected the request"))

    with pytest.raises(EscrowError):
        advance_milestone("m-1", "fund", escrow=escrow)

    assert log[-1].failed_attempt.tx_hash is None and log[-1].failed_attempt.transaction_id is None


def test_a_contractor_inflating_the_budget_leaves_an_audit_trail(log):
    log.append(entry_in_state("open"))
    escrow = FakeEscrow()
    escrow.fund = MagicMock(side_effect=EscrowBudgetMismatchError("Job 42 budget is 500.0 USDC but policy approved 100.0 USDC; refusing to fund."))

    with pytest.raises(EscrowBudgetMismatchError):
        advance_milestone("m-1", "fund", escrow=escrow)

    assert "refusing to fund" in log[-1].failed_attempt.error
    assert log[-1].escrow_status == "open"  # nothing was funded


def test_an_unexpected_exception_is_logged_with_its_type_and_reraised(log):
    log.append(entry_in_state("submitted"))
    boom = ConnectionError("rpc down")

    with pytest.raises(ConnectionError):
        advance_milestone("m-1", "release", escrow=failing_release(boom))

    assert log[-1].failed_attempt.error == "ConnectionError: rpc down"


def test_a_failure_to_even_build_the_escrow_client_is_logged(log):
    log.append(entry_in_state("submitted"))
    with patch("src.pipeline.EscrowClient", side_effect=RuntimeError("bad credentials")):
        with pytest.raises(RuntimeError):
            advance_milestone("m-1", "release")
    assert "bad credentials" in log[-1].failed_attempt.error


def test_an_identical_repeated_failure_is_not_logged_again(log):
    """The API has no auth: repeating a bad request must not grow the log forever."""
    log.append(entry_in_state("submitted"))
    for _ in range(5):
        with pytest.raises(EscrowError):
            advance_milestone("m-1", "release", escrow=failing_release(EscrowError("reverted")))
    assert len(log) == 2  # the original entry + ONE failure


def test_a_different_failure_or_action_is_logged(log):
    log.append(entry_in_state("submitted"))
    with pytest.raises(EscrowError):
        advance_milestone("m-1", "release", escrow=failing_release(EscrowError("reverted")))
    with pytest.raises(EscrowError):
        advance_milestone("m-1", "release", escrow=failing_release(EscrowError("timeout")))  # new error
    escrow = FakeEscrow()
    escrow.reject = MagicMock(side_effect=EscrowError("timeout"))
    with pytest.raises(EscrowError):
        advance_milestone("m-1", "reject", escrow=escrow)  # new action, same text
    assert len(log) == 4


def test_a_later_success_clears_the_failure_and_proceeds(log):
    log.append(entry_in_state("submitted"))
    with pytest.raises(EscrowError):
        advance_milestone("m-1", "release", escrow=failing_release(EscrowError("reverted")))
    assert log[-1].failed_attempt is not None

    entry = advance_milestone("m-1", "release", escrow=FakeEscrow())

    assert entry.failed_attempt is None
    assert (entry.escrow_status, entry.payment_executed) == ("released", True)


def test_state_refusals_are_not_logged_as_attempts(log):
    """Wrong state / unknown request / not-our-contractor never reached the escrow."""
    log.append(entry_in_state("released"))
    with pytest.raises(MilestoneError):
        advance_milestone("m-1", "release", escrow=FakeEscrow())
    with pytest.raises(MilestoneNotFoundError):
        advance_milestone("ghost", "release", escrow=FakeEscrow())

    log.append(entry_in_state("funded", "m-2"))
    with WITH_CIRCLE, pytest.raises(MilestoneError, match="must submit themselves"):
        advance_milestone("m-2", "deliver", escrow=FakeEscrow())

    assert len(log) == 2  # only the two entries we put there


def test_a_log_write_failure_never_hides_the_real_error(log):
    log.append(entry_in_state("submitted"))
    original = EscrowError("reverted")

    with patch("src.pipeline.append_entry", side_effect=OSError("disk full")):
        with pytest.raises(EscrowError) as raised:
            advance_milestone("m-1", "release", escrow=failing_release(original))

    assert raised.value is original  # not the OSError


def test_failed_attempts_keep_the_hash_chain_valid_and_round_trip(tmp_path):
    from src.audit.store import append_entry as real_append, read_all_entries
    from src.audit.verify import verify_chain

    path = str(tmp_path / "audit.jsonl")
    ok = entry_in_state("submitted")
    failed = ok.model_copy(update={"failed_attempt": FailedAttempt(action="release", error="reverted", tx_hash="0xbad")})
    for e in (ok, failed, ok.model_copy(update={"escrow_status": "released"})):
        real_append(e, path)

    assert verify_chain(path).ok
    assert read_all_entries(path)[1].failed_attempt.tx_hash == "0xbad"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_active_milestones_expose_the_last_failed_attempt(log):
    failed = entry_in_state("funded").model_copy(
        update={"failed_attempt": FailedAttempt(action="release", error="reverted", tx_hash="0xbad")}
    )
    log.append(failed)
    reader = MagicMock()
    reader.get_job.return_value = MagicMock(status="Funded", budget_usdc=100.0)

    with patch("src.api._chain_reader", return_value=reader):
        data = client.get("/api/milestones/active").json()

    assert data[0]["failed_attempt"] == {
        "action": "release", "error": "reverted", "transaction_id": None, "tx_hash": "0xbad",
    }


def test_stats_count_milestones_needing_attention(log):
    attempt = FailedAttempt(action="release", error="reverted")
    log.extend(
        [
            entry_in_state("funded", "a").model_copy(update={"failed_attempt": attempt}),
            entry_in_state("funded", "b"),
            entry_in_state("released", "c").model_copy(update={"failed_attempt": attempt}),  # resolved since
        ]
    )
    assert client.get("/api/stats").json()["milestones_needing_attention"] == 1


# --------------------------------------------------------------------------
# API (actions)
# --------------------------------------------------------------------------


def test_active_milestones_show_the_live_chain_status(log):
    log.extend([entry_in_state("funded", "m-1"), entry_in_state("released", "m-2")])
    job = MagicMock(status="Submitted", budget_usdc=100.0)
    reader = MagicMock()
    reader.get_job.return_value = job

    with patch("src.api._chain_reader", return_value=reader):
        data = client.get("/api/milestones/active").json()

    assert [d["request"]["request_id"] for d in data] == ["m-1"]  # released one is not active
    assert data[0]["chain_status"] == "Submitted" and data[0]["chain_budget_usdc"] == 100.0
    reader.get_job.assert_called_once_with(42)


def test_an_rpc_failure_does_not_hide_the_milestone(log):
    log.append(entry_in_state("funded"))
    reader = MagicMock()
    reader.get_job.side_effect = ConnectionError("rpc down")

    with patch("src.api._chain_reader", return_value=reader):
        data = client.get("/api/milestones/active").json()

    assert len(data) == 1
    assert data[0]["chain_status"] is None and "rpc down" in data[0]["chain_error"]


def test_no_active_milestones_never_touches_the_chain(log):
    with patch("src.api._chain_reader", side_effect=AssertionError("must not be called")):
        assert client.get("/api/milestones/active").json() == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [(MilestoneNotFoundError("nope"), 404), (MilestoneError("wrong state"), 409), (EscrowError("reverted"), 409)],
)
def test_milestone_action_maps_errors_to_http_codes(error, expected):
    with patch("src.api.advance_milestone", side_effect=error):
        response = client.post("/api/milestones/m-1/release", json={"reviewer": "ana"})
    assert response.status_code == expected
    assert response.json()["detail"] == str(error)


def test_milestone_action_passes_the_reviewer_through_and_returns_the_entry(log):
    log.append(entry_in_state("submitted"))
    with patch("src.pipeline.EscrowClient", return_value=FakeEscrow()):
        response = client.post("/api/milestones/m-1/release", json={"reviewer": "ana"})
    assert response.status_code == 200
    assert response.json()["escrow_status"] == "released" and response.json()["reviewer"] == "ana"


def test_unknown_action_is_rejected_by_validation():
    assert client.post("/api/milestones/m-1/steal", json={}).status_code == 422


def test_stats_separate_locked_funds_from_paid_funds(log):
    log.extend(
        [
            entry_in_state("funded", "a"),  # 100 locked
            entry_in_state("submitted", "b"),  # 100 locked
            entry_in_state("open", "c"),  # job open, nothing locked yet
        ]
    )
    released = entry_in_state("funded", "d")
    released.escrow_status, released.payment_status, released.payment_executed = "released", "executed", True
    log.append(released)

    data = client.get("/api/stats").json()

    assert data["escrowed_total_usdc"] == 200.0
    assert data["milestones_active"] == 3
    assert data["paid_total_usdc"] == 100.0  # only the released one
