"""Verifies the integrity of the hash-chained audit log.

Usage:
    python -m src.audit.verify [path-to-audit_log.jsonl]

Exits 0 if the chain is intact, 1 if it is broken.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from src.audit.store import compute_entry_hash, get_audit_log_path


@dataclass
class ChainReport:
    ok: bool
    total: int  # non-empty lines examined
    chained: int  # lines that carry a hash
    legacy: int  # leading lines written before hashing existed
    bad_line: int | None  # 1-based line number of the first problem
    message: str


def verify_chain(path: str | None = None) -> ChainReport:
    """Walk the log and check every link of the chain.

    Legacy (unhashed) lines are accepted only as a prefix of the file. Once
    the chain starts, every line must be chained to its predecessor.
    """
    log_path = Path(path or get_audit_log_path())
    if not log_path.exists():
        return ChainReport(True, 0, 0, 0, None, "No audit log yet; nothing to verify.")

    total = chained = legacy = 0
    prev_hash: str | None = None

    with log_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            total += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return ChainReport(False, total, chained, legacy, line_no, f"Line {line_no} is not valid JSON.")

            stored_hash = record.get("entry_hash")
            if stored_hash is None:
                if chained:
                    return ChainReport(
                        False, total, chained, legacy, line_no,
                        f"Line {line_no} has no hash but follows chained entries.",
                    )
                legacy += 1
                continue

            if record.get("prev_hash") != prev_hash:
                return ChainReport(
                    False, total, chained, legacy, line_no,
                    f"Line {line_no} does not link to the previous entry "
                    "(an entry was removed, reordered, or inserted).",
                )
            if compute_entry_hash(record) != stored_hash:
                return ChainReport(
                    False, total, chained, legacy, line_no,
                    f"Line {line_no} was modified after it was written.",
                )
            chained += 1
            prev_hash = stored_hash

    note = f" ({legacy} legacy unhashed entries precede the chain)" if legacy else ""
    return ChainReport(True, total, chained, legacy, None, f"Chain intact: {chained} entries verified{note}.")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    report = verify_chain(path)
    print(report.message)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
