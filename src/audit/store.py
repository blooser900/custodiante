"""Persists audit entries to an append-only, hash-chained JSONL file.

One JSON object per line, never rewritten or deleted. Each line carries
`prev_hash` (the previous line's `entry_hash`) and its own `entry_hash`, a
SHA-256 over the line's content. Altering or removing any past line
therefore breaks every hash after it, which src/audit/verify.py detects.

What the chain does NOT detect: truncating lines off the END of the file
(there is no later line left to disagree). Closing that gap needs the latest
`entry_hash` to be published somewhere the log's owner cannot rewrite, e.g.
anchored on-chain.
"""

import hashlib
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

from src.audit.logger import AuditEntry

# Idempotent, and never overrides variables already set in the environment.
# Needed here (not only in pipeline.py) because the verify CLI and the API
# import this module without necessarily importing the pipeline first.
load_dotenv()

DEFAULT_AUDIT_LOG_PATH = "./data/audit_log.jsonl"


def get_audit_log_path() -> str:
    """Resolve the log location at call time, not import time, so a value
    from .env or a test's monkeypatched environment is always honoured."""
    return os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH)

# Serializes append_entry: it reads the last hash and then writes the next
# line, and two threads doing that at once would chain to the same parent.
# In-process only, matching the single-worker deployment (see pipeline.py).
_append_lock = threading.Lock()


def compute_entry_hash(record: dict) -> str:
    """SHA-256 over a log record, excluding its own `entry_hash` field.

    Canonical JSON (sorted keys, no whitespace) so the hash depends only on
    content, never on key order or formatting. Verification runs this over
    the raw parsed line, not a re-serialized model, so legacy lines that
    lack newer fields still hash exactly as they did when written.
    """
    payload = {k: v for k, v in record.items() if k != "entry_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_entry_hash(log_path: Path) -> str | None:
    """Hash of the last chained line in the file, or None if there is none."""
    if not log_path.exists():
        return None
    last: str | None = None
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line).get("entry_hash")
    return last


def append_entry(entry: AuditEntry, path: str | None = None) -> None:
    """Chain a single audit entry to the previous one and append it."""
    log_path = Path(path or get_audit_log_path())
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with _append_lock:
        record = entry.model_dump(mode="json")
        record["prev_hash"] = _last_entry_hash(log_path)
        record["entry_hash"] = compute_entry_hash(record)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_latest_per_request(path: str | None = None) -> list[AuditEntry]:
    """Collapse the append-only log to one entry per request_id: the most
    recent one. A request escalated and later approved/denied by a human
    will have multiple entries in the log; callers that want "current
    status" (the dashboard, the pending-escalations queue) want this view,
    not the raw history.

    Returned newest-first by timestamp.
    """
    latest: dict[str, AuditEntry] = {}
    for entry in read_all_entries(path):
        latest[entry.request.request_id] = entry  # later entries overwrite earlier ones
    return sorted(latest.values(), key=lambda e: e.timestamp, reverse=True)


def read_all_entries(path: str | None = None) -> list[AuditEntry]:
    """Read every audit entry back from the log file, in order."""
    log_path = Path(path or get_audit_log_path())
    if not log_path.exists():
        return []

    entries = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(AuditEntry.model_validate_json(line))
    return entries
