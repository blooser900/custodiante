"""On-chain escrow for contractor-milestone payments, via the ERC-8183
"Agentic Commerce" job contract that Arc already deploys on its testnet.

Why this exists: a plain transfer (ArcPaymentClient) pays immediately. For a
milestone payment we want the funds *locked in a contract* until someone
confirms the work was delivered, then released to the contractor, or
refunded to us. We do not write or deploy any Solidity: we call the deployed
contract through Circle's developer-controlled wallets, the same SDK used for
plain transfers.

Roles in the job lifecycle:
    client     our treasury wallet: creates the job, funds escrow
    provider   the contractor: sets the budget, submits a deliverable hash
    evaluator  our treasury wallet: alone may `complete` (release to the
               provider) or `reject` (refund the client)

Lifecycle (state in brackets):
    create_job [Open] -> set_budget -> fund [Funded] -> submit [Submitted]
        -> complete [Completed]   (funds released to the provider)
        -> reject   [Rejected]    (funds refunded to the client)
        -> claim_refund [Expired] (after `expiredAt`, funds refunded)

Reference: https://docs.arc.network/arc/tutorials/create-your-first-erc-8183-job

VERIFICATION STATUS (read before relying on this):
  Verified against Arc's published quickstart: createJob, setBudget, fund,
  submit, complete, getJob, the JobCreated event, and the USDC approve step.
  NOT verified on the deployed contract: reject(uint256,bytes32,bytes) and
  claimRefund(uint256). They follow the ERC-8183 draft standard, which the
  deployed contract diverges from in at least one place (the standard's
  fund() takes an extra `expectedBudget` argument; Arc's takes only
  `optParams`). Run `python -m scripts.escrow_smoke --scenario reject` on
  testnet before depending on the refund path.
"""

import os
import time
from dataclasses import dataclass
from decimal import Decimal

from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils
from web3 import Web3

from src.payments.arc_client import (
    ARC_BLOCKCHAIN,
    ARC_TESTNET_USDC_ADDRESS,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    TERMINAL_STATES,
)

# AgenticCommerce reference implementation on Arc Testnet (from the quickstart).
AGENTIC_COMMERCE_ADDRESS = "0x0747EEf0706327138c69792bF28Cd525089e4583"
DEFAULT_RPC_URL = "https://rpc.testnet.arc.io"

# The ERC-20 interface of USDC on Arc uses 6 decimals. (The chain's *native*
# balance uses 18; mixing the two is the classic Arc bug. Everything in this
# module is the 6-decimal ERC-20 view.)
USDC_DECIMALS = 6

JOB_STATUSES = ("Open", "Funded", "Submitted", "Completed", "Rejected", "Expired")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Minimal ABI: only what we read from the chain.
_ABI = [
    {
        "type": "function",
        "name": "getJob",
        "stateMutability": "view",
        "inputs": [{"name": "jobId", "type": "uint256"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "id", "type": "uint256"},
                    {"name": "client", "type": "address"},
                    {"name": "provider", "type": "address"},
                    {"name": "evaluator", "type": "address"},
                    {"name": "description", "type": "string"},
                    {"name": "budget", "type": "uint256"},
                    {"name": "expiredAt", "type": "uint256"},
                    {"name": "status", "type": "uint8"},
                    {"name": "hook", "type": "address"},
                ],
            }
        ],
    },
    {
        "type": "event",
        "name": "JobCreated",
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "jobId", "type": "uint256"},
            {"indexed": True, "name": "client", "type": "address"},
            {"indexed": True, "name": "provider", "type": "address"},
            {"indexed": False, "name": "evaluator", "type": "address"},
            {"indexed": False, "name": "expiredAt", "type": "uint256"},
            {"indexed": False, "name": "hook", "type": "address"},
        ],
    },
]


class EscrowError(RuntimeError):
    """An escrow operation could not be completed (before or on-chain)."""


class EscrowBudgetMismatchError(EscrowError):
    """The budget on the job is not the amount our policy approved."""


def usdc_to_base_units(amount_usdc: float) -> int:
    """Convert a USDC amount to the 6-decimal integer the contract uses.

    Goes through Decimal(str(x)) so 0.1 + 0.2-style float noise cannot leak
    in, and refuses amounts with more precision than USDC has rather than
    silently rounding money.
    """
    scaled = Decimal(str(amount_usdc)) * (10**USDC_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount_usdc} USDC has more than {USDC_DECIMALS} decimal places.")
    if scaled <= 0:
        raise ValueError("Escrow amount must be positive.")
    return int(scaled)


def text_to_bytes32(text: str) -> str:
    """keccak256 of `text` as a 0x-prefixed bytes32 hex string.

    The contract stores hashes, not content: a deliverable or a reason is
    represented by the hash of its description (or of a file / IPFS CID).
    """
    return Web3.to_hex(Web3.keccak(text=text))


@dataclass(frozen=True)
class EscrowJob:
    """A job as stored in the contract."""

    job_id: int
    client: str
    provider: str
    evaluator: str
    description: str
    budget: int  # 6-decimal base units
    expired_at: int  # unix seconds
    status: str  # one of JOB_STATUSES
    hook: str

    @property
    def budget_usdc(self) -> float:
        return self.budget / 10**USDC_DECIMALS


