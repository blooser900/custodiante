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


def build_entry(
    request: PaymentRequest,
    scores: DecisionScores,
    policy_decision: PolicyDecision,
    payment_executed: bool,
) -> AuditEntry:
    """Assemble a single audit entry from one full decision cycle."""
    return AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=request,
        scores=scores,
        decision=policy_decision.decision.value,
        reduced_amount=policy_decision.reduced_amount,
        reason=policy_decision.reason,
        payment_executed=payment_executed,
    )
