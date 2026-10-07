"""Smoke test of the on-chain escrow against the REAL Arc testnet.

This is the one step the unit tests cannot cover: whether Circle's wallet
accepts our contract calls, whether our EOA treasury works as the client and
evaluator, and whether the deployed contract exposes reject/claimRefund the
way the ERC-8183 draft says. Run it once, read the output, and send it back.

Prerequisites (.env):
    CIRCLE_API_KEY, CIRCLE_ENTITY_SECRET, ARC_WALLET_ADDRESS   (treasury, funded)
    ESCROW_PROVIDER_WALLET_ADDRESS   a SECOND Circle wallet you control, playing
                                     the contractor. It needs a little testnet
                                     USDC too: Arc pays gas in USDC.

Usage:
    python -m scripts.escrow_smoke --scenario happy    # fund -> submit -> complete
    python -m scripts.escrow_smoke --scenario reject   # fund -> reject (refund)
    python -m scripts.escrow_smoke --scenario expire   # fund -> wait -> claimRefund

Each run moves --amount USDC (default 1) of TESTNET funds and stops at the
first failure, printing exactly where it stopped and the job id so you can
inspect it on the explorer.
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from src.payments.escrow import EscrowClient, EscrowError  # noqa: E402

EXPLORER_TX = "https://explorer.testnet.arc.io/tx/"


def step(title: str) -> None:
    print(f"\n── {title}")


def show_tx(tx: dict) -> None:
    print(f"   ok  {EXPLORER_TX}{tx['tx_hash']}")


def show_job(escrow: EscrowClient, job_id: int) -> None:
    job = escrow.get_job(job_id)
    print(f"   job {job_id}: status={job.status} budget={job.budget_usdc} USDC")


def check_environment() -> tuple[str, str]:
    """Fail fast, before any transaction, on anything that would waste a run."""
    if os.environ.get("ARC_BLOCKCHAIN", "ARC-TESTNET") != "ARC-TESTNET":
        sys.exit("Refusing to run: ARC_BLOCKCHAIN is not ARC-TESTNET. This script is testnet-only.")
    missing = [
        name
        for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET", "ARC_WALLET_ADDRESS", "ESCROW_PROVIDER_WALLET_ADDRESS")
        if not os.environ.get(name)
    ]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")
    treasury = os.environ["ARC_WALLET_ADDRESS"]
    provider = os.environ["ESCROW_PROVIDER_WALLET_ADDRESS"]
    if treasury.lower() == provider.lower():
        sys.exit("ESCROW_PROVIDER_WALLET_ADDRESS must be a different wallet from ARC_WALLET_ADDRESS.")
    return treasury, provider


def run(scenario: str, amount: float, expire_after: int) -> None:
    _, provider = check_environment()
    escrow = EscrowClient()

    step("1. createJob (treasury is client and evaluator)")
    expires = expire_after if scenario == "expire" else 3600
    job_id, tx = escrow.create_job(provider, f"smoke test: {scenario}", expires_in_seconds=expires)
    show_tx(tx)
    show_job(escrow, job_id)

    step("2. setBudget (signed by the provider wallet)")
    show_tx(escrow.set_budget(job_id, amount, provider_wallet=provider))
    show_job(escrow, job_id)

    step("3. approve + fund (USDC leaves the treasury, locked in the contract)")
    tx = escrow.fund(job_id, approved_amount_usdc=amount)
    print(f"   approve: {EXPLORER_TX}{tx['approve_tx_hash']}")
    show_tx(tx)
    show_job(escrow, job_id)

    if scenario == "happy":
        step("4. submit (provider hands in the deliverable hash)")
        show_tx(escrow.submit(job_id, "smoke test deliverable", provider_wallet=provider))
        show_job(escrow, job_id)

        step("5. complete (evaluator releases the funds to the provider)")
        show_tx(escrow.complete(job_id, "smoke test: milestone verified"))
    elif scenario == "reject":
        step("4. reject (evaluator cancels; funds should return to the treasury)")
        show_tx(escrow.reject(job_id, "smoke test: rejected"))
    else:  # expire
        step(f"4. waiting {expire_after}s for the job to expire")
        time.sleep(expire_after + 5)
        step("5. claimRefund (funds should return to the treasury)")
        show_tx(escrow.claim_refund(job_id))

    show_job(escrow, job_id)
    print(f"\nDONE: scenario '{scenario}' completed for job {job_id}.")
    print("Now compare both wallets' USDC balances with what you expect.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Escrow smoke test on Arc testnet.")
    parser.add_argument("--scenario", choices=("happy", "reject", "expire"), default="happy")
    parser.add_argument("--amount", type=float, default=1.0, help="USDC to escrow (default 1)")
    parser.add_argument("--expire-after", type=int, default=90, help="seconds, for --scenario expire")
    args = parser.parse_args()
    try:
        run(args.scenario, args.amount, args.expire_after)
    except EscrowError as e:
        print(f"\nSTOPPED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