class ChainReader:
    """Read-only access to the contract over plain JSON-RPC (no signing)."""

    def __init__(self, rpc_url: str | None = None, contract_address: str | None = None, contract=None):
        self._web3 = Web3(Web3.HTTPProvider(rpc_url or os.environ.get("ARC_RPC_URL", DEFAULT_RPC_URL)))
        # `contract` can be injected so tests never touch the network.
        self._contract = contract or self._web3.eth.contract(
            address=Web3.to_checksum_address(contract_address or AGENTIC_COMMERCE_ADDRESS),
            abi=_ABI,
        )

    def get_job(self, job_id: int) -> EscrowJob:
        raw = self._contract.functions.getJob(job_id).call()
        status_index = int(raw[7])
        if status_index >= len(JOB_STATUSES):
            raise EscrowError(f"Job {job_id} has unknown status code {status_index}.")
        return EscrowJob(
            job_id=int(raw[0]),
            client=raw[1],
            provider=raw[2],
            evaluator=raw[3],
            description=raw[4],
            budget=int(raw[5]),
            expired_at=int(raw[6]),
            status=JOB_STATUSES[status_index],
            hook=raw[8],
        )

    def job_id_from_tx(self, tx_hash: str) -> int:
        """Read the new job's id out of the createJob transaction's receipt."""
        receipt = self._web3.eth.get_transaction_receipt(tx_hash)
        events = self._contract.events.JobCreated().process_receipt(receipt)
        if not events:
            raise EscrowError(f"No JobCreated event in transaction {tx_hash}.")
        return int(events[0]["args"]["jobId"])

    def latest_timestamp(self) -> int:
        return int(self._web3.eth.get_block("latest")["timestamp"])


