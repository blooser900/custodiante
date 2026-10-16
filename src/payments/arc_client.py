"""Executes approved payments on Arc, using Circle's Developer-Controlled
Wallets SDK (Python package: circle-developer-controlled-wallets).

Everything here targets ARC-TESTNET first, per project decision: no mainnet
transfer path is wired up yet. Swap ARC_BLOCKCHAIN to "ARC" only after the
testnet flow has been validated end to end.

Reference: https://developers.circle.com/wallets/dev-controlled/transfer-tokens-across-wallets
"""

import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timedelta

from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils

from src.money import format_usdc

# USDC contract address on Arc Testnet (native gas token; same address used
# in Circle's own quickstart for this chain).
ARC_TESTNET_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"

# Structural check only (0x + 40 hex chars). This catches typos, truncated
# copy-pastes, and addresses from the wrong chain format -- it does NOT
# verify EIP-55 checksum casing or that the address actually exists/is
# reachable on Arc.
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_evm_address(value: str) -> bool:
    """Structural check only: 0x followed by exactly 40 hex characters.

    Public because the escrow path validates the same way a plain transfer
    does, and a second copy of this rule would be a second thing to get wrong.
    """
    return bool(_EVM_ADDRESS_RE.match(value))


def _validate_destination(destination_wallet: str, source_wallet: str) -> None:
    """Reject a transfer before it reaches Circle's API if it's clearly wrong.

    Raises ValueError for a malformed address or a transfer back to the
    same wallet that is about to send it -- the latter being something
    Circle's API has been observed to accept and mark COMPLETE without the
    transfer being visible on the public explorer afterwards, which makes
    it a dangerous silent no-op for a payment-approval system to allow.
    """
    if not is_evm_address(destination_wallet):
        raise ValueError(
            f"'{destination_wallet}' is not a well-formed EVM address "
            "(expected 0x followed by 40 hex characters)."
        )
    if source_wallet and destination_wallet.lower() == source_wallet.lower():
        raise ValueError(
            "destination_wallet is the same as the source wallet. "
            "Refusing a self-transfer: it has been observed to return "
            "a COMPLETE state without a verifiable on-chain transaction."
        )


# Fixed forever: changing it would change every key and defeat the protection.
_IDEMPOTENCY_SALT = "tameion-payment-agent/idempotency/v1"


