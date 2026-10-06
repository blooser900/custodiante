"""Smoke test for the CLI wrapper. The actual pipeline logic is tested in
tests/test_pipeline.py -- this just confirms main.process_request delegates
to it correctly rather than re-implementing anything.
"""

from unittest.mock import MagicMock, patch

from src.main import process_request
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
