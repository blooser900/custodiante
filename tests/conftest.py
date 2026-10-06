"""Shared pytest fixtures."""

from unittest.mock import patch

import pytest


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
