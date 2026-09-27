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
    amount_usdc: float = Field(..., gt=0, description="Requested amount, in USDC.")
    requester: str = Field(..., description="Who or what is asking for this payment.")
    description: str = Field(..., description="Free-text context used by the decision engine.")
    due_date: datetime | None = Field(
        default=None, description="Deadline for the payment, if any."
    )
    counterparty_wallet: str | None = Field(
        default=None, description="Destination wallet address, if already known."
    )

    model_config = ConfigDict(use_enum_values=True)
