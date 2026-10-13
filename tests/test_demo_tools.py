"""Smoke tests for the demo helpers, which previously rotted unnoticed
(demo/inspect_scores.py imported names that no longer existed)."""

import json

from demo import seed_scenarios
from demo.inspect_scores import build_payload
from src.requests.schema import PaymentRequest


def test_inspect_scores_builds_the_same_payload_the_engine_sends():
    request = PaymentRequest(
        request_id="x",
        category="other",
        amount_usdc=10.0,
        requester="t",
        description="d",
    )
    payload = build_payload(request)
    from src.config import load_config

    expected = {"trust", "urgency", "importance", *load_config().assessment_questions}
    assert set(payload["questions"]) == expected  # core questions AND the configured extras
    assert payload["state"]["amount_usdc"] == 10.0


def test_seeded_scenarios_get_fresh_request_ids_each_run(tmp_path, monkeypatch):
    monkeypatch.setattr(seed_scenarios, "SCENARIOS_DIR", tmp_path)
    times = iter(["1006-100000", "1006-100001"])
    monkeypatch.setattr(seed_scenarios.time, "strftime", lambda _fmt: next(times))

    seed_scenarios.write_scenarios()
    first = json.loads((tmp_path / "demo-approve.json").read_text())["request_id"]
    seed_scenarios.write_scenarios()
    second = json.loads((tmp_path / "demo-approve.json").read_text())["request_id"]

    assert first != second
    # Every file still parses as a valid request.
    for path in tmp_path.glob("*.json"):
        PaymentRequest.model_validate(json.loads(path.read_text()))
