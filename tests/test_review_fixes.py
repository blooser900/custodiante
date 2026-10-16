"""Regressions for the four findings of the priority review (A3, A4, A5, A9).

Each test below failed against the code as it stood, for the reason named in
its docstring; they exist so the same defect cannot come back quietly. The
review that produced them is `docs/revisao-priorizada.md`; the item ids (A3,
A4, A5, A9) are the ones used while triaging it.

They are grouped here rather than scattered into the existing files because
they share one property: they were found by reading the code and then
confirmed by execution, not by a test suite that was already green.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.config import RulesConfig, ScoringCriteria, load_config
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.pipeline import apply_human_override, run_pipeline
from src.requests.schema import PaymentRequest, RequestCategory
from tests.rules_fixtures import category_limits, thresholds


def fake_rules_config() -> RulesConfig:
    """The payment-mechanics config the rest of the suite uses."""
    return RulesConfig(
        category_limits=category_limits(),
        thresholds=thresholds(),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        # An unseen payee would be escalated before the code under test runs.
        require_known_payee=False,
    )


@pytest.fixture
def mock_config():
    config = fake_rules_config()
    with (
        patch("src.policy.rules.load_config", return_value=config),
        patch("src.policy.limits.load_config", return_value=config),
    ):
        yield config


def make_request(wallet: str | None = "0x" + "ab" * 20, amount: float = 100.0) -> PaymentRequest:
    return PaymentRequest(
        request_id="review-fix-1",
        category=RequestCategory.OPERATIONAL_EXPENSE,
        amount_usdc=amount,
        requester="test-suite",
        description="A request used to test the review fixes.",
        counterparty_wallet=wallet,
    )


def scores(trust: float) -> DecisionScores:
    return DecisionScores(
        trust=ScoreResult(value=trust, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )


class FakeLog:
    """In-memory audit log with the semantics that matter: rows are appended in
    order and the per-request view keeps the last row written."""

    def __init__(self):
        self.rows: list = []

    def append(self, entry, *_a, **_k):
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


# --------------------------------------------------------------------------
# A3 -- load_config must not die on a filesystem it cannot write to
# --------------------------------------------------------------------------


def test_a3_load_config_survives_a_read_only_filesystem(tmp_path, monkeypatch):
    """A3: `load_config` wrote the default file with no error handling.

    `save_config` is a convenience -- it exists so a human has a file to
    hand-edit. On a read-only filesystem (a container, Lambda, a read-only
    volume) the write fails and the caller gets an exception instead of the
    defaults. `load_config` is called from `apply_policy`, from the engine and
    from the API, so this took down EVERY request rather than just skipping a
    convenience.

    The failure is forced where a read-only mount produces it -- creating the
    directory and opening the file for writing -- rather than by remounting
    anything, so the test is the same on every platform.
    """
    path = tmp_path / "config" / "rules.yaml"

    real_mkdir = Path.mkdir

    def read_only_mkdir(self, *args, **kwargs):
        if self == path.parent:
            raise PermissionError(13, "Permission denied (read-only filesystem)", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", read_only_mkdir)
    config = load_config(path)

    assert not path.exists(), "a read-only filesystem must be left alone"
    assert config.category_limits["operational_expense"].max_usdc == 500.0
    assert config.require_known_payee is True, "the in-memory defaults must be the safe ones"


def test_a3_an_unreadable_config_file_still_raises(tmp_path):
    """The other half of A3: tolerating a write failure must not swallow a
    genuinely broken config. A file that exists and cannot be parsed is a real
    problem and has to fail loudly, or a typo in the rules would silently
    become the shipped defaults."""
    from pydantic import ValidationError

    path = tmp_path / "rules.yaml"
    path.write_text("category_limits: not_a_dict\nthresholds: {}\ncriteria: {}\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(path)


# --------------------------------------------------------------------------
# A4 -- only an escalated request can be overridden by a human
# --------------------------------------------------------------------------


def test_a4_a_human_cannot_override_a_request_the_policy_already_approved(fake_log, mock_config):
    """A4: `apply_human_override` checked only `human_review is not None`.

    It never checked that the request had been escalated. Called on a request
    the policy already approved, it re-approved it and paid the FULL requested
    amount -- measured at 50,000 USDC against a 1,000 USDC category ceiling.
    The escalation route checks the decision before calling in, so the gap is
    only reachable through another caller, which is exactly why the guard
    belongs on the function that releases the money.
    """
    from src.pipeline import AlreadyReviewedError

    approved = run_pipeline(
        make_request(),
        engine=MagicMock(score=MagicMock(return_value=scores(0.95))),
        arc_client=MagicMock(),
        verbose=False,
    )
    assert approved.decision == "approve", "the fixture must start from a policy approval"
    # An approved payment writes two rows: the pending one, then its outcome.
    rows_after_approval = len(fake_log.rows)

    arc_client = MagicMock()
    with pytest.raises(AlreadyReviewedError):
        apply_human_override(approved, "approve", "joao", arc_client=arc_client)

    arc_client.send_payment.assert_not_called()
    assert len(fake_log.rows) == rows_after_approval, "a refused override must not append anything"


def test_a4_an_escalated_request_can_still_be_overridden(fake_log, mock_config):
    """The guard must not break the path it protects: a request the policy
    escalated is exactly what a human is there to override."""
    escalated = run_pipeline(
        make_request(),
        engine=MagicMock(score=MagicMock(return_value=scores(0.2))),
        arc_client=MagicMock(),
        verbose=False,
    )
    assert escalated.decision == "escalate"

    entry = apply_human_override(escalated, "approve", "ana", arc_client=MagicMock())

    assert entry.human_review == "approved"
    assert entry.payment_amount_usdc == 100.0


# --------------------------------------------------------------------------
# A5 -- the input contract must refuse an infinite amount
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_a5_the_schema_refuses_a_non_finite_amount(bad):
    """A5: `PaymentRequest.amount_usdc` was `Field(gt=0)` with no
    `allow_inf_nan=False`, so `inf` survived validation on the manager route
    and the CLI while the requester route rejected it (that one has the flag on
    its own model). `inf` reaches the policy comparison and the escrow
    conversion before anything refuses it. The contract should be the same for
    every caller.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PaymentRequest(
            request_id="a5",
            category=RequestCategory.OTHER,
            amount_usdc=bad,
            requester="test-suite",
            description="non-finite amount",
        )


