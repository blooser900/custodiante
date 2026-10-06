"""Persists audit entries to an append-only JSONL file.

Simple on purpose: one JSON object per line, never rewritten or deleted.
Swap this for a signed/hash-chained store later if the audit trail needs to
be tamper-evident for the demo.
"""

import os
from pathlib import Path

from src.audit.logger import AuditEntry

AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "./data/audit_log.jsonl")


def append_entry(entry: AuditEntry, path: str = AUDIT_LOG_PATH) -> None:
    """Append a single audit entry to the log file."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as f:
        f.write(entry.model_dump_json() + "\n")


def read_latest_per_request(path: str = AUDIT_LOG_PATH) -> list[AuditEntry]:
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


def read_all_entries(path: str = AUDIT_LOG_PATH) -> list[AuditEntry]:
    """Read every audit entry back from the log file, in order."""
    log_path = Path(path)
    if not log_path.exists():
        return []

    entries = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(AuditEntry.model_validate_json(line))
    return entries
