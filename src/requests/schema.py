"""Defines the shape of an incoming payment request.

This is the single input contract for the whole system: every request,
whatever its origin (API call, CLI, webhook), must be normalized into this
shape before it reaches the decision engine.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RequestCategory(str, Enum):
    """Broad category of the payment being requested."""

    CONTRACTOR_MILESTONE = "contractor_milestone"
    SUPPLY_PURCHASE = "supply_purchase"
    OPERATIONAL_EXPENSE = "operational_expense"
    OTHER = "other"


class PaymentRequest(BaseModel):
    """A single payment request awaiting a decision."""

    request_id: str = Field(..., description="Unique identifier for this request.")
    category: RequestCategory
    # `allow_inf_nan=False` is part of the CONTRACT, not a per-route nicety:
    # `inf` satisfies `gt=0`, so without this it survives validation here and
    # only fails later -- inside a policy comparison or the escrow conversion.
    # The requester route already refused it by setting the flag on its own
    # model, which meant the manager route and the CLI accepted an amount that
    # could never be paid. A value USDC cannot represent is refused once, at the
    # edge, for every caller.
    amount_usdc: float = Field(
        ..., gt=0, allow_inf_nan=False, description="Requested amount, in USDC."
    )
    requester: str = Field(..., description="Who or what is asking for this payment.")
    # Who is being PAID, when that is a different thing from who is asking. The
    # payee registry needs it: a request that redirects a vendor's payment to a
    # different wallet can only be caught by tying the vendor to the wallets it
    # uses, and `requester` is the department asking, not the payee. Optional
    # because not every request names a vendor, and because the requester portal
    # does not collect one -- falling back to `requester` keeps those working.
    vendor: str | None = Field(
        default=None,
        max_length=200,
        description="Payee entity, if distinct from the requester.",
    )
    description: str = Field(..., description="Free-text context used by the decision engine.")
    due_date: datetime | None = Field(
        default=None, description="Deadline for the payment, if any."
    )
    counterparty_wallet: str | None = Field(
        default=None, description="Destination wallet address, if already known."
    )
    # When the system received the request. The pipeline stamps this; a caller
    # sending a request may also set it explicitly. It matters because the hour
    # of day is one of the strongest fraud signals there is -- a transfer
    # requested at 03:00 is a different animal from the same amount at 10:45 --
    # and nothing else in this model records when the request happened. Kept
    # optional so hand-written scenarios (demo seeds, calibration samples) stay
    # reproducible instead of carrying a wall-clock timestamp.
    submitted_at: datetime | None = Field(
        default=None, description="When the request was received, timezone-aware."
    )

    model_config = ConfigDict(use_enum_values=True)
