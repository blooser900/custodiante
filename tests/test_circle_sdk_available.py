"""The Circle SDK must be importable, or nothing else in this project matters.

Why this file exists: the payment and escrow modules are the product, and a
broken Circle install fails with an error that says nothing about the cause:

    ModuleNotFoundError: No module named
      'circle.web3.developer_controlled_wallets.models.create_transfer_transaction_for_developer_request'
    resolving attribute 'TransactionsApi' ... of lazy module ...

That message points at a lazy-import resolution problem, which sends you
looking for a version mismatch that may not exist. This test names the real
requirement instead: the four symbols our code calls must import, and the files
behind them must be readable from this environment.

If this test fails, the fix is at the environment layer, not in our code:
reinstall the SDK (`uv sync --reinstall-package circle-developer-controlled-wallets`),
and if the install itself fails, note that Circle ships this package as an
sdist with no wheel from 9.2.0 onward, so it is built locally on install.
"""

import importlib
import os


def test_the_four_circle_symbols_our_code_uses_import():
    """`src/payments/*` cannot work without exactly these."""
    dcw = importlib.import_module("circle.web3.developer_controlled_wallets")
    utils = importlib.import_module("circle.web3.utils")

    # arc_client.py
    assert dcw.TransactionsApi is not None
    assert dcw.WalletsApi is not None
    assert dcw.CreateTransferTransactionForDeveloperRequest is not None
    assert utils.init_developer_controlled_wallets_client is not None


def test_the_escrow_contract_execution_symbol_imports():
    """escrow.py needs this one, and it is imported on a different code path
    than the transfer symbol, so a partial install can pass the test above."""
    dcw = importlib.import_module("circle.web3.developer_controlled_wallets")
    assert dcw.CreateContractExecutionTransactionForDeveloperRequest is not None


def test_the_sdk_files_behind_those_symbols_are_readable():
    """A file can be listed by the directory yet still fail os.stat/open.

    The import machinery cannot resolve a submodule it cannot stat, so this
    reports the underlying problem directly instead of leaving a confusing
    ModuleNotFoundError. Seen in a sandboxed Windows environment where 11 of
    the SDK's `models/*.py` files were unreadable while 153 others were fine.
    """
    dcw = importlib.import_module("circle.web3.developer_controlled_wallets")
    models_dir = os.path.join(os.path.dirname(dcw.__file__), "models")
    assert os.path.isdir(models_dir), f"SDK layout changed: {models_dir} is not a directory"

    unreadable = []
    for name in sorted(os.listdir(models_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(models_dir, name)
        try:
            with open(path, "rb") as handle:
                if not handle.read(1):
                    unreadable.append(f"{name} (empty)")
        except OSError as error:
            unreadable.append(f"{name} ({type(error).__name__})")

    # A handful of SDK modules are legitimately empty; only flag unreadable
    # ones, which is the condition that breaks the import.
    broken = [item for item in unreadable if "(empty)" not in item]
    assert not broken, (
        "the Circle SDK is installed but some model files cannot be opened "
        f"({len(broken)} of them), so its lazy imports will fail: {broken[:5]}. "
        "Reinstall it: uv sync --reinstall-package circle-developer-controlled-wallets"
    )


def test_the_generated_models_are_readable_one_by_one():
    """The four request models our money paths construct, checked explicitly.

    These are exactly the files that were unreadable in the broken install, so
    a failure here is the direct signal rather than a downstream symptom.
    """
    dcw = importlib.import_module("circle.web3.developer_controlled_wallets")
    models_dir = os.path.join(os.path.dirname(dcw.__file__), "models")
    required = (
        "create_transfer_transaction_for_developer_request.py",
        "create_contract_execution_transaction_for_developer_request.py",
        "create_wallet_upgrade_transaction_for_developer_request.py",
        "estimate_contract_execution_transaction_fee_request.py",
    )
    for name in required:
        path = os.path.join(models_dir, name)
        assert os.path.exists(path), f"{name} is missing from the installed SDK"
        with open(path, "rb") as handle:
            assert handle.read(1), f"{name} is present but empty"

