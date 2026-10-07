"""Append-only decision log.

Every decision the system makes is written here: what was seen, what the
engine scored, what rule applied, and what happened as a result. This is
the audit trail a reviewer should be able to replay without needing to
reconstruct anything from bank statements.
"""

from datetime import datetime, timezone

from pydantic import BaseModel

from src.decision_engine.engine import DecisionScores
from src.policy.rules import PolicyDecision
from src.requests.schema import PaymentRequest


class AuditEntry(BaseModel):
    timestamp: str
    request: PaymentRequest
    scores: DecisionScores
    decision: str
    reduced_amount: float | None
    reason: str
    payment_executed: bool
    # Populated only when payment_executed is True. Without these, the audit
    # trail cannot actually be verified against the chain or against Circle's
    # own console -- which defeats the point of having an audit trail at all.
    transaction_id: str | None = None
    tx_hash: str | None = None
    # Set only on an entry created by a human override of an escalated
    # request (see src/pipeline.py:apply_human_override). None means this
    # entry reflects the automatic pipeline only.
    human_review: str | None = None  # "approved" | "denied" | None
    reviewer: str | None = None
    # Which decision engine produced `scores` (e.g. "kev", "jev", "mock").
    # None on entries written before this field existed.
    engine_name: str | None = None
    # What happened to the on-chain payment, in a form a reviewer can read
    # without digging through stdout:
    #   "executed"       -- transfer completed
    #   "failed"         -- a transfer was attempted and errored (see payment_detail)
    #   "skipped"        -- approved, but not attempted (see payment_detail)
    #   "not_applicable" -- no payment was due (escalated or denied)
    # None on entries written before this field existed; fall back to
    # `payment_executed` when reading those.
    payment_status: str | None = None
    payment_detail: str | None = None
    # Milestone escrow (see src/payments/escrow.py). `escrow_job_id` is the
    # ERC-8183 job holding (or about to hold) the funds; `escrow_status`
    # tracks our side of its lifecycle: open -> funded -> submitted ->
    # released | rejected. Both None for requests that were paid directly.
    escrow_job_id: int | None = None
    escrow_status: str | None = None
    # Hash chain (set by src/audit/store.py at write time, never by callers):
    # `entry_hash` covers this entry's content plus `prev_hash`, and
    # `prev_hash` is the previous entry's `entry_hash`. Editing or removing
    # any past line breaks every hash after it. None on legacy entries.
    prev_hash: str | None = None
    entry_hash: str | None = None


def build_entry(
    request: PaymentRequest,
    scores: DecisionScores,
    policy_decision: PolicyDecision,
    payment_executed: bool,
    payment_result: dict | None = None,
    payment_status: str | None = None,
    payment_detail: str | None = None,
    engine_name: str | None = None,
    escrow_job_id: int | None = None,
    escrow_status: str | None = None,
) -> AuditEntry:
    """Assemble a single audit entry from one full decision cycle.

    `payment_result` is whatever ArcPaymentClient.send_payment returned
    (transaction_id, tx_hash, etc.), if a payment was attempted and
    succeeded. Pass None if no payment was executed.
    """
    return AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=request,
        scores=scores,
        decision=policy_decision.decision.value,
        reduced_amount=policy_decision.reduced_amount,
        reason=policy_decision.reason,
        payment_executed=payment_executed,
        transaction_id=(payment_result or {}).get("transaction_id"),
        tx_hash=(payment_result or {}).get("tx_hash"),
        engine_name=engine_name,
        payment_status=payment_status,
        payment_detail=payment_detail,
        escrow_job_id=escrow_job_id,
        escrow_status=escrow_status,
    )
