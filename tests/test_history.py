"""Tests for the history features computed from the audit log."""

from datetime import datetime, timedelta, timezone

import pytest

from src.audit.logger import AuditEntry
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.history import compute_history
from src.requests.schema import PaymentRequest, RequestCategory

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
WALLET = "0x" + "b" * 40
OTHER_WALLET = "0x" + "c" * 40

SCORES = DecisionScores(
    trust=ScoreResult(value=0.5, confidence=0.8),
    urgency=ScoreResult(value=0.5, confidence=0.8),
    importance=ScoreResult(value=0.5, confidence=0.8),
)


def entry(request_id="r", requester="Setor de TI", wallet=WALLET, amount=100.0, days_ago=1.0, **fields) -> AuditEntry:
    return AuditEntry(
        timestamp=(NOW - timedelta(days=days_ago)).isoformat(),
        request=PaymentRequest(
            request_id=request_id, category=RequestCategory.OPERATIONAL_EXPENSE, amount_usdc=amount,
            requester=requester, description="d", counterparty_wallet=wallet,
        ),
        scores=SCORES,
        decision=fields.pop("decision", "approve"),
        reduced_amount=fields.pop("reduced_amount", None),
        reason="r",
        payment_executed=fields.pop("payment_executed", False),
        **fields,
    )


def paid(**kw):
    return entry(payment_executed=True, payment_status="executed", **kw)


def current(amount=100.0, requester="Setor de TI", wallet=WALLET) -> PaymentRequest:
    return PaymentRequest(
        request_id="now", category=RequestCategory.OPERATIONAL_EXPENSE, amount_usdc=amount,
        requester=requester, description="d", counterparty_wallet=wallet,
    )


# --------------------------------------------------------------------------
# No history
# --------------------------------------------------------------------------


def test_an_empty_log_means_unknown_not_good_or_bad():
    h = compute_history(current(), [], NOW)

    for party in (h.requester, h.destination):
        assert party.known is False and party.attempts == 0 and party.first_seen_days is None
        assert party.reliability_index == 0.5  # no evidence either way
        assert party.avg_amount_usdc is None
    assert (h.amount_vs_requester_avg, h.amount_vs_destination_avg) == (None, None)


def test_a_request_without_a_wallet_has_no_destination_history():
    h = compute_history(current(wallet=None), [paid()], NOW)
    assert h.destination is None and h.amount_vs_destination_avg is None


def test_the_identity_basis_is_labelled_so_weak_evidence_is_visible():
    h = compute_history(current(), [], NOW)
    assert h.requester.identity_basis == "self_declared"
    assert h.destination.identity_basis == "wallet_address"


# --------------------------------------------------------------------------
# Classifying outcomes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "counted_as"),
    [
        (dict(payment_executed=True, payment_status="executed"), "succeeded"),
        (dict(escrow_status="released", escrow_job_id=1, payment_executed=True), "succeeded"),
        (dict(escrow_status="released", escrow_job_id=1), "succeeded"),
        (dict(payment_status="failed"), "failed"),
        (dict(escrow_status="rejected", escrow_job_id=1, payment_status="refunded"), "refunded"),
        (dict(decision="escalate", human_review="denied", reviewer="ana"), "denied"),
    ],
)
def test_each_outcome_is_counted_in_its_own_bucket(fields, counted_as):
    party = compute_history(current(), [entry(**fields)], NOW).destination
    assert party.attempts == 1
    assert getattr(party, counted_as) == 1
    assert sum([party.succeeded, party.failed, party.refunded, party.denied]) == 1


@pytest.mark.parametrize(
    "fields",
    [
        dict(decision="escalate"),  # waiting for a human
        dict(payment_status="skipped"),  # approved, never attempted
        dict(escrow_status="open", escrow_job_id=1, payment_status="escrow_open"),
        dict(escrow_status="funded", escrow_job_id=1, payment_status="escrowed"),
        dict(escrow_status="submitted", escrow_job_id=1, payment_status="escrowed"),
    ],
)
def test_requests_still_in_flight_have_no_outcome_yet(fields):
    party = compute_history(current(), [entry(**fields)], NOW).destination
    assert (party.attempts, party.pending) == (0, 1)
    assert party.reliability_index == 0.5  # an unfinished payment proves nothing


def test_a_failed_escrow_action_does_not_count_against_anyone():
    """failed_attempt only annotates an escrow that is still intact."""
    from src.audit.logger import FailedAttempt

    e = entry(escrow_status="funded", escrow_job_id=1, payment_status="escrowed",
              failed_attempt=FailedAttempt(action="release", error="reverted"))
    assert compute_history(current(), [e], NOW).destination.failed == 0


