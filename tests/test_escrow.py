"""Tests for the on-chain escrow client.

Nothing here touches the network: Circle's API and the chain reader are
replaced by fakes that record every call, so the tests can assert exactly
which transactions would have been sent, and in what order.
"""

from unittest.mock import MagicMock

import pytest

from src.payments.escrow import (
    AGENTIC_COMMERCE_ADDRESS,
    ChainReader,
    EscrowBudgetMismatchError,
    EscrowClient,
    EscrowError,
    EscrowJob,
    text_to_bytes32,
    usdc_to_base_units,
)

TREASURY = "0x" + "a" * 40
PROVIDER = "0x" + "b" * 40
USDC = "0x3600000000000000000000000000000000000000"


class FakeTransactionsApi:
    """Records contract calls; each one settles with the given final state."""

    def __init__(self, final_state: str = "COMPLETE"):
        self.final_state = final_state
        self.sent: list[dict] = []

    def create_developer_transaction_contract_execution(self, request):
        self.sent.append(request.to_dict())
        response = MagicMock()
        response.data.to_dict.return_value = {"id": f"tx-{len(self.sent)}"}
        return response

    def get_transaction(self, id):  # noqa: A002 -- mirrors the SDK signature
        response = MagicMock()
        response.data.to_dict.return_value = {
            "transaction": {"state": self.final_state, "txHash": f"0xhash-{id}"}
        }
        return response

    @property
    def signatures(self) -> list[str]:
        return [call["abiFunctionSignature"] for call in self.sent]


class FakeReader:
    def __init__(self, job: EscrowJob | None = None, now: int = 1_000):
        self.job = job
        self.now = now
        self.next_job_id = 7

    def get_job(self, job_id):
        return self.job

    def job_id_from_tx(self, tx_hash):
        return self.next_job_id

    def latest_timestamp(self):
        return self.now


def make_job(**overrides) -> EscrowJob:
    base = dict(
        job_id=7,
        client=TREASURY,
        provider=PROVIDER,
        evaluator=TREASURY,
        description="milestone 1",
        budget=100_000_000,  # 100 USDC
        expired_at=9_999,
        status="Open",
        hook="0x" + "0" * 40,
    )
    return EscrowJob(**{**base, **overrides})


def make_client(job=None, final_state="COMPLETE", now=1_000):
    api = FakeTransactionsApi(final_state)
    reader = FakeReader(job, now)
    client = EscrowClient(
        transactions_api=api,
        reader=reader,
        treasury_wallet_address=TREASURY,
        poll_interval=0,
        poll_timeout=3,
    )
    return client, api, reader


# --------------------------------------------------------------------------
# Units and hashing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usdc", "base"),
    [(100.0, 100_000_000), (0.000001, 1), (12.345678, 12_345_678), (0.1, 100_000)],
)
def test_usdc_to_base_units(usdc, base):
    assert usdc_to_base_units(usdc) == base


@pytest.mark.parametrize("bad", [0, -5, 1.0000001])
def test_usdc_to_base_units_rejects_zero_negative_and_excess_precision(bad):
    with pytest.raises(ValueError):
        usdc_to_base_units(bad)


def test_text_to_bytes32_is_deterministic_and_well_formed():
    first = text_to_bytes32("deliverable")
    assert first == text_to_bytes32("deliverable")
    assert first != text_to_bytes32("other")
    assert first.startswith("0x") and len(first) == 66


# --------------------------------------------------------------------------
# create_job
# --------------------------------------------------------------------------


def test_create_job_sends_the_right_call_and_returns_the_job_id():
    client, api, _ = make_client(now=1_000)

    job_id, tx = client.create_job(PROVIDER, "milestone 1", expires_in_seconds=500)

    assert job_id == 7 and tx["tx_hash"] == "0xhash-tx-1"
    call = api.sent[0]
    assert call["walletAddress"] == TREASURY
    assert call["contractAddress"] == AGENTIC_COMMERCE_ADDRESS
    assert call["abiFunctionSignature"] == "createJob(address,address,uint256,string,address)"
    provider, evaluator, expired_at, description, hook = call["abiParameters"]
    assert (provider, evaluator) == (PROVIDER, TREASURY)  # we evaluate by default
    assert expired_at == "1500"  # chain time + expiry
    assert description == "milestone 1"
    assert hook == "0x" + "0" * 40


