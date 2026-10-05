"""One-time setup helper for the Arc TESTNET payment flow.

What it does, in order (each step is skipped if already done):
  1. Registers a new entity secret with Circle and saves the recovery file
     to ./recovery/, then appends CIRCLE_ENTITY_SECRET to .env.
  2. Creates a wallet set and one EOA wallet on ARC-TESTNET, then appends
     ARC_WALLET_ID and ARC_WALLET_ADDRESS to .env.
  3. Opens the public Circle faucet so you can fund the wallet with testnet
     USDC (this step stays manual on purpose -- see note below).
  4. Optionally waits until the wallet shows a USDC balance (--wait).

Prerequisite: CIRCLE_API_KEY must already be in your .env file.

Usage:
    python -m scripts.setup_arc_wallet
    python -m scripts.setup_arc_wallet --wait

Note on funding: Circle's programmatic faucet endpoint (/v1/faucet/drips)
requires an account upgraded beyond testnet, so it is not reliable for a
fresh developer account. The public faucet (faucet.circle.com) only needs
a wallet address and no account, so we guide you there instead.

References:
  https://developers.circle.com/wallets/dev-controlled/register-entity-secret
  https://developers.circle.com/wallets/fund-a-testnet-wallet
"""

import argparse
import os
import re
import sys
import time
import webbrowser
from pathlib import Path

from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils
from dotenv import load_dotenv

ENV_PATH = Path(".env")
RECOVERY_DIR = Path("./recovery")
BLOCKCHAIN = "ARC-TESTNET"
FAUCET_URL = "https://faucet.circle.com/"
WALLET_SET_NAME = "tameion-payment-agent"


# --------------------------------------------------------------------------
# .env helpers
# --------------------------------------------------------------------------


def env_has_key(key: str, env_path: Path = ENV_PATH) -> bool:
    """True if `key=` already appears (with a non-empty value) in the .env file."""
    if not env_path.exists():
        return False
    pattern = re.compile(rf"^{re.escape(key)}=(.+)$", re.MULTILINE)
    return bool(pattern.search(env_path.read_text()))


def append_env(key: str, value: str, env_path: Path = ENV_PATH) -> None:
    """Set KEY=value in the .env file. Refuses to overwrite an existing value.

    If the file has an empty placeholder line (e.g. `KEY=` copied from
    .env.example), that line is filled in instead of adding a duplicate key.
    """
    if env_has_key(key, env_path):
        raise RuntimeError(f"{key} already exists in {env_path}. Refusing to overwrite it.")

    text = env_path.read_text() if env_path.exists() else ""
    placeholder = re.compile(rf"^{re.escape(key)}=[ \t]*$", re.MULTILINE)
    if placeholder.search(text):
        env_path.write_text(placeholder.sub(f"{key}={value}", text, count=1))
    else:
        with env_path.open("a") as f:
            f.write(f"\n{key}={value}\n")


# --------------------------------------------------------------------------
# Step 1: entity secret
# --------------------------------------------------------------------------


def ensure_entity_secret(api_key: str, env_path: Path = ENV_PATH) -> str:
    """Return the entity secret, generating and registering one if needed."""
    if env_has_key("CIRCLE_ENTITY_SECRET", env_path):
        print("[1/3] Entity secret already in .env -- skipping registration.")
        return os.environ["CIRCLE_ENTITY_SECRET"]

    print("[1/3] Generating and registering a new entity secret...")
    entity_secret = os.urandom(32).hex()
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)

    utils.register_entity_secret_ciphertext(
        api_key=api_key,
        entity_secret=entity_secret,
        recoveryFileDownloadPath=str(RECOVERY_DIR),
    )
    append_env("CIRCLE_ENTITY_SECRET", entity_secret, env_path)

    print(f"      Registered. Recovery file saved under {RECOVERY_DIR}/")
    print("      IMPORTANT: back up that recovery file somewhere safe and separate.")
    print("      It is the only way to reset the entity secret if it is lost.")
    return entity_secret


# --------------------------------------------------------------------------
# Step 2: wallet set + wallet
# --------------------------------------------------------------------------