def test_a5_a_plain_amount_is_still_accepted():
    assert make_request(amount=250.5).amount_usdc == 250.5


# --------------------------------------------------------------------------
# A9 -- a timestamp collision must not pick the wrong row
# --------------------------------------------------------------------------


def test_a9_a_tied_timestamp_resolves_to_the_row_written_last():
    """A9: `_latest_for` used `max()` on the timestamp alone.

    An approved payment writes two rows -- the pending row carrying the
    in-flight marker, then the outcome -- and `_settle_recorded` writes them
    back to back. When both land on the same timestamp (the clock is coarser
    than the writes), `max()` returned whichever came first in the list, so it
    resolved to the pending row. `_in_flight` then read the request as having
    money in flight: a legitimately paid request was refused with
    `PaymentInFlightError` and had to be reconciled by hand.

    The newest row is the one written last, and among equal timestamps the only
    available evidence of "last" is the order of the list the caller passes in
    (the file is read in write order). `_latest_for` must therefore break the
    tie by position and keep the LAST matching row, not the first one it meets.
    """
    from src.audit.logger import build_entry
    from src.pipeline import _in_flight, _latest_for
    from src.policy.rules import Decision, PolicyDecision

    request = make_request()
    policy_decision = PolicyDecision(Decision.APPROVE, None, "High trust score.")
    timestamp = "2026-10-13T10:00:00+00:00"

    pending = build_entry(
        request=request,
        scores=scores(0.95),
        policy_decision=policy_decision,
        payment_executed=False,
        payment_status="not_applicable",
        payment_attempt="transfer 100.0 USDC to the wallet",
        payment_amount_usdc=100.0,
        engine_name="review",
    ).model_copy(update={"timestamp": timestamp})
    outcome = pending.model_copy(
        update={"payment_executed": True, "payment_status": "executed", "payment_attempt": None}
    )

    # Write order: the pending row, then its outcome, both stamped the same.
    entries = [pending, outcome]

    chosen = _latest_for(entries, request.request_id)
    assert chosen.payment_status == "executed", (
        "the outcome row was written last and must win the tie"
    )
    assert _in_flight(chosen) is False
