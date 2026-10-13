"""Deterministic policy rules.

This is the "contract" mentioned in the spec: the decision engine's scores
are checked against thresholds here. The engine cannot override or bypass
this layer -- it only supplies numbers. Threshold values come from
config/rules.yaml (see src/config.py), so a business can tune its risk
appetite without touching code; only the shape of the logic lives here.
"""

import logging
from enum import Enum

from src.config import (
    FRAUD_CHOICE_OPTION,
    FRAUD_QUESTION_ID,
    TRANSACTION_NATURE_QUESTION_ID,
    load_config,
)
from src.decision_engine.engine import DecisionScores
from src.money import quantize_usdc, to_decimal
from src.payees import PayeeStatus
from src.policy.limits import get_limit_for_category, reduced_payment_allowed
from src.requests.schema import PaymentRequest

logger = logging.getLogger(__name__)


class Decision(str, Enum):
    APPROVE = "approve"
    APPROVE_REDUCED = "approve_reduced"
    ESCALATE = "escalate"


class PolicyDecision:
    """Result of applying policy rules to a request's scores."""

    def __init__(self, decision: Decision, reduced_amount: float | None, reason: str) -> None:
        self.decision = decision
        self.reduced_amount = reduced_amount
        self.reason = reason


def _fraud_probability(scores: DecisionScores) -> float | None:
    """The engine's probability of fraud, or None when it did not answer.

    `noul` answers are a probability of "yes", so no confidence is involved.
    A missing or mistyped answer returns None and the veto simply does not
    apply: this must never be the reason a payment is blocked, only a reason
    to stop approving one automatically.
    """
    answer = scores.extra.get(FRAUD_QUESTION_ID)
    if answer is None:
        return None
    value = getattr(answer, "noul", None)
    if not isinstance(value, (int, float)):
        logger.warning(
            "The %s answer is a %s, not a noul; the fraud veto cannot read it.",
            FRAUD_QUESTION_ID,
            getattr(answer, "type", type(answer).__name__),
        )
        return None
    return float(value)


def _answered_fraud_outright(scores: DecisionScores) -> bool:
    """True when the engine named fraud in the transaction-nature question.

    The second, independent path to a veto: `natureza_transacao` offers
    `risco_fraude` as an explicit choice, so an engine that classifies a request
    as fraud there must stop the automatic approval even if the separate
    probability question came back low. Reading only the probability left a
    signal that existed and was never consulted.
    """
    answer = scores.extra.get(TRANSACTION_NATURE_QUESTION_ID)
    if answer is None:
        return False
    chosen = getattr(answer, "choice", None)
    if chosen is None:
        logger.warning(
            "The %s answer is a %s, not a choice; the fraud veto cannot read it.",
            TRANSACTION_NATURE_QUESTION_ID,
            getattr(answer, "type", type(answer).__name__),
        )
        return False
    return chosen == FRAUD_CHOICE_OPTION


def _payee_check(request: PaymentRequest, registry, enforce: bool = True) -> PolicyDecision | None:
    """Escalate when the payee is unknown, or is paid at an unregistered wallet.

    This is the check that catches a redirected invoice, and it is deliberately
    not a model judgement: it compares the wallet in the request against the
    wallets the payee is known to use. Measured on real engine answers, a
    redirected contractor payment scored trust 0.806 and P(fraud) 0.362 -- above
    the auto-approve bar and nowhere near the veto -- so neither the score nor
    the probability would have stopped it.

    Both outcomes escalate rather than deny, so a first-time payee or a vendor
    that really did change banks costs a human click, not a blocked payment.

    An EMPTY registry is not a reason to skip the check: it means every payee is
    unknown, so each one's first payment is confirmed by a person (and then
    remembered, because the log teaches the registry). The only ways the check
    does not run are explicit: the caller supplied no registry at all (unit
    tests of the scoring rules; `run_pipeline` always supplies one), or a
    manager turned `require_known_payee` off in the config.
    """
    if registry is None or not enforce:
        return None
    # The payee is the vendor when the request names one, otherwise the
    # requester: whichever entity the money is going to and being invoiced by.
    payee = (request.vendor or request.requester or "").strip()
    assessment = registry.assess(payee, request.counterparty_wallet)
    if assessment.status is PayeeStatus.MISMATCH:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=(
                f"Destination wallet is not registered for the payee "
                f"({assessment.detail}). A human confirms before money moves."
            ),
        )
    if assessment.status is PayeeStatus.UNKNOWN:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=(
                f"Payee could not be checked against the registry "
                f"({assessment.detail})."
            ),
        )
    return None