def payment_idempotency_key(request_id: str, attempt: int = 1) -> str:
    """A stable UUID for "the `attempt`-th transfer for this request".

    Circle deduplicates transfers on `idempotencyKey`, so sending the same key
    twice can never move the money twice, even when the first call's answer was
    lost to a timeout or a crash. Derived from the request id instead of random
    for exactly that reason: a retry of the same request reproduces the key.

    Circle documents the key as a UUID v4, so the 16 hash bytes are stamped with
    the v4 version and variant bits: deterministic, yet valid wherever a v4 is
    required (a v5 would be just as unique but not what the documentation asks).

    `attempt` is how a deliberate retry gets a fresh key. A retry after a
    transfer is KNOWN to have failed must use attempt + 1, because Circle may
    replay the stored (failed) result for a repeated key. A retry of a transfer
    whose fate is UNKNOWN must reuse the SAME attempt, so that Circle, not our
    guess, decides whether it already exists.
    """
    digest = hashlib.sha256(f"{_IDEMPOTENCY_SALT}:{request_id}:{attempt}".encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


class PaymentUnconfirmedError(RuntimeError):
    """A transfer may have been sent, and we do not know how it ended.

    Raised when the outcome cannot be established: the poll ran out of time
    with the transaction still in a non-terminal state, a poll call failed, or
    the create call died on something other than an API refusal (a network
    cut can lose the answer of a request Circle did receive).

    This is NOT a failure. Reporting it as one invites a retry, and a retry
    of a transfer that did complete is a double payment. The caller must keep
    the payment marked as in flight until it is reconciled against Circle,
    using `transaction_id` when there is one, or the idempotency key (safe to
    resend) when there is not.
    """

    def __init__(self, message: str, *, transaction_id: str | None = None, state: str | None = None):
        super().__init__(message)
        self.transaction_id = transaction_id
        self.state = state

_SEARCH_PAGE_SIZE = 50


def _summarize_transaction(transaction: dict) -> dict:
    """The few fields reconciliation needs, from a Transaction.to_dict()."""
    return {
        "transaction_id": transaction.get("id"),
        "state": transaction.get("state"),
        # to_dict() aliases these to camelCase, unlike "state" and "id".
        "tx_hash": transaction.get("txHash"),
        "error_reason": transaction.get("errorReason"),
    }


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

    def send_payment(
        self,
        destination_wallet: str,
        amount_usdc: float,
        memo: str,
        idempotency_key: str | None = None,
        ref_id: str | None = None,
    ) -> dict:
        """Send USDC from the configured wallet to a destination address.

        Blocks until the transaction reaches a terminal state (COMPLETE,
        FAILED, CANCELLED, DENIED) or POLL_TIMEOUT_SECONDS elapses. `memo`
        is logged locally (Circle's transfer API has no on-chain memo field
        for a plain USDC transfer) so it still ends up in our own audit log.

        `idempotency_key` (see payment_idempotency_key) makes the call safe to
        repeat: Circle will not create a second transfer for the same key.
        `ref_id` is stored on the transaction so it can be found in Circle's
        console from our request id.

        Returns the final transaction dict from Circle. Raises:
          ValueError                 the destination or the amount failed
                                     validation, before any API call is made;
          RuntimeError               the transfer definitely did NOT happen
                                     (Circle refused it, or it ended FAILED,
                                     CANCELLED or DENIED);
          PaymentUnconfirmedError    the transfer may have happened and we
                                     could not find out (a RuntimeError
                                     subclass, so it must be caught FIRST).
        """
        _validate_destination(destination_wallet, self.wallet_address)
        # Rendered from a Decimal, never str(float): 1e-06 and 0.30000000000000004
        # are what a float prints. Raises (a ValueError) before any API call if the
        # amount is not something USDC can represent.
        amount_text = format_usdc(amount_usdc)

        payload = {
            "walletAddress": self.wallet_address,
            "blockchain": ARC_BLOCKCHAIN,
            "destinationAddress": destination_wallet,
            "tokenAddress": ARC_TESTNET_USDC_ADDRESS,
            "amounts": [amount_text],
            "feeLevel": "MEDIUM",
        }
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        if ref_id:
            payload["refId"] = ref_id
        request = dcw.CreateTransferTransactionForDeveloperRequest.from_dict(payload)

        try:
            transfer_response = self.transactions_api.create_developer_transaction_transfer(
                request
            )
        except dcw.ApiException as e:
            # Circle answered and said no: nothing was created.
            raise RuntimeError(f"Failed to create transfer (memo: {memo}): {e}") from e
        except Exception as e:  # noqa: BLE001 -- see below
            # Anything else (timeout, connection reset...) can mean the request
            # reached Circle and only the answer was lost. We cannot claim the
            # transfer did not happen.
            raise PaymentUnconfirmedError(
                f"The transfer request may have reached Circle but its answer was lost "
                f"(memo: {memo}): {type(e).__name__}: {e}",
            ) from e

        transfer_data = transfer_response.data.to_dict()
        transaction_id = transfer_data["id"]
        current_state = transfer_data.get("state", "")
        transaction: dict = {}

        elapsed = 0
        try:
            while current_state not in TERMINAL_STATES and elapsed < POLL_TIMEOUT_SECONDS:
                time.sleep(POLL_INTERVAL_SECONDS)
                elapsed += POLL_INTERVAL_SECONDS
                poll_response = self.transactions_api.get_transaction(id=transaction_id)
                transaction = poll_response.data.to_dict()["transaction"]
                current_state = transaction.get("state", "")
            if current_state == "COMPLETE" and not transaction:
                # Already complete in the create response: one read to get the
                # tx hash, which the audit trail needs.
                transaction = self.transactions_api.get_transaction(id=transaction_id).data.to_dict()["transaction"]
        except Exception as e:  # noqa: BLE001
            raise PaymentUnconfirmedError(
                f"Transfer {transaction_id} was created but polling it failed "
                f"(memo: {memo}): {type(e).__name__}: {e}",
                transaction_id=transaction_id,
            ) from e

        if current_state in TERMINAL_STATES and current_state != "COMPLETE":
            # Definitively over, and it did not move money.
            raise RuntimeError(
                f"Transfer {transaction_id} did not complete (memo: {memo}); "
                f"final state: {current_state}"
            )
        if current_state != "COMPLETE":
            # Still in flight when we stopped waiting: it may yet complete.
            raise PaymentUnconfirmedError(
                f"Transfer {transaction_id} was still {current_state or 'unreported'} after "
                f"{POLL_TIMEOUT_SECONDS}s (memo: {memo}); it may still complete.",
                transaction_id=transaction_id,
                state=current_state or None,
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

    def find_transfer(
        self,
        ref_id: str,
        transaction_id: str | None = None,
        destination_wallet: str | None = None,
        since: str | None = None,
    ) -> dict | None:
        """Look up a transfer we may have sent. READ ONLY: this never creates anything.

        With a `transaction_id` it is an exact lookup. Without one (the create
        call died before we learned the id, or the process crashed first) it
        lists transactions of our wallet to `destination_wallet` created since
        `since` and matches `ref_id` (the request id we send as `refId`)
        client-side, because the API has no filter for it.

        Returns {"transaction_id", "state", "tx_hash", "error_reason"}, or None
        when nothing matches. None means "not found in this search", not "never
        sent": the caller must treat it as unknown.

        Raises RuntimeError when the answer cannot be trusted: the API failed,
        the page was full (a match may be on the next page), or several
        transactions share the ref id.
        """
        try:
            if transaction_id:
                data = self.transactions_api.get_transaction(id=transaction_id).data.to_dict()
                return _summarize_transaction(data["transaction"])

            filters: dict = {"wallet_ids": self.wallet_id, "page_size": _SEARCH_PAGE_SIZE}
            if destination_wallet:
                filters["destination_address"] = destination_wallet
            if since:
                filters["var_from"] = datetime.fromisoformat(since) - timedelta(minutes=10)
            data = self.transactions_api.list_transactions(**filters).data.to_dict()
        except dcw.ApiException as e:
            raise RuntimeError(f"Could not query Circle for request {ref_id!r}: {e}") from e

        transactions = data.get("transactions") or []
        matches = [t for t in transactions if t.get("refId") == ref_id]
        if len(matches) > 1:
            raise RuntimeError(
                f"{len(matches)} transactions share ref id {ref_id!r}; check them by hand in Circle's console."
            )
        if matches:
            return _summarize_transaction(matches[0])
        if len(transactions) >= _SEARCH_PAGE_SIZE:
            raise RuntimeError(
                f"The search for request {ref_id!r} returned a full page without a match, so a "
                "match could be on the next page. Look the transfer up by transaction id instead."
            )
        return None

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
