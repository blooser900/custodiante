"""Shared pytest fixtures.

Three jobs, all about the suite not touching -- and not being touched by -- the
developer's real machine:

1. `isolate_pipeline_from_real_audit_log` keeps tests off the real
   `data/audit_log.jsonl`.
2. `isolate_config_file` keeps tests off the real `config/rules.yaml`. Without
   it, `load_config()` -- which WRITES the file with defaults when it is
   missing -- recreates `config/rules.yaml` in the working directory on every
   run, because several tests legitimately ask for the real defaults.
3. `neutralise_ambient_env` stops a developer's `.env` from changing what the
   suite measures. `src.auth`, `src.pipeline` and `src.audit.store` all call
   `load_dotenv()` at import time, so without this a real `DECISION_ENGINE` or
   `CIRCLE_API_KEY` silently alters test behaviour.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import src.config as config_module

# The token every test uses unless it is specifically testing auth.
MANAGER_TOKEN = "test-manager-token-0123456789"
MANAGER_AUTH = {"Authorization": f"Bearer {MANAGER_TOKEN}"}

# Where the isolated config file lives. Removed after each test, so the suite
# leaves nothing behind.
ISOLATED_CONFIG_PATH = Path(__file__).resolve().parent / ".rules-under-test.yaml"

# Variables that change behaviour if inherited from a real .env. Tests that
# care about them set them explicitly (usually with patch.dict(..., clear=True)).
_AMBIENT_VARS_TO_CLEAR = (
    "DECISION_ENGINE",
    "JEV_API_KEY",
    "JEV_MODEL",
    "JEV_API_URL",
    "FALLBACK_API_KEY",
    "FALLBACK_API_URL",
    "FALLBACK_MODEL",
    "CIRCLE_API_KEY",
    "CIRCLE_ENTITY_SECRET",
    "ARC_WALLET_ADDRESS",
    "ARC_WALLET_ID",
    "ESCROW_ENABLED",
    "ESCROW_PROVIDER_WALLET_ADDRESS",
    "ESCROW_CONTRACT_ADDRESS",
    "ENGINE_TIMEOUT_SECONDS",
    "AUDIT_LOG_PATH",
)


@pytest.fixture(autouse=True)
def manager_token_env(monkeypatch):
    monkeypatch.setenv("MANAGER_TOKEN", MANAGER_TOKEN)
    monkeypatch.delenv("REQUESTER_ACCESS_CODE", raising=False)


@pytest.fixture(autouse=True)
def neutralise_ambient_env(monkeypatch):
    """A developer's .env must not decide what the suite measures."""
    for name in _AMBIENT_VARS_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_pipeline_from_real_audit_log():
    """By default no test should read the developer's real data/audit_log.jsonl.

    The pipeline reads the log to detect duplicate request_ids and to
    re-check human reviews; without this, a leftover log from a real run
    could make unrelated tests fail with DuplicateRequestError. Tests that
    exercise that behaviour patch `src.pipeline.read_latest_per_request`
    themselves, which takes precedence over this default.
    """
    with patch("src.pipeline.read_latest_per_request", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def isolate_config_file():
    """Keep every default-path `load_config()` off the real config/rules.yaml.

    Patching the `CONFIG_PATH` *attribute* does not work here, and
    `tests/test_manage_rules.py:5` had already documented why: `load_config`
    and `save_config` take `path=CONFIG_PATH` as a **default argument**, frozen
    at import time. The attribute can be patched and the frozen default still
    points at the real file (verified directly).

    Patching the module attribute is also not enough on its own for another
    reason: modules that did `from src.config import load_config` hold their own
    reference to that function object, so a wrapper on `src.config` would not be
    seen by `src.policy.limits`, `src.policy.rules`, etc.

    Both problems have the same fix: replace `__defaults__` on the function
    object itself, which every importer shares. An explicit `path` argument
    still wins, so `test_config.py` and friends, which pass `tmp_path`, are
    unaffected.
    """
    ISOLATED_CONFIG_PATH.unlink(missing_ok=True)
    original_load_defaults = config_module.load_config.__defaults__
    original_save_defaults = config_module.save_config.__defaults__
    config_module.load_config.__defaults__ = (ISOLATED_CONFIG_PATH,)
    config_module.save_config.__defaults__ = (ISOLATED_CONFIG_PATH,)
    try:
        yield
    finally:
        config_module.load_config.__defaults__ = original_load_defaults
        config_module.save_config.__defaults__ = original_save_defaults
        ISOLATED_CONFIG_PATH.unlink(missing_ok=True)
