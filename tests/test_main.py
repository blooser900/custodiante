"""Smoke test for the CLI wrapper. The actual pipeline logic is tested in
tests/test_pipeline.py -- this just confirms main.process_request delegates
to it correctly rather than re-implementing anything.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.main import main, process_request
from src.requests.schema import PaymentRequest, RequestCategory


def test_process_request_delegates_to_run_pipeline():
    request = PaymentRequest(
        request_id="cli-test-1",
        category=RequestCategory.OTHER,
        amount_usdc=10.0,
        requester="test-suite",
        description="CLI delegation smoke test.",
    )
    engine = MagicMock()
    arc_client = MagicMock()

    with patch("src.main.run_pipeline") as mock_run_pipeline:
        process_request(request, engine=engine, arc_client=arc_client)

    mock_run_pipeline.assert_called_once_with(
        request, engine=engine, arc_client=arc_client, verbose=True
    )


def test_main_prints_short_message_on_runtime_error(tmp_path, capsys, monkeypatch):
    """A RuntimeError from the pipeline (engine unreachable, etc.) should
    produce a short stderr message and exit code 1 -- not a full traceback.
    """
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps(
            {
                "request_id": "cli-error-test",
                "category": "other",
                "amount_usdc": 10.0,
                "requester": "test-suite",
                "description": "Triggers a pipeline failure.",
            }
        )
    )

    monkeypatch.setattr(sys, "argv", ["main.py", "--input", str(request_file)])
    with patch("src.main.process_request", side_effect=RuntimeError("engine unreachable")):
        exit_code = main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "engine unreachable" in captured.err
