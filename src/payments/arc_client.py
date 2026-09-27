"""Executes approved payments on Arc, using the Circle stack.

Kept intentionally thin: this module's only job is to move USDC once a
decision has already been made upstream. It must never make an approval
decision itself.
"""

import os


class ArcPaymentClient:
    """Thin wrapper around the Circle Agent Stack / Arc CLI."""

    def __init__(self) -> None:
        self.wallet_id = os.environ.get("ARC_WALLET_ID", "")
        self.rpc_url = os.environ.get("ARC_RPC_URL", "")
        self.api_key = os.environ.get("CIRCLE_API_KEY", "")

    def send_payment(self, destination_wallet: str, amount_usdc: float, memo: str) -> dict:
        """Send USDC to a destination wallet on Arc.

        NOTE: skeleton only. Wire this up to the Circle Agent Stack SDK
        (Wallets + Paymaster) or the Arc CLI once credentials are available.
        Testnet USDC is acceptable for the hackathon submission.
        """
        raise NotImplementedError("Wire this up to the Circle Agent Stack / Arc CLI.")

    def get_balance(self) -> float:
        """Return the current USDC balance of the configured wallet."""
        raise NotImplementedError("Wire this up to the Circle Agent Stack / Arc CLI.")
