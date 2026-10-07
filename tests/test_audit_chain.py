"""Tests for the hash-chained audit log and its verifier.

These use real files in tmp_path (no mocks) because the whole point is that
tampering with the actual bytes on disk is detected.
"""

import json
import threading

import pytest

from src.audit.logger import AuditEntry
from src.audit.store import append_entry, read_all_entries
from src.audit.verify import verify_chain
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.requests.schema import PaymentRequest, RequestCategory


def make_entry(request_id: str, amount: float = 100.0) -> AuditEntry:
    return AuditEntry(
        timestamp="2026-10-06T12:00:00+00:00",
        request=PaymentRequest(
            request_id=request_id,
            category=RequestCategory.OPERATIONAL_EXPENSE,
            amount_usdc=amount,
            requester="t",
            description="d",
        ),
        scores=DecisionScores(
            trust=ScoreResult(value=0.5, confidence=0.8),
            urgency=ScoreResult(value=0.5, confidence=0.8),
            importance=ScoreResult(value=0.5, confidence=0.8),
        ),
        decision="approve",
        reduced_amount=None,
        reason="r",
        payment_executed=False,
    )


@pytest.fixture
def log(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    for i in range(3):
        append_entry(make_entry(f"r{i}"), path)
    return path


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def test_missing_log_verifies_as_empty(tmp_path):
    assert verify_chain(str(tmp_path / "nope.jsonl")).ok


def test_intact_chain_verifies_and_entries_still_read_back(log):
    report = verify_chain(log)
    assert report.ok and report.chained == 3 and report.bad_line is None
    assert [e.request.request_id for e in read_all_entries(log)] == ["r0", "r1", "r2"]


def test_each_entry_links_to_the_previous_one(log):
    records = [json.loads(line) for line in read_lines(log)]
    assert records[0]["prev_hash"] is None
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert records[2]["prev_hash"] == records[1]["entry_hash"]


def test_editing_an_entry_is_detected_at_that_line(log):
    lines = read_lines(log)
    record = json.loads(lines[1])
    record["request"]["amount_usdc"] = 1.0  # e.g. hide how much was approved
    lines[1] = json.dumps(record)
    write_lines(log, lines)

    report = verify_chain(log)
    assert not report.ok and report.bad_line == 2
    assert "modified" in report.message


def test_deleting_a_middle_entry_is_detected(log):
    lines = read_lines(log)
    del lines[1]
    write_lines(log, lines)

    report = verify_chain(log)
    assert not report.ok and report.bad_line == 2
    assert "does not link" in report.message


def test_reordering_entries_is_detected(log):
    lines = read_lines(log)
    lines[1], lines[2] = lines[2], lines[1]
    write_lines(log, lines)
    assert not verify_chain(log).ok


def test_editing_and_recomputing_one_hash_still_breaks_the_next_link(log):
    """An attacker who rewrites entry 1 AND its own hash is caught by entry 2."""
    from src.audit.store import compute_entry_hash

    lines = read_lines(log)
    record = json.loads(lines[1])
    record["reason"] = "forged"
    record["entry_hash"] = compute_entry_hash(record)
    lines[1] = json.dumps(record)
    write_lines(log, lines)

    report = verify_chain(log)
    assert not report.ok and report.bad_line == 3


def test_garbage_line_is_reported_not_crashed_on(log):
    lines = read_lines(log)
    lines[1] = "{not json"
    write_lines(log, lines)
    assert "not valid JSON" in verify_chain(log).message


def test_legacy_unhashed_prefix_is_accepted_and_chain_continues_from_it(tmp_path):
    path = str(tmp_path / "legacy.jsonl")
    legacy = make_entry("old").model_dump(mode="json")
    legacy.pop("prev_hash"), legacy.pop("entry_hash")  # as written before hashing existed
    write_lines(path, [json.dumps(legacy)])

    append_entry(make_entry("new"), path)

    report = verify_chain(path)
    assert report.ok and report.legacy == 1 and report.chained == 1


def test_unhashed_line_after_chain_started_is_rejected(log):
    lines = read_lines(log)
    stripped = json.loads(lines[2])
    stripped.pop("entry_hash")
    lines[2] = json.dumps(stripped)
    write_lines(log, lines)
    assert not verify_chain(log).ok


def test_concurrent_appends_keep_a_valid_chain(tmp_path):
    path = str(tmp_path / "concurrent.jsonl")
    threads = [
        threading.Thread(target=append_entry, args=(make_entry(f"c{i}"), path)) for i in range(25)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    report = verify_chain(path)
    assert report.ok and report.chained == 25


def test_verify_cli_exit_codes(log, monkeypatch, capsys):
    import sys

    from src.audit.verify import main

    monkeypatch.setattr(sys, "argv", ["verify", log])
    assert main() == 0

    lines = read_lines(log)
    lines[0] = lines[0].replace("100.0", "999.0")
    write_lines(log, lines)
    assert main() == 1
    assert "modified" in capsys.readouterr().out


def test_log_path_is_resolved_at_call_time_not_import_time(tmp_path, monkeypatch):
    """Regression: the path used to be frozen at import, so a value set later
    (e.g. loaded from .env after the module was imported) was ignored."""
    target = tmp_path / "late.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))

    append_entry(make_entry("late-1"))  # no explicit path

    assert target.exists()
    assert [e.request.request_id for e in read_all_entries()] == ["late-1"]
    assert verify_chain().ok
