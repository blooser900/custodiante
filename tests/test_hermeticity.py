"""Hermeticity of the suite itself.

These are tests about the tests: running the suite must not write to the
developer's working tree. `load_config()` creates `config/rules.yaml` with
defaults when the file is missing, and several tests legitimately ask for
those defaults -- so without the `isolate_config_file` fixture in conftest.py
the suite regenerates a real config file on every run.

The second half came from the same class of mistake, found by looking at the
tree rather than at the code: the suite also APPENDED to the real
`data/audit_log.jsonl`. Nothing noticed while only the payment paths wrote
there, because those tests mock `append_entry`; once `PUT /api/config` started
recording a config change, `tests/test_api.py` put real rows in the
developer's log on every run.
"""

from pathlib import Path

from src.config import load_config

# The real locations, derived from this file rather than imported: importing
# CONFIG_PATH here would run while the conftest fixture is already active and
# would therefore hand back the isolated path.
ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG_PATH = ROOT / "config" / "rules.yaml"
REAL_AUDIT_LOG = ROOT / "data" / "audit_log.jsonl"


def test_running_the_suite_does_not_create_or_modify_the_real_config_file():
    """A default-path load_config() must not touch the working tree.

    The assertion is on the file's CONTENT and mtime, not just its existence.
    `config/rules.yaml` is git-ignored and generated on demand, so a developer
    who has run the demo or the CLI legitimately has one -- and a test that
    only asked "does it exist?" then reported the suite as non-hermetic when
    it had written nothing at all. Measured: with the file absent the suite
    leaves it absent, and with it present the suite leaves it byte-identical.
    """
    existed_before = REAL_CONFIG_PATH.exists()
    before = REAL_CONFIG_PATH.read_bytes() if existed_before else None
    before_mtime = REAL_CONFIG_PATH.stat().st_mtime if existed_before else None

    load_config()  # would write config/rules.yaml if the fixture were absent

    if not existed_before:
        assert not REAL_CONFIG_PATH.exists(), (
            f"the suite created {REAL_CONFIG_PATH}; load_config() must be redirected "
            "to a temporary path by the isolate_config_file fixture"
        )
        return

    assert REAL_CONFIG_PATH.read_bytes() == before, (
        f"the suite rewrote {REAL_CONFIG_PATH}; load_config() must be redirected "
        "to a temporary path by the isolate_config_file fixture"
    )
    assert REAL_CONFIG_PATH.stat().st_mtime == before_mtime, (
        f"the suite touched {REAL_CONFIG_PATH}; load_config() must be redirected "
        "to a temporary path by the isolate_config_file fixture"
    )


def test_load_config_still_returns_the_defaults_under_isolation():
    """Redirecting the path must not change what a default read returns."""
    config = load_config()
    assert config.category_limits["contractor_milestone"].max_usdc == 2000.0
    assert len(config.criteria.trust) == 3
    assert config.assessment_questions  # the extras are on by default


# --------------------------------------------------------------------------
# The audit log, not just the config
# --------------------------------------------------------------------------


def test_the_suite_does_not_write_to_the_real_audit_log():
    """The suite appended real rows to `data/audit_log.jsonl`.

    Written as a real end-to-end action rather than an assertion about the path
    helper, because the leak came from a route that nobody expected to write:
    `PUT /api/config` recording a config change. If the isolation is removed, or
    a future route writes through `append_entry`, this fails.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from src.api import app
    from src.config import _default_config
    from tests.conftest import MANAGER_AUTH
    from tests.rules_fixtures import rules_config

    existed = REAL_AUDIT_LOG.exists()
    before = REAL_AUDIT_LOG.read_bytes() if existed else None

    client = TestClient(app, headers=MANAGER_AUTH)
    # A config change is a real write on the default path: the exact action that
    # leaked into the developer's log.
    with patch("src.api.load_config", return_value=rules_config()), patch(
        "src.policy.ceiling.require_limits_within_balance", return_value=None
    ):
        response = client.put("/api/config", json=_default_config().model_dump(mode="json"))

    assert response.status_code == 200

    if not existed:
        assert not REAL_AUDIT_LOG.exists(), (
            f"the suite created {REAL_AUDIT_LOG}; writes must be redirected to a "
            "temporary path by the isolate_writes_from_the_real_audit_log fixture"
        )
        return
    assert REAL_AUDIT_LOG.read_bytes() == before, (
        f"the suite appended to {REAL_AUDIT_LOG}; writes must be redirected to a "
        "temporary path by the isolate_writes_from_the_real_audit_log fixture"
    )


def test_a_pipeline_run_does_not_write_to_the_real_audit_log():
    """The payment path too, not only the config route."""
    from unittest.mock import MagicMock, patch

    from src.decision_engine.engine import DecisionScores, ScoreResult
    from src.pipeline import run_pipeline
    from src.requests.schema import PaymentRequest, RequestCategory
    from tests.rules_fixtures import rules_config

    existed = REAL_AUDIT_LOG.exists()
    before = REAL_AUDIT_LOG.read_bytes() if existed else None

    engine = MagicMock()
    engine.score.return_value = DecisionScores(
        trust=ScoreResult(value=0.95, confidence=0.9),
        urgency=ScoreResult(value=0.1, confidence=0.9),
        importance=ScoreResult(value=0.1, confidence=0.9),
    )
    request = PaymentRequest(
        request_id="hermeticity-check",
        category=RequestCategory.OTHER,
        amount_usdc=1.0,
        requester="t",
        description="d",
    )
    with patch("src.pipeline.DEFAULT_ENGINE_FACTORY", return_value=engine), patch(
        "src.policy.rules.load_config", return_value=rules_config()
    ):
        run_pipeline(request, arc_client=MagicMock(), verbose=False)

    if not existed:
        assert not REAL_AUDIT_LOG.exists(), "a pipeline run created the real audit log"
        return
    assert REAL_AUDIT_LOG.read_bytes() == before, "a pipeline run appended to the real audit log"