class EscrowClient:
    """Drives an ERC-8183 job from our treasury wallet.

    Every method that moves money or state checks the job on-chain first and
    refuses to send a transaction that is wrong or doomed to revert: a reverted
    transaction still costs gas, and a wrong one (e.g. funding a budget the
    policy never approved) can cost money.
    """

    def __init__(
        self,
        transactions_api=None,
        reader: ChainReader | None = None,
        treasury_wallet_address: str | None = None,
        contract_address: str | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        poll_timeout: float = POLL_TIMEOUT_SECONDS,
    ) -> None:
        self.treasury = treasury_wallet_address or os.environ.get("ARC_WALLET_ADDRESS", "")
        self.contract_address = contract_address or os.environ.get(
            "ESCROW_CONTRACT_ADDRESS", AGENTIC_COMMERCE_ADDRESS
        )
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        if transactions_api is None:
            client = utils.init_developer_controlled_wallets_client(
                api_key=os.environ.get("CIRCLE_API_KEY", ""),
                entity_secret=os.environ.get("CIRCLE_ENTITY_SECRET", ""),
            )
            transactions_api = dcw.TransactionsApi(client)
        self.transactions_api = transactions_api
        self.reader = reader or ChainReader(contract_address=self.contract_address)

    # ---- reads ---------------------------------------------------------

    def get_job(self, job_id: int) -> EscrowJob:
        return self.reader.get_job(job_id)

    # ---- treasury actions (we sign) -------------------------------------

    def create_job(
        self,
        provider_wallet: str,
        description: str,
        expires_in_seconds: int = 7 * 24 * 3600,
        evaluator_wallet: str | None = None,
    ) -> tuple[int, dict]:
        """Open a job. Returns (job_id, transaction info)."""
        if provider_wallet.lower() == self.treasury.lower():
            raise EscrowError("The provider cannot be the treasury wallet itself.")
        expired_at = self.reader.latest_timestamp() + expires_in_seconds
        tx = self._execute(
            self.treasury,
            self.contract_address,
            "createJob(address,address,uint256,string,address)",
            [provider_wallet, evaluator_wallet or self.treasury, str(expired_at), description, ZERO_ADDRESS],
            label="create job",
        )
        return self.reader.job_id_from_tx(tx["tx_hash"]), tx

    def fund(self, job_id: int, approved_amount_usdc: float) -> dict:
        """Lock `approved_amount_usdc` in escrow for the job.

        The key safety property: the budget is set by the *provider*, so it
        is untrusted input. We compare it to the amount our policy approved
        and refuse to fund on any mismatch, and we `approve` exactly that
        amount, never more, so the contract could not pull a larger sum even
        if asked to.
        """
        expected = usdc_to_base_units(approved_amount_usdc)
        job = self.get_job(job_id)
        self._require_status(job, ("Open",), "fund")
        self._require_client(job)
        if job.budget != expected:
            raise EscrowBudgetMismatchError(
                f"Job {job_id} budget is {job.budget_usdc} USDC but policy approved "
                f"{approved_amount_usdc} USDC; refusing to fund."
            )
        approve_tx = self._execute(
            self.treasury,
            ARC_TESTNET_USDC_ADDRESS,
            "approve(address,uint256)",
            [self.contract_address, str(expected)],
            label="approve USDC",
        )
        fund_tx = self._execute(
            self.treasury,
            self.contract_address,
            "fund(uint256,bytes)",
            [str(job_id), "0x"],
            label="fund escrow",
        )
        return {**fund_tx, "approve_tx_hash": approve_tx["tx_hash"]}

    def complete(self, job_id: int, reason: str) -> dict:
        """Release the escrowed funds to the provider (evaluator action)."""
        job = self.get_job(job_id)
        self._require_status(job, ("Submitted",), "complete")
        return self._execute(
            self.treasury,
            self.contract_address,
            "complete(uint256,bytes32,bytes)",
            [str(job_id), text_to_bytes32(reason), "0x"],
            label="complete job",
        )

    def reject(self, job_id: int, reason: str) -> dict:
        """Refund the escrow to the client. (Signature NOT yet verified on
        the deployed contract; see the module docstring.)"""
        job = self.get_job(job_id)
        self._require_status(job, ("Open", "Funded", "Submitted"), "reject")
        return self._execute(
            self.treasury,
            self.contract_address,
            "reject(uint256,bytes32,bytes)",
            [str(job_id), text_to_bytes32(reason), "0x"],
            label="reject job",
        )

    def claim_refund(self, job_id: int) -> dict:
        """Reclaim the escrow of an expired job. (Signature NOT yet verified
        on the deployed contract; see the module docstring.)"""
        job = self.get_job(job_id)
        self._require_status(job, ("Funded", "Submitted"), "claim a refund for")
        if self.reader.latest_timestamp() < job.expired_at:
            raise EscrowError(f"Job {job_id} has not expired yet; cannot claim a refund.")
        return self._execute(
            self.treasury,
            self.contract_address,
            "claimRefund(uint256)",
            [str(job_id)],
            label="claim refund",
        )

    # ---- provider actions (only when WE control the provider wallet) ----
    # In production the contractor signs these from their own wallet. These
    # exist so a demo can play both sides with a second wallet we own.

    def set_budget(self, job_id: int, amount_usdc: float, provider_wallet: str) -> dict:
        job = self.get_job(job_id)
        self._require_status(job, ("Open",), "set the budget of")
        return self._execute(
            provider_wallet,
            self.contract_address,
            "setBudget(uint256,uint256,bytes)",
            [str(job_id), str(usdc_to_base_units(amount_usdc)), "0x"],
            label="set budget",
        )

    def submit(self, job_id: int, deliverable: str, provider_wallet: str) -> dict:
        job = self.get_job(job_id)
        self._require_status(job, ("Funded",), "submit work for")
        return self._execute(
            provider_wallet,
            self.contract_address,
            "submit(uint256,bytes32,bytes)",
            [str(job_id), text_to_bytes32(deliverable), "0x"],
            label="submit deliverable",
        )

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _require_status(job: EscrowJob, allowed: tuple[str, ...], action: str) -> None:
        if job.status not in allowed:
            raise EscrowError(
                f"Cannot {action} job {job.job_id}: it is {job.status}, expected {' or '.join(allowed)}."
            )

    def _require_client(self, job: EscrowJob) -> None:
        if job.client.lower() != self.treasury.lower():
            raise EscrowError(f"Job {job.job_id} belongs to {job.client}, not our treasury wallet.")

    def _execute(
        self, wallet_address: str, contract: str, signature: str, params: list[str], label: str
    ) -> dict:
        """Send one contract call through Circle and wait for it to settle."""
        request = dcw.CreateContractExecutionTransactionForDeveloperRequest.from_dict(
            {
                "walletAddress": wallet_address,
                "blockchain": ARC_BLOCKCHAIN,
                "contractAddress": contract,
                "abiFunctionSignature": signature,
                "abiParameters": params,
                "feeLevel": "MEDIUM",
            }
        )
        try:
            response = self.transactions_api.create_developer_transaction_contract_execution(request)
        except dcw.ApiException as e:
            raise EscrowError(f"Failed to {label}: {e}") from e

        transaction_id = response.data.to_dict()["id"]
        state, transaction = "", {}
        # Bounded by attempt count, not elapsed time, so the loop terminates
        # even when poll_interval is 0 (as in tests).
        max_polls = max(1, round(self.poll_timeout / self.poll_interval)) if self.poll_interval else int(self.poll_timeout)
        for _ in range(max_polls):
            time.sleep(self.poll_interval)
            poll = self.transactions_api.get_transaction(id=transaction_id)
            transaction = poll.data.to_dict()["transaction"]
            state = transaction.get("state", "")
            if state in TERMINAL_STATES:
                break

        if state != "COMPLETE":
            raise EscrowError(
                f"Failed to {label}: transaction {transaction_id} ended as {state or 'TIMEOUT'}."
            )
        # Aliased to "txHash" in to_dict() output (see arc_client.py).
        tx_hash = transaction.get("txHash")
        if not tx_hash:
            raise EscrowError(f"Failed to {label}: transaction {transaction_id} has no tx hash.")
        return {"transaction_id": transaction_id, "tx_hash": tx_hash, "state": state, "label": label}
