"""What a requester is allowed to learn about their own request.

The audit entry holds everything the system knows: scores, confidences, the
policy's reason (which can quote exact limits), the engine used, escrow and
failure internals. Anything shown to a requester is something they can use
to probe for a way past the controls, so this module works by ALLOW-LIST:
`PublicStatus` has a handful of fields, and `to_public_status` fills each one
explicitly. A field added to AuditEntry later is invisible to requesters
until someone deliberately exposes it here, which is the safe default.

Even a coarse status is an oracle (a requester can retry with different
wording and watch the outcome). We cannot remove that, only avoid widening
it; the real defense is that the policy limits, human review and escrow
bound what a fooled model can cost.
"""

import secrets
from typing import Literal

from pydantic import BaseModel

from src.audit.logger import AuditEntry

PublicState = Literal["in_review", "approved", "paid", "not_sent", "declined"]

# Only ids that start with this are served by the requester endpoints, so
# they cannot be used to probe ids created through other channels (CLI, demo).
REQUESTER_ID_PREFIX = "req-"


def new_request_id() -> str:
    """An unguessable id: possessing it is what lets you read the status."""
    return REQUESTER_ID_PREFIX + secrets.token_urlsafe(12)


class PublicStatus(BaseModel):
    request_id: str
    status: PublicState
    category: str
    amount_requested: float
    # Set once the request is approved or paid (and on `not_sent`, which was
    # authorized but never sent); None while in review or after a refusal, so a
    # refusal never reveals what would have been acceptable.
    amount_approved: float | None
    updated_at: str
    # Only once paid, and only the payment itself: it is public on-chain anyway.
    tx_hash: str | None


def public_state(entry: AuditEntry) -> PublicState:
    if entry.payment_executed:
        return "paid"
    if entry.escrow_status == "rejected":
        return "declined"
    if entry.decision == "escalate" and entry.human_review != "approved":
        # Escalated and still waiting for a human, or refused by one. (A human
        # approval is recorded as decision="approve"; the != check above only
        # keeps this correct if that representation ever changes.)
        return "declined" if entry.human_review == "denied" else "in_review"
    # Approved, but provably not sent: a skipped payment (no destination wallet,
    # no Circle credentials) or one that definitely failed. These used to read as
    # "approved", which left a requester watching "payment processing" forever
    # with no way to learn it would never resolve. Only the fact that nothing was
    # sent is exposed; the reason stays the manager's.
    if entry.payment_status in ("skipped", "failed"):
        return "not_sent"
    # Approved by the policy or by a human, and either held in escrow or still in
    # flight (`unconfirmed`, where the money may actually have moved -- so never
    # "not_sent"). Its internals are the manager's.
    return "approved"


def to_public_status(entry: AuditEntry) -> PublicStatus:
    state = public_state(entry)
    approved = state in ("approved", "paid", "not_sent")
    return PublicStatus(
        request_id=entry.request.request_id,
        status=state,
        category=str(entry.request.category),
        amount_requested=entry.request.amount_usdc,
        amount_approved=(entry.reduced_amount or entry.request.amount_usdc) if approved else None,
        updated_at=entry.timestamp,
        tx_hash=entry.tx_hash if state == "paid" else None,
    )