def apply_policy(
    request: PaymentRequest, scores: DecisionScores, payee_registry=None
) -> PolicyDecision:
    """Combine the three scores with configured thresholds to reach a decision.

    This function is the single place where "how cautious is the system"
    is defined. Keep it readable -- a reviewer should be able to follow the
    logic without needing the rest of the codebase. The *numbers* are
    tunable (config/rules.yaml); the *shape* of the logic is not.

    `payee_registry` is optional so every existing caller keeps working; when
    given, it adds the one check the model cannot make (see `_payee_check`).
    """
    config = load_config()
    thresholds = config.thresholds
    category_limit = get_limit_for_category(request.category)

    # A fraud signal vetoes automatic approval outright: no trust score, no
    # urgency and no amount makes it right for the machine to pay on its own.
    # It cannot override a human, so this escalates rather than blocks, and a
    # person can still approve it deliberately from the escalation queue.
    #
    # Two independent paths, and either one is enough:
    #   - the `fraud_indicators` probability at or above `fraud_veto_at`;
    #   - `natureza_transacao` answering `risco_fraude`, the engine saying so
    #     outright rather than in a probability.
    fraud = _fraud_probability(scores)
    if fraud is not None and fraud >= thresholds.fraud_veto_at:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=(
                f"Fraud indicators vetoed automatic approval "
                f"(P(fraud)={fraud:.2f} >= {thresholds.fraud_veto_at})."
            ),
        )
    if _answered_fraud_outright(scores):
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=(
                f"Fraud indicators vetoed automatic approval "
                f"(the engine classified the transaction as {FRAUD_CHOICE_OPTION!r})."
            ),
        )

    # Low trust is a hard stop, regardless of urgency or importance.
    if scores.trust.value < thresholds.trust_min_to_consider:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason="Trust score below minimum threshold.",
        )

    # Request exceeds the category's hard limit -- always needs a human,
    # no matter how confident the engine is.
    if request.amount_usdc > category_limit:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            reduced_amount=None,
            reason=f"Amount exceeds hard limit for category ({category_limit} USDC).",
        )

    # Is this payee allowed to be paid at this wallet? Checked before any
    # approval path, because it is about *where the money goes*, not how
    # plausible the request reads.
    payee_decision = _payee_check(request, payee_registry, enforce=config.require_known_payee)
    if payee_decision is not None:
        return payee_decision

    # High trust: auto-approve. Deliberately WITHOUT a confidence requirement.
    #
    # The rule used to also demand `trust.confidence >= 0.7`. Calibration on 39
    # real answers showed the engine never emits a trust confidence above 0.60,
    # so that clause never fired: it blocked 6 of 12 legitimate requests while
    # stopping none of the bad ones. Confidence here measures how concentrated
    # the model's distribution was, not whether the answer is right -- and on
    # this data the bad cases had the HIGHER confidence (mean 0.42 against 0.30
    # for the good ones), because a clear fraud story is a crisp narrative.
    # The score carries the signal; confidence stays recorded and shown to
    # managers, but no longer decides.
    if scores.trust.value >= thresholds.trust_min_auto_approve:
        return PolicyDecision(
            decision=Decision.APPROVE,
            reduced_amount=None,
            reason="High trust score.",
        )

    # Medium trust, but high importance/urgency: approve at a reduced amount
    # rather than blocking or fully escalating.
    if scores.importance.value >= thresholds.importance_high or scores.urgency.value >= thresholds.urgency_high:
        # ...but only for categories where a part payment is a meaningful
        # outcome. Half a supply purchase is still supply; half an agreed
        # contract price is not half a delivery, it is paying for work that was
        # never accepted. The category decides, and the alternative is a human
        # rather than a fraction.
        if not reduced_payment_allowed(request.category):
            return PolicyDecision(
                decision=Decision.ESCALATE,
                reduced_amount=None,
                reason=(
                    f"A partial payment is not a valid outcome for category "
                    f"{request.category!r} (allow_reduced is false), so this needs "
                    "a human instead of a fraction of the amount."
                ),
            )
        # Decimal arithmetic, rounded DOWN to USDC's 6 places. In floats,
        # 333.33 * 0.35 is 116.66549999999999: an amount with more precision than
        # USDC has, which is either refused downstream or authorizes a figure that
        # no rule produced. Rounding down means we never authorize MORE than the
        # fraction of the limit; and the full request is still the ceiling.
        capped = quantize_usdc(to_decimal(category_limit) * to_decimal(thresholds.reduced_amount_fraction))
        reduced = min(to_decimal(request.amount_usdc), capped)
        if reduced <= 0:
            return PolicyDecision(
                decision=Decision.ESCALATE,
                reduced_amount=None,
                reason="The capped amount rounds to zero USDC, so this needs a human.",
            )
        reduced_amount = float(reduced)
        return PolicyDecision(
            decision=Decision.APPROVE_REDUCED,
            reduced_amount=reduced_amount,
            reason="Medium trust, but high urgency/importance justifies a capped approval.",
        )

    # Default: when in doubt, escalate.
    return PolicyDecision(
        decision=Decision.ESCALATE,
        reduced_amount=None,
        reason="No rule confidently applies; deferring to a human.",
    )
