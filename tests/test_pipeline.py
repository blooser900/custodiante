"""Tests that pipeline.run_pipeline wires the decision engine, policy layer,
and Arc payment execution together correctly -- with everything external
mocked (no real Jev/Kev server, no real Circle credentials needed).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.config import RulesConfig, ScoringCriteria, Thresholds
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.pipeline import run_pipeline
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds



def fake_rules_config() -> RulesConfig:
    return RulesConfig(
        category_limits=category_limits(operational_expense=500.0, other=200.0),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        # Payment-mechanics tests: an unseen payee would be escalated before the code under test runs.
        require_known_payee=False,
    )


@pytest.fixture(autouse=True)
def mock_config():
    config = fake_rules_config()
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        yield config


def make_request(counterparty_wallet: str | None) -> PaymentRequest:
    return PaymentRequest(
        request_id="main-test-1",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=100.0,
        requester="test-suite",
        description="A request used to test main.py wiring.",
        counterparty_wallet=counterparty_wallet,
    )


def make_high_trust_engine() -> MagicMock:
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.95, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    return engine


# An approved payment appends TWO entries: a `pending` row written before the
# money moves, then the outcome row (see pipeline._pending_entry). Tests that
# assert on "the" logged entry want the outcome, i.e. the last append.
def last_entry(append_mock):
    return append_mock.call_args[0][0]


def all_entries(append_mock):
    return [call_args[0][0] for call_args in append_mock.call_args_list]


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_with_wallet_executes_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {
        "transaction_id": "tx-123",
        "state": "COMPLETE",
        "tx_hash": "0xabc123",
        "destination_wallet": "0xdeadbeef",
        "amount_usdc": 100.0,
        "memo": "payment for request main-test-1",
    }

    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_called_once()
    _, kwargs = arc_client.send_payment.call_args
    assert kwargs["destination_wallet"] == "0xdeadbeef"
    assert kwargs["amount_usdc"] == 100.0

    logged_entry = last_entry(mock_append_entry)
    assert logged_entry.payment_executed is True
    # The whole point of this fix: transaction_id/tx_hash must actually
    # reach the audit log, not just the console print.
    assert logged_entry.transaction_id == "tx-123"
    assert logged_entry.tx_hash == "0xabc123"


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {}, clear=True)
def test_approved_request_without_credentials_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = last_entry(mock_append_entry)
    assert logged_entry.payment_executed is False
    assert logged_entry.transaction_id is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_approved_request_without_wallet_skips_payment(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()

    run_pipeline(make_request(None), engine=engine, arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    logged_entry = last_entry(mock_append_entry)
    assert logged_entry.payment_executed is False


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_failed_transfer_is_logged_not_raised(mock_append_entry):
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    # Should not raise -- a failed on-chain transfer is recorded, not fatal.
    run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    logged_entry = last_entry(mock_append_entry)
    assert logged_entry.payment_executed is False
    assert logged_entry.transaction_id is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_rejected_destination_address_is_logged_not_raised(mock_append_entry):
    """A malformed address or a self-transfer must not crash the pipeline --
    ArcPaymentClient raises ValueError for these (see _validate_destination),
    and main.py must catch that the same way it catches a failed transfer."""
    engine = make_high_trust_engine()
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = ValueError("not a well-formed EVM address")

    run_pipeline(make_request("not-a-real-address"), engine=engine, arc_client=arc_client)

    logged_entry = last_entry(mock_append_entry)
    assert logged_entry.payment_executed is False


# --------------------------------------------------------------------------
# Payment outcome is recorded in the audit entry (not just printed)
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_executed_payment_is_marked_executed(mock_append_entry):
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=arc_client)

    entry = last_entry(mock_append_entry)
    assert entry.payment_status == "executed"
    assert entry.payment_detail is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_failed_payment_records_status_and_reason(mock_append_entry):
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=arc_client)

    entry = last_entry(mock_append_entry)
    assert entry.payment_status == "failed"
    assert "transfer DENIED" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch("src.pipeline.ArcPaymentClient", side_effect=Exception("bad credentials"))
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_arc_client_construction_failure_is_recorded_not_raised(_mock_cls, mock_append_entry):
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine())

    entry = last_entry(mock_append_entry)
    assert entry.payment_status == "failed"
    assert "bad credentials" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_skipped_payment_says_why_for_missing_wallet(mock_append_entry):
    run_pipeline(make_request(None), engine=make_high_trust_engine(), arc_client=MagicMock())

    entry = last_entry(mock_append_entry)
    assert entry.payment_status == "skipped"
    assert "counterparty_wallet" in entry.payment_detail


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {}, clear=True)
def test_skipped_payment_says_why_for_missing_api_key(mock_append_entry):
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(), arc_client=MagicMock())

    entry = last_entry(mock_append_entry)
    assert entry.payment_status == "skipped"
    assert "CIRCLE_API_KEY" in entry.payment_detail


@patch("src.pipeline.append_entry")
def test_escalated_request_has_no_payment_applicable(mock_append_entry):
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.1, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )

    run_pipeline(make_request("0xdeadbeef"), engine=engine)

    entry = last_entry(mock_append_entry)
    assert entry.decision == "escalate"
    assert entry.payment_status == "not_applicable"


@patch("src.pipeline.append_entry")
def test_engine_name_is_recorded(mock_append_entry):
    engine = make_high_trust_engine()
    engine.name = "kev"

    run_pipeline(make_request(None), engine=engine)

    assert last_entry(mock_append_entry).engine_name == "kev"


# --------------------------------------------------------------------------
# Idempotency: a request is decided and paid at most once
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_resubmitting_a_processed_request_id_is_rejected_before_any_payment(mock_append_entry):
    from src.pipeline import DuplicateRequestError

    engine = make_high_trust_engine()
    arc_client = MagicMock()
    existing = MagicMock()
    existing.request.request_id = "main-test-1"
    # A finished request: no marker left behind, so the refusal is
    # "already processed" rather than "a payment is in flight".
    existing.payment_attempt = None
    existing.payment_status = "executed"

    with patch("src.pipeline.read_latest_per_request", return_value=[existing]):
        with pytest.raises(DuplicateRequestError):
            run_pipeline(make_request("0xdeadbeef"), engine=engine, arc_client=arc_client)

    engine.score.assert_not_called()
    arc_client.send_payment.assert_not_called()
    mock_append_entry.assert_not_called()


# --------------------------------------------------------------------------
# Crash recovery (SEV-02): the intent is recorded BEFORE the money moves
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_the_attempt_is_recorded_before_the_money_moves(mock_append_entry):
    """The whole point of the pending row: it exists before Circle is called.

    If it were written after, a crash mid-payment would leave no trace and a
    retry would pay again. Asserted by reading the log from inside the
    payment call, i.e. at the exact moment money is in flight.
    """
    arc_client = MagicMock()
    log: list = []
    mock_append_entry.side_effect = log.append

    seen_at_payment_time: list = []

    def send_payment(**_kwargs):
        seen_at_payment_time.extend(log)  # the log as it stands mid-payment
        return {"transaction_id": "tx-1", "tx_hash": "0x1"}

    arc_client.send_payment.side_effect = send_payment

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                 arc_client=arc_client, verbose=False)

    assert len(seen_at_payment_time) == 1, "the pending row must precede the payment"
    pending = seen_at_payment_time[0]
    assert pending.payment_attempt is not None
    assert pending.payment_amount_usdc == 100.0
    assert pending.payment_executed is False

    entries = all_entries(mock_append_entry)
    assert len(entries) == 2, "pending row, then the outcome row"
    confirmed = entries[-1]
    assert confirmed.payment_executed is True
    # The marker must be cleared by the outcome, or _in_flight reads forever.
    assert confirmed.payment_attempt is None
    assert confirmed.transaction_id == "tx-1"


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_a_crash_after_the_transfer_is_recorded_refuses_to_pay_again(mock_append_entry):
    """Reproduces the crash window: the transfer was sent, the outcome row
    never made it. A retry must refuse instead of paying a second time.
    """
    from src.pipeline import PaymentInFlightError

    log: list = []
    mock_append_entry.side_effect = log.append

    engine = make_high_trust_engine()
    arc_client = MagicMock()
    # Simulate the crash: the process dies while the money is in flight, so
    # only the pending row ever reached the log.
    arc_client.send_payment.side_effect = KeyboardInterrupt("process killed")

    with patch("src.pipeline.read_latest_per_request", side_effect=lambda: list(log)):
        with pytest.raises(KeyboardInterrupt):
            run_pipeline(make_request("0xdeadbeef"), engine=engine,
                         arc_client=arc_client, verbose=False)

        assert len(log) == 1 and log[0].payment_attempt is not None

        # The retry: same request_id, and the log still shows an unconfirmed
        # attempt. Paying now is exactly the double-spend.
        with pytest.raises(PaymentInFlightError):
            run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                         arc_client=arc_client, verbose=False)

    # Exactly one payment attempt reached the client across both runs.
    assert arc_client.send_payment.call_count == 1


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_an_unconfirmed_attempt_is_not_confused_with_a_finished_request(mock_append_entry):
    """In-flight and already-processed are distinct errors: the first says
    'reconcile', the second says 'use a new id'. Conflating them would tell an
    operator to resubmit a payment that may already be out the door.
    """
    from src.pipeline import DuplicateRequestError

    # A genuinely finished request: an outcome row with no marker.
    log: list = []
    mock_append_entry.side_effect = log.append
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}

    with patch("src.pipeline.read_latest_per_request", side_effect=lambda: list(log)):
        run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                     arc_client=arc_client, verbose=False)
        # The finished request's LATEST row carries no marker (the pending row
        # is superseded), so the error is "already processed".
        assert log[-1].payment_attempt is None

        with pytest.raises(DuplicateRequestError):
            run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                         arc_client=arc_client, verbose=False)

    assert arc_client.send_payment.call_count == 1


@patch("src.pipeline.append_entry")
def test_an_escalated_request_writes_one_row_and_no_attempt_marker(mock_append_entry):
    """Nothing moves when the decision is to escalate, so there is no in-flight
    window to protect: one row, no marker."""
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.1, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )

    run_pipeline(make_request("0xdeadbeef"), engine=engine, verbose=False)

    entries = all_entries(mock_append_entry)
    assert len(entries) == 1
    assert entries[0].payment_attempt is None
    assert entries[0].payment_status == "not_applicable"


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_a_failed_payment_still_clears_the_marker(mock_append_entry):
    """A failure is an outcome: it must clear the marker, or every later
    retry would be refused as 'in flight' even though nothing moved."""
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                 arc_client=arc_client, verbose=False)

    confirmed = last_entry(mock_append_entry)
    assert confirmed.payment_status == "failed"
    assert confirmed.payment_attempt is None


@patch("src.pipeline.append_entry")
@patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key"})
def test_concurrent_submissions_of_the_same_request_pay_once(mock_append_entry):
    """Simulates a double click: the real log is stood in for by a list that
    append_entry writes to, so the second thread sees the first one's entry
    only if the per-request lock really serializes check -> pay -> append.

    Either refusal is correct and means the same thing here (pay once):
    DuplicateRequestError if it arrived after the outcome row, or
    PaymentInFlightError if it arrived while the first was still paying.
    """
    import threading
    import time

    from src.pipeline import DuplicateRequestError, PaymentInFlightError

    log: list = []
    mock_append_entry.side_effect = log.append

    arc_client = MagicMock()

    def slow_payment(**_kwargs):
        time.sleep(0.2)  # wide window for the second thread to slip through
        return {"transaction_id": "tx-1", "tx_hash": "0x1"}

    arc_client.send_payment.side_effect = slow_payment

    results: list[str] = []

    def submit():
        try:
            run_pipeline(
                make_request("0xdeadbeef"),
                engine=make_high_trust_engine(),
                arc_client=arc_client,
                verbose=False,
            )
            results.append("ok")
        except (DuplicateRequestError, PaymentInFlightError):
            results.append("refused")

    with patch("src.pipeline.read_latest_per_request", side_effect=lambda: list(log)):
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(results) == ["ok", "refused"]
    assert arc_client.send_payment.call_count == 1


@patch("src.pipeline.append_entry")
def test_human_override_rejects_a_request_already_reviewed(mock_append_entry):
    from src.audit.logger import AuditEntry
    from src.pipeline import AlreadyReviewedError, apply_human_override

    scores = DecisionScores(
        trust=ScoreResult(value=0.1, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    base = dict(
        timestamp="2026-10-06T12:00:00+00:00",
        request=make_request("0xdeadbeef"),
        scores=scores,
        decision="escalate",
        reduced_amount=None,
        reason="r",
        payment_executed=False,
    )
    pending = AuditEntry(**base)
    already_reviewed = AuditEntry(**base, human_review="approved", reviewer="ana")

    # The caller holds a stale `pending` copy, but the log already shows a review.
    with patch("src.pipeline.read_latest_per_request", return_value=[already_reviewed]):
        with pytest.raises(AlreadyReviewedError):
            apply_human_override(pending, "approve", "joão", arc_client=MagicMock())

    mock_append_entry.assert_not_called()


# --------------------------------------------------------------------------
# History reaches the engine, and what the engine saw is logged
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
def test_the_engine_is_given_the_history_computed_from_the_log(mock_append_entry):
    from datetime import datetime, timezone

    from src.audit.logger import AuditEntry

    wallet = "0x" + "b" * 40
    previous = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=make_request(wallet).model_copy(update={"request_id": "earlier"}),
        scores=make_high_trust_engine().score.return_value,
        decision="approve", reduced_amount=None, reason="r",
        payment_executed=True, payment_status="executed",
    )
    engine = make_high_trust_engine()

    with patch("src.pipeline.read_latest_per_request", return_value=[previous]):
        run_pipeline(make_request(wallet), engine=engine, verbose=False)

    history = engine.score.call_args.kwargs["history"]
    assert history.destination.succeeded == 1 and history.destination.known is True
    assert history.requester.succeeded == 1  # same requester name


@patch("src.pipeline.append_entry")
def test_the_logged_entry_carries_the_answers_and_what_the_model_saw(mock_append_entry):
    from src.decision_engine.mock_engine import MockDecisionEngine

    run_pipeline(make_request("0x" + "b" * 40), engine=MockDecisionEngine(), verbose=False)

    scores = last_entry(mock_append_entry).scores
    assert "fraud_indicators" in scores.extra
    assert scores.evidence.state["history"]["destination"]["identity_basis"] == "wallet_address"
    assert "trust" in scores.evidence.answers


# --------------------------------------------------------------------------
# The submission timestamp, so the engine can see the hour of day
# --------------------------------------------------------------------------


@patch("src.pipeline.append_entry")
def test_the_pipeline_stamps_when_the_request_arrived(mock_append_entry):
    """Nothing else records when the request happened, and the hour matters.

    A transfer requested at 03:00 is a different animal from the same amount at
    10:45, and before this the state carried no time at all.
    """
    from datetime import datetime, timezone

    from src.decision_engine.mock_engine import MockDecisionEngine

    before = datetime.now(timezone.utc)
    run_pipeline(make_request("0x" + "b" * 40), engine=MockDecisionEngine(), verbose=False)

    entry = last_entry(mock_append_entry)
    stamped = entry.request.submitted_at
    assert stamped is not None, "the pipeline must stamp submitted_at"
    assert stamped.tzinfo is not None, "a naive timestamp cannot be placed in a timezone"
    assert before <= stamped <= datetime.now(timezone.utc)

    state = entry.scores.evidence.state
    assert state["submitted_at"] is not None
    assert state["local_hour"] is not None and len(state["local_hour"]) == 5  # "HH:MM"
    assert state["utc_offset"] is not None  # e.g. "-0300": the hour is meaningless without it


@patch("src.pipeline.append_entry")
def test_a_caller_supplied_timestamp_is_respected(mock_append_entry):
    """Demo seeds and calibration scenarios pin the time to stay reproducible."""
    from datetime import datetime, timezone

    from src.decision_engine.mock_engine import MockDecisionEngine

    fixed = datetime(2026, 10, 3, 3, 14, tzinfo=timezone.utc)  # 03:14 UTC, a suspicious hour
    request = make_request("0x" + "b" * 40).model_copy(update={"submitted_at": fixed})

    run_pipeline(request, engine=MockDecisionEngine(), verbose=False)

    entry = last_entry(mock_append_entry)
    assert entry.request.submitted_at == fixed
    assert entry.scores.evidence.state["submitted_at"] == fixed.isoformat()


@patch("src.pipeline.append_entry")
def test_the_engine_sees_a_naive_timestamp_as_utc_not_local(mock_append_entry):
    """A naive timestamp is ambiguous; reading it as local would be a guess,
    and a wrong guess here silently mislabels the signal."""
    from datetime import datetime, timezone

    from src.decision_engine.mock_engine import MockDecisionEngine

    naive = datetime(2026, 10, 3, 3, 14)
    request = make_request("0x" + "b" * 40).model_copy(update={"submitted_at": naive})

    run_pipeline(request, engine=MockDecisionEngine(), verbose=False)

    state = last_entry(mock_append_entry).scores.evidence.state
    assert state["submitted_at"] == naive.replace(tzinfo=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Human approval writes the in-flight marker too (review R2-01, SEV-03)
# --------------------------------------------------------------------------


def make_low_trust_engine() -> MagicMock:
    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.2, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    return engine


class FakeLog:
    """An in-memory stand-in for the audit log with the semantics that matter:
    rows are appended in order and `read_latest_per_request` returns the last
    row of each request. (Patching `append_entry`/`read_latest_per_request`
    separately would let a test invent states the real log never reaches.)"""

    def __init__(self):
        self.rows: list = []
        self.fail_on_append: int | None = None  # 1-based index of the append that "crashes"
        self.appends = 0

    def append(self, entry, *_a, **_k):
        self.appends += 1
        if self.fail_on_append == self.appends:
            raise KeyboardInterrupt("process killed while writing the log")
        self.rows.append(entry)

    def latest(self):
        by_id = {}
        for row in self.rows:
            by_id[row.request.request_id] = row
        return list(by_id.values())


@pytest.fixture
def fake_log():
    log = FakeLog()
    with (
        patch("src.pipeline.append_entry", side_effect=log.append),
        patch("src.pipeline.read_latest_per_request", side_effect=log.latest),
        patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key", "ESCROW_ENABLED": "false"}),
    ):
        yield log


def escalate(fake_log):
    """Run a request that the policy escalates, and return its entry."""
    entry = run_pipeline(make_request("0xdeadbeef"), engine=make_low_trust_engine(),
                         arc_client=MagicMock(), verbose=False)
    assert entry.decision == "escalate" and len(fake_log.rows) == 1
    return entry


def test_a_human_approval_records_the_attempt_before_the_money_moves(fake_log):
    from src.pipeline import apply_human_override

    escalated = escalate(fake_log)
    arc_client = MagicMock()
    seen_at_payment_time: list = []

    def send_payment(**_kwargs):
        seen_at_payment_time.extend(fake_log.rows)
        return {"transaction_id": "tx-1", "tx_hash": "0x1"}

    arc_client.send_payment.side_effect = send_payment

    final = apply_human_override(escalated, "approve", "ana", arc_client=arc_client)

    # The row that existed mid-payment is the pending one, with the marker.
    assert len(seen_at_payment_time) == 2, "escalated row + the pending row"
    pending = seen_at_payment_time[-1]
    assert pending.payment_attempt is not None
    assert pending.payment_executed is False
    assert pending.human_review == "approved" and pending.reviewer == "ana"
    assert pending.payment_amount_usdc == 100.0
    # And the outcome replaces it, clearing the marker.
    assert final.payment_executed is True
    assert final.payment_attempt is None
    assert final.transaction_id == "tx-1"
    assert fake_log.rows[-1] is final


def test_a_crash_after_a_human_approval_paid_refuses_a_second_payment(fake_log):
    """The bug itself. The transfer goes out, the process dies before the
    outcome row is written, the manager approves again. Before the fix the
    second approval found no trace of the first and paid again."""
    from src.pipeline import PaymentInFlightError, apply_human_override

    escalated = escalate(fake_log)
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}

    # Appends so far: 1 (the escalation). The approval's pending row is #2,
    # its outcome row would be #3 -- that is the write that never happens.
    fake_log.fail_on_append = 3
    with pytest.raises(KeyboardInterrupt):
        apply_human_override(escalated, "approve", "ana", arc_client=arc_client)
    assert arc_client.send_payment.call_count == 1
    assert fake_log.latest()[0].payment_attempt is not None

    fake_log.fail_on_append = None
    with pytest.raises(PaymentInFlightError):
        apply_human_override(escalated, "approve", "ana", arc_client=arc_client)

    assert arc_client.send_payment.call_count == 1, "the second approval must not pay"


def test_a_human_denial_still_writes_a_single_row_and_pays_nothing(fake_log):
    from src.pipeline import apply_human_override

    escalated = escalate(fake_log)
    arc_client = MagicMock()
    final = apply_human_override(escalated, "deny", "ana", arc_client=arc_client)

    assert final.human_review == "denied" and final.decision == "escalate"
    assert final.payment_attempt is None
    assert len(fake_log.rows) == 2, "the escalation, then one denial row"
    arc_client.send_payment.assert_not_called()


def test_a_failed_human_approval_payment_is_final_and_explains_itself(fake_log):
    from src.pipeline import apply_human_override

    escalated = escalate(fake_log)
    arc_client = MagicMock()
    arc_client.send_payment.side_effect = RuntimeError("transfer DENIED")

    final = apply_human_override(escalated, "approve", "ana", arc_client=arc_client)

    assert final.payment_status == "failed"
    assert final.payment_attempt is None, "a definite failure is not in flight"
    assert "transfer DENIED" in final.reason


# --------------------------------------------------------------------------
# An unconfirmed transfer is not a failure (review R2-02)
# --------------------------------------------------------------------------


def unconfirmed_arc(transaction_id="tx-77"):
    from src.payments.arc_client import PaymentUnconfirmedError

    arc_client = MagicMock()
    arc_client.send_payment.side_effect = PaymentUnconfirmedError(
        "still SENT after 60s", transaction_id=transaction_id, state="SENT"
    )
    return arc_client


def test_an_unconfirmed_transfer_keeps_the_marker_and_the_transaction_id(fake_log):
    entry = run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                         arc_client=unconfirmed_arc(), verbose=False)

    assert entry.payment_status == "unconfirmed"
    assert entry.payment_executed is False
    assert entry.payment_attempt is not None, "still in flight: do not clear the marker"
    assert entry.transaction_id == "tx-77", "the id must be structured, not buried in text"
    assert "PaymentUnconfirmedError" in entry.payment_detail


def test_an_unconfirmed_transfer_blocks_a_retry_of_the_same_request(fake_log):
    from src.pipeline import PaymentInFlightError

    arc_client = unconfirmed_arc()
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                 arc_client=arc_client, verbose=False)

    with pytest.raises(PaymentInFlightError):
        run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                     arc_client=arc_client, verbose=False)
    assert arc_client.send_payment.call_count == 1


def test_an_unconfirmed_human_approval_blocks_a_second_click(fake_log):
    from src.pipeline import PaymentInFlightError, apply_human_override

    escalated = escalate(fake_log)
    arc_client = unconfirmed_arc()
    final = apply_human_override(escalated, "approve", "ana", arc_client=arc_client)
    assert final.payment_status == "unconfirmed" and final.payment_attempt is not None

    with pytest.raises(PaymentInFlightError):
        apply_human_override(escalated, "approve", "ana", arc_client=arc_client)
    assert arc_client.send_payment.call_count == 1


def test_a_definite_failure_is_still_a_failure_not_unconfirmed(fake_log):
    entry = run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                         arc_client=MagicMock(send_payment=MagicMock(side_effect=RuntimeError("DENIED"))),
                         verbose=False)
    assert entry.payment_status == "failed"
    assert entry.payment_attempt is None


def test_the_transfer_is_sent_with_a_key_derived_from_the_request_id(fake_log):
    from src.payments.arc_client import payment_idempotency_key

    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}
    run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                 arc_client=arc_client, verbose=False)

    _, kwargs = arc_client.send_payment.call_args
    assert kwargs["idempotency_key"] == payment_idempotency_key("main-test-1")
    assert kwargs["ref_id"] == "main-test-1"


# --------------------------------------------------------------------------
# Getting an approved payment out of a stuck state (review R2-03)
# --------------------------------------------------------------------------


def failing_arc(message="DENIED by Circle"):
    return MagicMock(send_payment=MagicMock(side_effect=RuntimeError(message)))


def paid_arc(transaction_id="tx-ok"):
    return MagicMock(send_payment=MagicMock(return_value={"transaction_id": transaction_id, "tx_hash": "0xok"}))


def lookup_arc(found, **kwargs):
    """An arc client whose Circle lookup answers `found` (a dict, or None)."""
    arc_client = paid_arc()
    arc_client.find_transfer.return_value = found
    for key, value in kwargs.items():
        setattr(arc_client, key, value)
    return arc_client


def approve_and_get_stuck(fake_log, arc_client):
    """An auto-approved request whose payment ended up in the given state."""
    entry = run_pipeline(make_request("0xdeadbeef"), engine=make_high_trust_engine(),
                         arc_client=arc_client, verbose=False)
    assert entry.decision == "approve"
    return entry


# -- reconcile --------------------------------------------------------------


def test_reconcile_records_a_transfer_circle_says_completed(fake_log):
    from src.pipeline import reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    arc_client = lookup_arc({"transaction_id": "tx-77", "state": "COMPLETE", "tx_hash": "0xdone", "error_reason": None})

    result = reconcile_payment("main-test-1", reviewer="ana", arc_client=arc_client)

    assert result.changed and result.observed == "COMPLETE"
    entry = result.entry
    assert entry.payment_status == "executed" and entry.payment_executed is True
    assert entry.payment_attempt is None, "no longer in flight"
    assert entry.tx_hash == "0xdone" and entry.transaction_id == "tx-77"
    assert "Reconciled against Circle by ana" in entry.reason
    assert fake_log.rows[-1] is entry
    arc_client.send_payment.assert_not_called()  # reconciling never sends money


def test_reconcile_looks_the_transfer_up_by_id_when_it_has_one(fake_log):
    from src.pipeline import reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    arc_client = lookup_arc({"transaction_id": "tx-77", "state": "SENT", "tx_hash": None, "error_reason": None})
    reconcile_payment("main-test-1", arc_client=arc_client)

    kwargs = arc_client.find_transfer.call_args.kwargs
    assert kwargs["transaction_id"] == "tx-77"
    assert kwargs["ref_id"] == "main-test-1"


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "DENIED"])
def test_reconcile_turns_a_transfer_circle_says_failed_into_a_retryable_failure(fake_log, state):
    from src.pipeline import reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    arc_client = lookup_arc({"transaction_id": "tx-77", "state": state, "tx_hash": None, "error_reason": "INSUFFICIENT_TOKEN"})

    result = reconcile_payment("main-test-1", arc_client=arc_client)

    assert result.changed
    assert result.entry.payment_status == "failed"
    assert result.entry.payment_attempt is None
    assert state in result.entry.payment_detail and "INSUFFICIENT_TOKEN" in result.entry.payment_detail


def test_reconcile_records_nothing_while_the_transfer_is_still_in_progress(fake_log):
    from src.pipeline import reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    rows_before = len(fake_log.rows)

    result = reconcile_payment("main-test-1", arc_client=lookup_arc(
        {"transaction_id": "tx-77", "state": "SENT", "tx_hash": None, "error_reason": None}))

    assert result.changed is False and result.observed == "SENT"
    assert len(fake_log.rows) == rows_before, "pressing the button again must not fill the log"
    assert result.entry.payment_attempt is not None, "still in flight"


def test_reconcile_records_nothing_when_circle_has_no_record(fake_log):
    from src.pipeline import reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc(None))
    rows_before = len(fake_log.rows)

    result = reconcile_payment("main-test-1", arc_client=lookup_arc(None))

    assert result.changed is False and result.observed == "not_found"
    assert len(fake_log.rows) == rows_before
    assert fake_log.latest()[0].payment_attempt is not None


def test_reconcile_leaves_everything_alone_when_circle_cannot_be_asked(fake_log):
    from src.pipeline import PaymentLookupError, reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    rows_before = len(fake_log.rows)
    arc_client = MagicMock()
    arc_client.find_transfer.side_effect = RuntimeError("page full, match could be on the next page")

    with pytest.raises(PaymentLookupError, match="next page"):
        reconcile_payment("main-test-1", arc_client=arc_client)
    assert len(fake_log.rows) == rows_before


def test_reconcile_resolves_the_row_a_crash_left_behind(fake_log):
    """The pending row of a human approval whose process died mid-payment has no
    transaction id at all: Circle is searched by the request id instead."""
    from src.pipeline import PaymentInFlightError, apply_human_override, reconcile_payment

    escalated = escalate(fake_log)
    fake_log.fail_on_append = 3
    with pytest.raises(KeyboardInterrupt):
        apply_human_override(escalated, "approve", "ana", arc_client=paid_arc())
    fake_log.fail_on_append = None
    assert fake_log.latest()[0].transaction_id is None

    arc_client = lookup_arc({"transaction_id": "tx-9", "state": "COMPLETE", "tx_hash": "0x9", "error_reason": None})
    result = reconcile_payment("main-test-1", reviewer="ana", arc_client=arc_client)

    assert arc_client.find_transfer.call_args.kwargs["transaction_id"] is None
    assert result.entry.payment_status == "executed" and result.entry.tx_hash == "0x9"
    assert result.entry.human_review == "approved", "the human approval is carried over"


def test_reconcile_refuses_what_is_not_in_flight_or_does_not_exist(fake_log):
    from src.pipeline import PaymentNotFoundError, PaymentStateError, reconcile_payment

    approve_and_get_stuck(fake_log, paid_arc())
    with pytest.raises(PaymentStateError, match="no payment in flight"):
        reconcile_payment("main-test-1", arc_client=MagicMock())
    with pytest.raises(PaymentNotFoundError):
        reconcile_payment("nope", arc_client=MagicMock())


def test_reconcile_refuses_escrow_payments(fake_log):
    from src.pipeline import PaymentStateError, reconcile_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    with patch("src.pipeline.escrow_enabled", return_value=True), \
         patch("src.pipeline._uses_escrow", return_value=True):
        with pytest.raises(PaymentStateError, match="escrow"):
            reconcile_payment("main-test-1", arc_client=MagicMock())


# -- retry ------------------------------------------------------------------


def test_retry_after_a_failure_pays_with_a_new_idempotency_key(fake_log):
    from src.payments.arc_client import payment_idempotency_key
    from src.pipeline import retry_payment

    failed = approve_and_get_stuck(fake_log, failing_arc())
    assert failed.payment_status == "failed"
    arc_client = paid_arc("tx-2")

    entry = retry_payment("main-test-1", reviewer="ana", arc_client=arc_client)

    kwargs = arc_client.send_payment.call_args.kwargs
    assert kwargs["idempotency_key"] == payment_idempotency_key("main-test-1", 2)
    assert kwargs["idempotency_key"] != payment_idempotency_key("main-test-1", 1)
    assert kwargs["amount_usdc"] == 100.0
    assert entry.payment_status == "executed" and entry.attempt_number == 2
    assert "retried by ana (attempt 2)" in entry.reason
    assert entry.payment_attempt is None


def test_each_retry_after_a_failure_gets_its_own_attempt_number(fake_log):
    from src.pipeline import retry_payment

    approve_and_get_stuck(fake_log, failing_arc())
    second = retry_payment("main-test-1", arc_client=failing_arc())
    third = retry_payment("main-test-1", arc_client=paid_arc())

    assert second.payment_status == "failed" and second.attempt_number == 2
    assert third.payment_status == "executed" and third.attempt_number == 3


def test_a_retry_records_the_attempt_before_the_money_moves(fake_log):
    from src.pipeline import retry_payment

    approve_and_get_stuck(fake_log, failing_arc())
    seen: list = []
    arc_client = paid_arc()
    arc_client.send_payment.side_effect = lambda **_k: (
        seen.append(fake_log.latest()[0]) or {"transaction_id": "tx-2", "tx_hash": "0x2"}
    )

    retry_payment("main-test-1", arc_client=arc_client)

    assert seen[0].payment_attempt is not None and seen[0].attempt_number == 2


def test_a_crash_during_a_retry_refuses_the_next_retry(fake_log):
    """A retry is a payment like any other: if the process dies after the
    transfer went out, the next retry must find the marker, not pay again."""
    from src.pipeline import PaymentInFlightError, retry_payment

    approve_and_get_stuck(fake_log, failing_arc())
    arc_client = paid_arc()
    # Appends so far: pending + failed outcome. The retry's pending is #3, its
    # outcome #4 -- the write that never happens.
    fake_log.fail_on_append = 4
    with pytest.raises(KeyboardInterrupt):
        retry_payment("main-test-1", arc_client=arc_client)
    fake_log.fail_on_append = None

    with pytest.raises(PaymentInFlightError):
        retry_payment("main-test-1", arc_client=arc_client)
    assert arc_client.send_payment.call_count == 1


def test_retry_after_skipped_keeps_the_first_attempt_number(fake_log):
    from src.payments.arc_client import payment_idempotency_key
    from src.pipeline import retry_payment

    with patch.dict(os.environ, {"CIRCLE_API_KEY": ""}):
        skipped = approve_and_get_stuck(fake_log, MagicMock())
    assert skipped.payment_status == "skipped"

    arc_client = paid_arc()
    entry = retry_payment("main-test-1", arc_client=arc_client)

    assert arc_client.send_payment.call_args.kwargs["idempotency_key"] == payment_idempotency_key("main-test-1", 1)
    assert entry.payment_status == "executed" and entry.attempt_number == 1


def test_a_human_approved_payment_can_be_retried_too(fake_log):
    from src.pipeline import apply_human_override, retry_payment

    escalated = escalate(fake_log)
    approved = apply_human_override(escalated, "approve", "ana", arc_client=failing_arc())
    assert approved.payment_status == "failed" and approved.human_review == "approved"

    entry = retry_payment("main-test-1", reviewer="bruno", arc_client=paid_arc())
    assert entry.payment_status == "executed" and entry.human_review == "approved"


def test_retry_refuses_an_unconfirmed_payment_by_default(fake_log):
    from src.pipeline import PaymentInFlightError, retry_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    arc_client = paid_arc()
    with pytest.raises(PaymentInFlightError, match="Reconcile"):
        retry_payment("main-test-1", arc_client=arc_client)
    arc_client.send_payment.assert_not_called()


def test_resend_is_refused_when_circle_already_has_the_transfer(fake_log):
    from src.pipeline import PaymentStateError, retry_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc("tx-77"))
    arc_client = lookup_arc({"transaction_id": "tx-77", "state": "SENT", "tx_hash": None, "error_reason": None})

    with pytest.raises(PaymentStateError, match="already has a transfer"):
        retry_payment("main-test-1", arc_client=arc_client, resend_unconfirmed=True)
    arc_client.send_payment.assert_not_called()


def test_resend_is_refused_when_circle_cannot_be_asked(fake_log):
    from src.pipeline import PaymentLookupError, retry_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc(None))
    arc_client = paid_arc()
    arc_client.find_transfer.side_effect = RuntimeError("503")

    with pytest.raises(PaymentLookupError):
        retry_payment("main-test-1", arc_client=arc_client, resend_unconfirmed=True)
    arc_client.send_payment.assert_not_called()


def test_resend_with_no_record_in_circle_reuses_the_same_key(fake_log):
    """Circle has no record: send again, but with the SAME key, so that if the
    search missed an existing transfer Circle answers with it, not a second one."""
    from src.payments.arc_client import payment_idempotency_key
    from src.pipeline import retry_payment

    approve_and_get_stuck(fake_log, unconfirmed_arc(None))
    arc_client = lookup_arc(None)

    entry = retry_payment("main-test-1", reviewer="ana", arc_client=arc_client, resend_unconfirmed=True)

    assert arc_client.send_payment.call_args.kwargs["idempotency_key"] == payment_idempotency_key("main-test-1", 1)
    assert entry.payment_status == "executed" and entry.attempt_number == 1


def test_retry_refuses_what_is_already_paid_or_was_never_approved(fake_log):
    from src.pipeline import PaymentStateError, retry_payment

    approve_and_get_stuck(fake_log, paid_arc())
    with pytest.raises(PaymentStateError, match="already paid"):
        retry_payment("main-test-1", arc_client=paid_arc())

    escalate_log = FakeLog()
    with patch("src.pipeline.append_entry", side_effect=escalate_log.append), \
         patch("src.pipeline.read_latest_per_request", side_effect=escalate_log.latest):
        run_pipeline(make_request("0xdeadbeef").model_copy(update={"request_id": "esc-1"}),
                     engine=make_low_trust_engine(), arc_client=MagicMock(), verbose=False)
        with pytest.raises(PaymentStateError, match="not an approved payment"):
            retry_payment("esc-1", arc_client=paid_arc())


def test_retry_refuses_a_request_with_no_wallet_and_an_unknown_request(fake_log):
    from src.pipeline import PaymentNotFoundError, PaymentStateError, retry_payment

    run_pipeline(make_request(None), engine=make_high_trust_engine(), arc_client=MagicMock(), verbose=False)
    with pytest.raises(PaymentStateError, match="no counterparty_wallet"):
        retry_payment("main-test-1", arc_client=paid_arc())
    with pytest.raises(PaymentNotFoundError):
        retry_payment("nope", arc_client=paid_arc())


# --------------------------------------------------------------------------
# An amount that cannot be paid never enters the pipeline (review R2-04)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [0.1 + 0.2, 1.0000001, float("inf")])
def test_an_unpayable_amount_is_refused_before_scoring_or_logging(fake_log, amount):
    from src.money import InvalidAmountError

    engine = make_high_trust_engine()
    request = make_request("0xdeadbeef").model_copy(update={"amount_usdc": amount})

    with pytest.raises(InvalidAmountError):
        run_pipeline(request, engine=engine, arc_client=MagicMock(), verbose=False)

    engine.score.assert_not_called()
    assert fake_log.rows == [], "a request that could never be paid is not logged either"
