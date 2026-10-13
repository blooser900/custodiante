"""The payee registry, and the rule that some categories cannot be paid in part.

Both exist because of something the audit data cannot provide on its own.

The registry: `history.py` keys the destination history by the wallet *in the
request*, so a wallet that was paid cleanly before looks established even when
it was never the wallet of the vendor being invoiced. Measured on real engine
answers, a request redirecting an established contractor's payment to another
account scored trust 0.806 (above the 0.75 auto-approve bar) and P(fraud) 0.362
(far below the 0.7 veto), so it was approved with no human. The check that
catches it is an equality, not a model judgement.

Part payments: the reduced-approval branch pays a fraction of the ceiling. That
is meaningful for supplies and meaningless for a contract milestone.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.audit.logger import AuditEntry
from src.config import (
    CategoryLimit,
    RulesConfig,
    ScoringCriteria,
    Thresholds,
    VendorRegistration,
)
from src.decision_engine.answers import NoulAnswer
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.payees import PayeeRegistry, PayeeStatus
from src.pipeline import run_pipeline
from src.policy.rules import Decision, apply_policy
from src.requests.schema import PaymentRequest

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def config(require_known_payee: bool = True, **overrides) -> RulesConfig:
    limits = {
        "contractor_milestone": CategoryLimit(max_usdc=2000.0, allow_reduced=False),
        "supply_purchase": CategoryLimit(max_usdc=1000.0, allow_reduced=True),
        "operational_expense": CategoryLimit(max_usdc=500.0, allow_reduced=True),
        "other": CategoryLimit(max_usdc=200.0, allow_reduced=True),
    }
    limits.update(overrides)
    return RulesConfig(
        category_limits=limits,
        thresholds=Thresholds(trust_min_auto_approve=0.75, trust_min_to_consider=0.4,
                              importance_high=0.7, urgency_high=0.7,
                              reduced_amount_fraction=0.5, fraud_veto_at=0.7),
        criteria=ScoringCriteria(trust=["a", "b"], urgency=["a", "b"], importance=["a", "b"]),
        require_known_payee=require_known_payee,
    )


def scores(trust=0.9, importance=0.1, urgency=0.1) -> DecisionScores:
    return DecisionScores(
        trust=ScoreResult(value=trust, confidence=0.8),
        urgency=ScoreResult(value=urgency, confidence=0.8),
        importance=ScoreResult(value=importance, confidence=0.8),
    )


def request(wallet=WALLET_A, vendor="Acme Ltda", category="operational_expense", amount=100.0):
    return PaymentRequest(
        request_id="r1", category=category, amount_usdc=amount, requester="IT Department",
        vendor=vendor, description="d", counterparty_wallet=wallet,
    )


def registry_for(vendor="acme ltda", wallets=(WALLET_A,)) -> PayeeRegistry:
    return PayeeRegistry({vendor: VendorRegistration(key=vendor, wallets=list(wallets))})


def decide(req, scores_, registry, cfg=None):
    c = cfg or config()
    with patch("src.policy.rules.load_config", return_value=c), \
         patch("src.policy.limits.load_config", return_value=c):
        return apply_policy(req, scores_, payee_registry=registry)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_an_empty_registry_escalates_every_payee_as_unknown():
    """An empty registry is NOT "nothing to contradict": it is a system that has
    never confirmed a payee, so a person confirms the first payment to each one.
    (It used to skip the check, which exempted the very first payment while the
    second payee already needed a human.)"""
    decision = decide(request(), scores(trust=0.99), PayeeRegistry())
    assert decision.decision == Decision.ESCALATE
    assert "not in the registry" in decision.reason


def test_the_check_is_on_by_default():
    from src.config import _default_config

    assert RulesConfig.model_fields["require_known_payee"].default is True
    assert _default_config().require_known_payee is True


def test_switching_the_check_off_is_explicit_and_turns_all_of_it_off():
    """The flag is for demos and for rolling the check out on a system that has
    history. Off means off: not even a wallet mismatch escalates."""
    off = config(require_known_payee=False)
    assert decide(request(), scores(), PayeeRegistry(), off).decision == Decision.APPROVE
    assert decide(request(wallet=WALLET_B), scores(), registry_for(), off).decision == Decision.APPROVE


def test_a_caller_that_supplies_no_registry_is_not_asking_for_the_check():
    """`apply_policy(request, scores)` is how the scoring rules are unit tested;
    `run_pipeline` always supplies a registry, which the tests below pin."""
    assert decide(request(), scores(), None).decision == Decision.APPROVE


def test_a_registered_wallet_is_accepted():
    result = decide(request(wallet=WALLET_A), scores(), registry_for())
    assert result.decision == Decision.APPROVE


def test_a_different_wallet_for_a_known_payee_escalates():
    """The redirected invoice -- the case no model score caught.

    Measured: trust 0.806, P(fraud) 0.362, classified as `compra_padrao`. It was
    approved automatically. The registry catches it because the wallet is not one
    the payee is registered to use.
    """
    result = decide(request(wallet=WALLET_B), scores(), registry_for())

    assert result.decision == Decision.ESCALATE
    assert "not registered for the payee" in result.reason
    assert result.reduced_amount is None


def test_the_redirect_is_caught_even_with_a_perfect_trust_score():
    """No score buys past it: this is not a threshold, it is an equality."""
    perfect = scores(trust=1.0)
    perfect.extra["fraud_indicators"] = NoulAnswer(noul=0.0)  # the engine sees no fraud
    result = decide(request(wallet=WALLET_B), perfect, registry_for())

    assert result.decision == Decision.ESCALATE


def test_an_unknown_payee_escalates_so_a_first_payment_is_confirmed():
    result = decide(request(), scores(), registry_for(vendor="someone else"))
    assert result.decision == Decision.ESCALATE
    assert "could not be checked against the registry" in result.reason


def test_a_deliberate_move_is_allowed_by_registering_both_wallets():
    """A vendor really moving banks is a manager decision, not a policy guess."""
    result = decide(request(wallet=WALLET_B), scores(), registry_for(wallets=(WALLET_A, WALLET_B)))
    assert result.decision == Decision.APPROVE


def test_the_payee_falls_back_to_the_requester_when_no_vendor_is_named():
    """The portal does not collect a vendor, so those requests must still work."""
    req = request(vendor=None)
    result = decide(req, scores(), registry_for(vendor="it department"))
    assert result.decision == Decision.APPROVE


def test_the_vendor_wins_over_the_requester_when_both_are_named():
    """The party being PAID is the one that matters for a destination check."""
    reg = registry_for(vendor="acme ltda", wallets=(WALLET_A,))
    # The requester is registered elsewhere; paying the vendor's own wallet is fine.
    reg.learn_from_log([])
    result = decide(request(wallet=WALLET_A, vendor="Acme Ltda"), scores(), reg)
    assert result.decision == Decision.APPROVE


def test_an_entry_written_before_the_vendor_field_existed_still_loads():
    """Legacy rows have no `vendor`; the registry must not choke on them."""
    legacy = AuditEntry(
        timestamp="2026-10-05T12:00:00+00:00",
        request=PaymentRequest(request_id="old", category="operational_expense",
                               amount_usdc=10.0, requester="Reception", description="d",
                               counterparty_wallet=WALLET_A),
        scores=scores(), decision="approve", reduced_amount=None, reason="r",
        payment_executed=True, payment_status="executed",
    )
    reg = PayeeRegistry().learn_from_log([legacy])
    assert reg.known_wallets("reception") == (WALLET_A,)


# --------------------------------------------------------------------------
# What the log teaches, and what it must not
# --------------------------------------------------------------------------


def entry(wallet=WALLET_A, status="executed", **overrides) -> AuditEntry:
    base = dict(
        timestamp="2026-10-05T12:00:00+00:00", request=request(wallet=wallet), scores=scores(),
        decision="approve", reduced_amount=None, reason="r", payment_executed=(status == "executed"),
        payment_status=status,
    )
    base.update(overrides)
    return AuditEntry(**base)


def test_a_successful_payment_teaches_the_registry():
    reg = PayeeRegistry().learn_from_log([entry()])
    assert reg.known_wallets("ACME LTDA") == (WALLET_A,)  # case-insensitive
    assert reg.assess("Acme Ltda", WALLET_A).status is PayeeStatus.KNOWN
    assert reg.assess("Acme Ltda", WALLET_B).status is PayeeStatus.MISMATCH


@pytest.mark.parametrize("status,kwargs", [
    ("refunded", {"escrow_status": "rejected"}),
    ("failed", {}),
    ("not_applicable", {"human_review": "denied"}),
    ("skipped", {}),
    ("unconfirmed", {"payment_attempt": "transfer 100.0 USDC"}),
])
def test_only_a_settled_success_counts_as_proof(status, kwargs):
    """A refund means the destination was rejected; a failure means it did not
    work; a denial means a human said no. None of them prove the payee uses the
    wallet, and treating them as proof would let a rejected wallet become
    trusted."""
    reg = PayeeRegistry().learn_from_log([entry(status=status, **kwargs)])
    assert reg.is_empty()
    assert reg.assess("Acme Ltda", WALLET_A).status is PayeeStatus.UNKNOWN


def test_a_registered_wallet_is_not_overwritten_by_the_log():
    """Configuration is authoritative; the log only adds."""
    reg = registry_for(wallets=(WALLET_A,))
    reg.learn_from_log([entry(wallet=WALLET_B)])
    assert set(reg.known_wallets("acme ltda")) == {WALLET_A, WALLET_B}
    assert reg.source_of("acme ltda", WALLET_A) == "registered"
    assert reg.source_of("acme ltda", WALLET_B) == "observed"


def test_an_entry_without_a_wallet_teaches_nothing():
    reg = PayeeRegistry().learn_from_log([entry(wallet=None)])
    assert reg.is_empty()


# --------------------------------------------------------------------------
# Part payments are not always an option
# --------------------------------------------------------------------------


def test_a_category_that_forbids_part_payment_escalates_instead():
    """Half a milestone is not half a delivery: it pays for unaccepted work."""
    result = decide(
        request(category="contractor_milestone", amount=1500.0),
        scores(trust=0.5, importance=0.9),  # would otherwise be approve_reduced
        registry_for(),
    )

    assert result.decision == Decision.ESCALATE
    assert result.reduced_amount is None
    assert "partial payment is not a valid outcome" in result.reason


def test_a_category_that_allows_part_payment_still_reduces():
    result = decide(
        request(category="supply_purchase", amount=300.0),
        scores(trust=0.5, importance=0.9),
        registry_for(),
    )

    assert result.decision == Decision.APPROVE_REDUCED
    assert result.reduced_amount == 300.0


def test_the_reduced_branch_is_the_only_thing_the_rule_blocks():
    """A high trust score still auto-approves a milestone in full."""
    result = decide(
        request(category="contractor_milestone", amount=1500.0),
        scores(trust=0.9),
        registry_for(),
    )
    assert result.decision == Decision.APPROVE


def test_config_defaults_forbid_part_payment_only_where_it_makes_no_sense():
    from src.config import load_config

    limits = load_config().category_limits
    assert limits["contractor_milestone"].allow_reduced is False
    assert limits["supply_purchase"].allow_reduced is True
    assert limits["operational_expense"].allow_reduced is True
    assert limits["other"].allow_reduced is True


# --------------------------------------------------------------------------
# End to end: what a fresh install does (the registry check is on by default)
# --------------------------------------------------------------------------


def run_with_log(prior_entries, cfg=None):
    """Run one request through the real pipeline and policy against a given log."""
    cfg = cfg or config()
    engine = MagicMock()
    engine.score.return_value = scores(trust=0.95)
    arc_client = MagicMock()
    arc_client.send_payment.return_value = {"transaction_id": "tx-1", "tx_hash": "0x1"}
    with (
        patch("src.pipeline.append_entry"),
        patch("src.pipeline.read_latest_per_request", return_value=prior_entries),
        patch("src.policy.rules.load_config", return_value=cfg),
        patch("src.policy.limits.load_config", return_value=cfg),
        patch("src.config.load_config", return_value=cfg),
        patch.dict(os.environ, {"CIRCLE_API_KEY": "fake-key", "ESCROW_ENABLED": "false"}),
    ):
        # A new request id: the prior entries use "r1" and a repeat would be refused.
        new_request = request().model_copy(update={"request_id": "new-1"})
        result = run_pipeline(new_request, engine=engine, arc_client=arc_client, verbose=False)
    return result, arc_client


def test_a_fresh_install_escalates_the_first_payment_to_any_payee():
    result, arc_client = run_with_log([])
    assert result.decision == "escalate"
    assert "registry" in result.reason
    arc_client.send_payment.assert_not_called()


def test_once_a_payee_has_been_paid_at_a_wallet_the_next_request_goes_through():
    result, arc_client = run_with_log([entry(wallet=WALLET_A)])
    assert result.decision == "approve"
    arc_client.send_payment.assert_called_once()


def test_an_unconfirmed_first_payment_does_not_open_the_gate():
    """The probe that motivated this: a first payment that stayed unconfirmed
    taught the registry nothing, the registry stayed empty, and the check stayed
    off for every request after it."""
    unconfirmed = entry(status="unconfirmed", payment_attempt="transfer 100.0 USDC")
    result, arc_client = run_with_log([unconfirmed])
    assert result.decision == "escalate"
    arc_client.send_payment.assert_not_called()


def test_a_manager_can_switch_the_check_off_and_the_first_payment_goes_through():
    result, arc_client = run_with_log([], cfg=config(require_known_payee=False))
    assert result.decision == "approve"
    arc_client.send_payment.assert_called_once()
