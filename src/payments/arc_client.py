"""Executes approved payments on Arc, using Circle's Developer-Controlled
Wallets SDK (Python package: circle-developer-controlled-wallets).

Everything here targets ARC-TESTNET first, per project decision: no mainnet
transfer path is wired up yet. Swap ARC_BLOCKCHAIN to "ARC" only after the
testnet flow has been validated end to end.

Reference: https://developers.circle.com/wallets/dev-controlled/transfer-tokens-across-wallets
"""

import os
import time

from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils

# USDC contract address on Arc Testnet (native gas token; same address used
# in Circle's own quickstart for this chain).
ARC_TESTNET_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"

ARC_BLOCKCHAIN = os.environ.get("ARC_BLOCKCHAIN", "ARC-TESTNET")
TERMINAL_STATES = {"COMPLETE", "FAILED", "CANCELLED", "DENIED"}
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 60


class ArcPaymentClient:
    """Thin wrapper around Circle's Developer-Controlled Wallets SDK."""

    def __init__(self) -> None:
        api_key = os.environ.get("CIRCLE_API_KEY", "")
        entity_secret = os.environ.get("CIRCLE_ENTITY_SECRET", "")
        self.wallet_address = os.environ.get("ARC_WALLET_ADDRESS", "")
        self.wallet_id = os.environ.get("ARC_WALLET_ID", "")

        client = utils.init_developer_controlled_wallets_client(
            api_key=api_key,
            entity_secret=entity_secret,
        )
        self.transactions_api = dcw.TransactionsApi(client)
        self.wallets_api = dcw.WalletsApi(client)

    def send_payment(self, destination_wallet: str, amount_usdc: float, memo: str) -> dict:
        """Send USDC from the configured wallet to a destination address.

        Blocks until the transaction reaches a terminal state (COMPLETE,
        FAILED, CANCELLED, DENIED) or POLL_TIMEOUT_SECONDS elapses. `memo`
        is logged locally (Circle's transfer API has no on-chain memo field
        for a plain USDC transfer) so it still ends up in our own audit log.

        Returns the final transaction dict from Circle. Raises RuntimeError
        if the transaction does not complete.
        """
        request = dcw.CreateTransferTransactionForDeveloperRequest.from_dict(
            {
                "walletAddress": self.wallet_address,
                "blockchain": ARC_BLOCKCHAIN,
                "destinationAddress": destination_wallet,
                "tokenAddress": ARC_TESTNET_USDC_ADDRESS,
                "amounts": [str(amount_usdc)],
                "feeLevel": "MEDIUM",
            }
        )

        try:
            transfer_response = self.transactions_api.create_developer_transaction_transfer(
                request
            )
        except dcw.ApiException as e:
            raise RuntimeError(f"Failed to create transfer (memo: {memo}): {e}") from e

        transfer_data = transfer_response.data.to_dict()
        transaction_id = transfer_data["id"]
        current_state = transfer_data.get("state", "")

        elapsed = 0
        while current_state not in TERMINAL_STATES and elapsed < POLL_TIMEOUT_SECONDS:
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            poll_response = self.transactions_api.get_transaction(id=transaction_id)
            transaction = poll_response.data.to_dict()["transaction"]
            current_state = transaction.get("state", "")

        if current_state != "COMPLETE":
            raise RuntimeError(
                f"Transfer {transaction_id} did not complete (memo: {memo}); "
                f"final state: {current_state or 'TIMEOUT'}"
            )

        return {
            "transaction_id": transaction_id,
            "state": current_state,
            # NOTE: aliased to "txHash" in to_dict() output, unlike "state"
            # on the same model -- confirmed by reading the generated model
            # source directly rather than assuming consistent naming.
            "tx_hash": transaction.get("txHash"),
            "destination_wallet": destination_wallet,
            "amount_usdc": amount_usdc,
            "memo": memo,
        }

    def get_balance(self) -> float:
        """Return the configured wallet's USDC balance."""
        try:
            response = self.wallets_api.list_wallet_balance(id=self.wallet_id)
        except dcw.ApiException as e:
            raise RuntimeError(f"Failed to fetch wallet balance: {e}") from e

        # NOTE: BalancesData.to_dict() uses the camelCase alias here, unlike
        # most fields in this SDK -- confirmed by reading the generated
        # model source directly (a prior version of this file incorrectly
        # read "token_balances" and silently always returned an empty list).
        balances = response.data.to_dict().get("tokenBalances", [])
        for entry in balances:
            if entry.get("token", {}).get("symbol") == "USDC":
                return float(entry["amount"])
        return 0.0