def test_create_job_refuses_a_self_job():
    client, api, _ = make_client()
    with pytest.raises(EscrowError, match="cannot be the treasury"):
        client.create_job(TREASURY.upper().replace("0X", "0x"), "x")
    assert api.sent == []


# --------------------------------------------------------------------------
# fund: the safety-critical path
# --------------------------------------------------------------------------


def test_fund_approves_exactly_the_approved_amount_then_funds_in_order():
    client, api, _ = make_client(job=make_job(budget=100_000_000))

    result = client.fund(7, approved_amount_usdc=100.0)

    assert api.signatures == ["approve(address,uint256)", "fund(uint256,bytes)"]
    approve, fund = api.sent
    assert approve["contractAddress"] == USDC
    assert approve["abiParameters"] == [AGENTIC_COMMERCE_ADDRESS, "100000000"]
    assert fund["contractAddress"] == AGENTIC_COMMERCE_ADDRESS
    assert fund["abiParameters"] == ["7", "0x"]
    assert result["approve_tx_hash"] == "0xhash-tx-1"
    assert result["tx_hash"] == "0xhash-tx-2"


def test_fund_refuses_an_inflated_budget_before_sending_anything():
    """The provider sets the budget, so it is untrusted: a contractor
    asking for more than policy approved must not move a single token."""
    client, api, _ = make_client(job=make_job(budget=500_000_000))  # asks for 500

    with pytest.raises(EscrowBudgetMismatchError, match="refusing to fund"):
        client.fund(7, approved_amount_usdc=100.0)

    assert api.sent == []  # not even the approve


def test_fund_refuses_a_job_that_is_not_open():
    client, api, _ = make_client(job=make_job(status="Funded"))
    with pytest.raises(EscrowError, match="is Funded, expected Open"):
        client.fund(7, 100.0)
    assert api.sent == []


def test_fund_refuses_a_job_owned_by_someone_else():
    client, api, _ = make_client(job=make_job(client="0x" + "c" * 40))
    with pytest.raises(EscrowError, match="not our treasury"):
        client.fund(7, 100.0)
    assert api.sent == []


# --------------------------------------------------------------------------
# complete / reject / claim_refund
# --------------------------------------------------------------------------


def test_complete_releases_a_submitted_job():
    client, api, _ = make_client(job=make_job(status="Submitted"))
    client.complete(7, "milestone verified")
    call = api.sent[0]
    assert call["abiFunctionSignature"] == "complete(uint256,bytes32,bytes)"
    assert call["abiParameters"] == ["7", text_to_bytes32("milestone verified"), "0x"]


@pytest.mark.parametrize("status", ["Open", "Funded", "Completed", "Rejected", "Expired"])
def test_complete_refuses_anything_not_yet_submitted(status):
    client, api, _ = make_client(job=make_job(status=status))
    with pytest.raises(EscrowError, match="expected Submitted"):
        client.complete(7, "x")
    assert api.sent == []


def test_reject_refunds_a_funded_job_and_refuses_a_finished_one():
    client, api, reader = make_client(job=make_job(status="Funded"))
    client.reject(7, "work not delivered")
    assert api.signatures == ["reject(uint256,bytes32,bytes)"]

    reader.job = make_job(status="Completed")
    with pytest.raises(EscrowError):
        client.reject(7, "too late")
    assert len(api.sent) == 1


def test_claim_refund_only_after_expiry():
    client, api, reader = make_client(job=make_job(status="Funded", expired_at=2_000), now=1_500)
    with pytest.raises(EscrowError, match="not expired yet"):
        client.claim_refund(7)
    assert api.sent == []

    reader.now = 2_000
    client.claim_refund(7)
    assert api.sent[0]["abiFunctionSignature"] == "claimRefund(uint256)"
    assert api.sent[0]["abiParameters"] == ["7"]