def ensure_wallet(client, env_path: Path = ENV_PATH) -> tuple[str, str]:
    """Return (wallet_id, wallet_address), creating them on ARC-TESTNET if needed."""
    if env_has_key("ARC_WALLET_ID", env_path) and env_has_key("ARC_WALLET_ADDRESS", env_path):
        print("[2/3] Wallet already in .env -- skipping creation.")
        return os.environ["ARC_WALLET_ID"], os.environ["ARC_WALLET_ADDRESS"]

    print(f"[2/3] Creating a wallet set and a wallet on {BLOCKCHAIN}...")
    wallet_sets_api = dcw.WalletSetsApi(client)
    wallets_api = dcw.WalletsApi(client)

    set_request = dcw.CreateWalletSetRequest.from_dict({"name": WALLET_SET_NAME})
    set_response = wallet_sets_api.create_wallet_set(set_request)
    # NOTE: WalletSetResponseData.to_dict() overrides the key to camelCase
    # ("walletSet"), unlike most other fields in this SDK -- confirmed by
    # reading the generated model source directly.
    wallet_set = set_response.data.to_dict()["walletSet"]
    wallet_set_id = wallet_set["id"]

    wallet_request = dcw.CreateWalletRequest.from_dict(
        {
            "blockchains": [BLOCKCHAIN],
            "count": 1,
            "walletSetId": wallet_set_id,
            "accountType": "EOA",
        }
    )
    wallet_response = wallets_api.create_wallet(wallet_request)
    wallet = wallet_response.data.to_dict()["wallets"][0]

    wallet_id, wallet_address = wallet["id"], wallet["address"]
    append_env("ARC_WALLET_ID", wallet_id, env_path)
    append_env("ARC_WALLET_ADDRESS", wallet_address, env_path)

    print(f"      Wallet created: {wallet_address}")
    return wallet_id, wallet_address


# --------------------------------------------------------------------------
# Step 3: funding (manual) + optional balance wait
# --------------------------------------------------------------------------


def get_usdc_balance(client, wallet_id: str) -> float:
    """Read the wallet's current USDC balance (0.0 if none)."""
    response = dcw.WalletsApi(client).list_wallet_balance(id=wallet_id)
    # NOTE: the SDK's to_dict() uses the camelCase alias here ("tokenBalances"),
    # unlike most fields -- see the matching note in src/payments/arc_client.py.
    for entry in response.data.to_dict().get("tokenBalances", []):
        if entry.get("token", {}).get("symbol") == "USDC":
            return float(entry["amount"])
    return 0.0


def guide_funding(wallet_address: str) -> None:
    print("[3/3] Fund the wallet with testnet USDC:")
    print(f"      1. Open {FAUCET_URL}")
    print("      2. Choose network: Arc Testnet, token: USDC")
    print(f"      3. Paste this address: {wallet_address}")
    try:
        webbrowser.open(FAUCET_URL)
    except Exception:
        pass  # No browser available (e.g. WSL without a browser); the URL is printed above.


def wait_for_funds(
    client,
    wallet_id: str,
    timeout_seconds: int = 300,
    interval_seconds: int = 10,
) -> bool:
    """Poll until the wallet holds USDC or the timeout elapses."""
    print(f"      Waiting up to {timeout_seconds}s for USDC to arrive...")
    waited = 0
    while waited < timeout_seconds:
        balance = get_usdc_balance(client, wallet_id)
        if balance > 0:
            print(f"      Funded! Current USDC balance: {balance}")
            return True
        time.sleep(interval_seconds)
        waited += interval_seconds
    print("      Timed out without seeing a balance. Check the faucet and try again.")
    return False


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the Arc testnet wallet.")
    parser.add_argument(
        "--wait", action="store_true", help="After funding guidance, wait for a USDC balance."
    )
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    api_key = os.environ.get("CIRCLE_API_KEY", "")
    if not api_key:
        print("CIRCLE_API_KEY is missing. Create an API key in the Circle Console")
        print("(https://console.circle.com/) and add it to your .env first.")
        return 1

    entity_secret = ensure_entity_secret(api_key)
    client = utils.init_developer_controlled_wallets_client(
        api_key=api_key, entity_secret=entity_secret
    )
    wallet_id, wallet_address = ensure_wallet(client)

    guide_funding(wallet_address)
    if args.wait:
        wait_for_funds(client, wallet_id)

    print("\nDone. Next: re-run the demo with real payments enabled:")
    print("    bash demo/run_demo.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