# --------------------------------------------------------------------------
# The reliability index
# --------------------------------------------------------------------------


def test_one_lucky_payment_cannot_buy_a_high_index():
    one = compute_history(current(), [paid(request_id="a")], NOW).destination
    assert one.reliability_index == pytest.approx(0.667, abs=1e-3)  # (1+1)/(1+2)


def test_a_long_clean_record_earns_a_high_index():
    entries = [paid(request_id=f"r{i}") for i in range(10)]
    assert compute_history(current(), entries, NOW).destination.reliability_index == pytest.approx(0.917, abs=1e-3)


def test_bad_outcomes_pull_the_index_down():
    entries = [paid(request_id="a"), paid(request_id="b"), entry(request_id="c", payment_status="failed"),
               entry(request_id="d", escrow_status="rejected", escrow_job_id=1)]
    assert compute_history(current(), entries, NOW).destination.reliability_index == pytest.approx(0.5)  # (2+1)/(4+2)


def test_the_index_always_stays_inside_zero_one():
    for entries in ([entry(request_id=str(i), payment_status="failed") for i in range(50)],
                    [paid(request_id=str(i)) for i in range(50)]):
        index = compute_history(current(), entries, NOW).destination.reliability_index
        assert 0 < index < 1


# --------------------------------------------------------------------------
# Whose history is it
# --------------------------------------------------------------------------


def test_destination_history_is_keyed_by_wallet_ignoring_case_and_spaces():
    entries = [paid(request_id="a", wallet=WALLET.upper().replace("0X", "0x")), paid(request_id="b", wallet=OTHER_WALLET)]
    h = compute_history(current(wallet=f"  {WALLET}  "), entries, NOW)
    assert h.destination.succeeded == 1


def test_requester_history_is_keyed_by_name_ignoring_case_and_spaces():
    entries = [paid(request_id="a", requester="  setor de ti "), paid(request_id="b", requester="Financeiro")]
    h = compute_history(current(requester="SETOR DE TI"), entries, NOW)
    assert h.requester.succeeded == 1


def test_the_two_histories_are_independent():
    """A trusted wallet reached by a stranger, and a known requester paying a new wallet."""
    entries = [paid(request_id="a", requester="Financeiro", wallet=WALLET)]

    stranger = compute_history(current(requester="Visitante"), entries, NOW)
    assert stranger.requester.known is False and stranger.destination.succeeded == 1

    known_new_wallet = compute_history(current(requester="Financeiro", wallet=OTHER_WALLET), entries, NOW)
    assert known_new_wallet.requester.succeeded == 1 and known_new_wallet.destination.known is False


# --------------------------------------------------------------------------
# Time, amounts, ratios
# --------------------------------------------------------------------------


def test_first_seen_is_the_age_of_the_oldest_record_in_days():
    entries = [paid(request_id="a", days_ago=30), paid(request_id="b", days_ago=2)]
    assert compute_history(current(), entries, NOW).destination.first_seen_days == 30.0


def test_only_bad_outcomes_inside_the_last_day_count_as_recent():
    entries = [
        entry(request_id="old", payment_status="failed", days_ago=3),
        entry(request_id="new", payment_status="failed", days_ago=0.2),
        entry(request_id="den", decision="escalate", human_review="denied", days_ago=0.5),
        paid(request_id="ok", days_ago=0.1),  # good outcomes are not "bad"
    ]
    party = compute_history(current(), entries, NOW).destination
    assert (party.failed, party.recent_bad_outcomes_24h) == (2, 2)


def test_the_average_uses_money_that_actually_moved_and_the_reduced_amount():
    entries = [
        paid(request_id="a", amount=100.0),
        paid(request_id="b", amount=400.0, decision="approve_reduced", reduced_amount=200.0),
        entry(request_id="c", amount=9000.0, payment_status="failed"),  # never paid: ignored
    ]
    party = compute_history(current(amount=450.0), entries, NOW).destination
    assert party.avg_amount_usdc == 150.0  # (100 + 200) / 2


def test_the_ratio_compares_this_amount_to_the_usual_one():
    entries = [paid(request_id="a", amount=150.0)]
    h = compute_history(current(amount=450.0), entries, NOW)
    assert h.amount_vs_destination_avg == 3.0 and h.amount_vs_requester_avg == 3.0


def test_timestamps_without_a_timezone_are_treated_as_utc():
    e = paid(request_id="a")
    e.timestamp = (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()
    assert compute_history(current(), [e], NOW).destination.first_seen_days == 2.0


def test_the_features_serialize_to_plain_json_for_the_engine():
    import json

    h = compute_history(current(), [paid()], NOW)
    assert json.loads(json.dumps(h.model_dump(mode="json")))["destination"]["succeeded"] == 1