# --------------------------------------------------------------------------
# provider-side helpers (demo only: we control the provider wallet)
# --------------------------------------------------------------------------


def test_set_budget_and_submit_are_signed_by_the_provider_wallet():
    client, api, reader = make_client(job=make_job(status="Open"))
    client.set_budget(7, 100.0, provider_wallet=PROVIDER)
    reader.job = make_job(status="Funded")
    client.submit(7, "final report", provider_wallet=PROVIDER)

    budget, submit = api.sent
    assert budget["walletAddress"] == PROVIDER
    assert budget["abiParameters"] == ["7", "100000000", "0x"]
    assert submit["walletAddress"] == PROVIDER
    assert submit["abiParameters"] == ["7", text_to_bytes32("final report"), "0x"]


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["FAILED", "DENIED", "CANCELLED"])
def test_a_transaction_that_does_not_complete_raises_with_its_state(state):
    client, _, _ = make_client(job=make_job(status="Submitted"), final_state=state)
    with pytest.raises(EscrowError, match=f"complete job: transaction tx-1 ended as {state}"):
        client.complete(7, "x")


def test_a_transaction_stuck_in_flight_times_out():
    client, _, _ = make_client(job=make_job(status="Submitted"), final_state="PENDING")
    with pytest.raises(EscrowError, match="TIMEOUT|PENDING"):
        client.complete(7, "x")


def test_circle_api_error_is_wrapped():
    from circle.web3 import developer_controlled_wallets as dcw

    client, api, _ = make_client(job=make_job(status="Submitted"))
    api.create_developer_transaction_contract_execution = MagicMock(
        side_effect=dcw.ApiException(status=400, reason="bad request")
    )
    with pytest.raises(EscrowError, match="Failed to complete job"):
        client.complete(7, "x")


# --------------------------------------------------------------------------
# ChainReader (contract injected: no network)
# --------------------------------------------------------------------------


def test_chain_reader_decodes_a_job_tuple_and_status_name():
    contract = MagicMock()
    contract.functions.getJob.return_value.call.return_value = (
        7, TREASURY, PROVIDER, TREASURY, "milestone 1", 100_000_000, 9_999, 2, "0x" + "0" * 40,
    )
    job = ChainReader(contract=contract).get_job(7)

    assert job.status == "Submitted"
    assert job.budget_usdc == 100.0
    assert (job.client, job.provider) == (TREASURY, PROVIDER)


def test_chain_reader_rejects_an_unknown_status_code():
    contract = MagicMock()
    contract.functions.getJob.return_value.call.return_value = (
        7, TREASURY, PROVIDER, TREASURY, "x", 1, 1, 99, "0x" + "0" * 40,
    )
    with pytest.raises(EscrowError, match="unknown status code 99"):
        ChainReader(contract=contract).get_job(7)


def test_chain_reader_fails_clearly_when_the_receipt_has_no_job_created_event():
    contract = MagicMock()
    contract.events.JobCreated.return_value.process_receipt.return_value = []
    reader = ChainReader(contract=contract)
    reader._web3 = MagicMock()
    with pytest.raises(EscrowError, match="No JobCreated event"):
        reader.job_id_from_tx("0xabc")


# --------------------------------------------------------------------------
# Errors identify the transaction that failed (for the audit log)
# --------------------------------------------------------------------------


def test_a_transaction_that_fails_on_chain_carries_its_ids():
    client, _, _ = make_client(job=make_job(status="Submitted"), final_state="FAILED")
    with pytest.raises(EscrowError) as raised:
        client.complete(7, "x")
    assert raised.value.transaction_id == "tx-1"
    assert raised.value.tx_hash == "0xhash-tx-1"


def test_a_refusal_before_sending_carries_no_transaction():
    client, _, _ = make_client(job=make_job(status="Funded"))
    with pytest.raises(EscrowError) as raised:
        client.complete(7, "x")  # not submitted yet
    assert raised.value.transaction_id is None and raised.value.tx_hash is None
