"""Conditional payment release, inspired by circlefin/arc-escrow.

Used mainly for contractor-milestone payments: funds are only released once
a verification step confirms the milestone was actually completed.
"""

from dataclasses import dataclass

from src.payments.arc_client import ArcPaymentClient


@dataclass
class EscrowRelease:
    request_id: str
    destination_wallet: str
    amount_usdc: float
    milestone_verified: bool


class EscrowManager:
    """Holds funds conditionally and releases them once verified."""

    def __init__(self, arc_client: ArcPaymentClient) -> None:
        self.arc_client = arc_client

    def release_if_verified(self, release: EscrowRelease) -> bool:
        """Release funds only if the milestone has been verified.

        Returns True if the payment was executed, False if it was held.
        """
        if not release.milestone_verified:
            return False

        # NOTE: skeleton -- delegate the actual transfer to ArcPaymentClient
        # once it is wired up to the Circle Agent Stack.
        self.arc_client.send_payment(
            destination_wallet=release.destination_wallet,
            amount_usdc=release.amount_usdc,
            memo=f"escrow release for request {release.request_id}",
        )
        return True
