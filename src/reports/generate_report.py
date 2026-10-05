"""Generates a human-readable report directly from the audit log.

Deliberately reuses src/audit/store.py rather than capturing any new data --
the report is a view over decisions already made, not a separate pipeline.
"""

from src.audit.store import read_all_entries


def generate_markdown_report(path: str | None = None) -> str:
    """Build a simple Markdown summary of every logged decision."""
    entries = read_all_entries(path) if path else read_all_entries()

    lines = ["# Payment Decisions Report", ""]
    lines.append(f"Total decisions logged: {len(entries)}")
    lines.append("")

    for entry in entries:
        lines.append(f"## Request `{entry.request.request_id}`")
        lines.append(f"- Timestamp: {entry.timestamp}")
        lines.append(f"- Category: {entry.request.category}")
        lines.append(f"- Amount requested: {entry.request.amount_usdc} USDC")
        lines.append(f"- Decision: **{entry.decision}**")
        if entry.reduced_amount is not None:
            lines.append(f"- Approved (reduced) amount: {entry.reduced_amount} USDC")
        lines.append(f"- Reason: {entry.reason}")
        lines.append(f"- Payment executed: {entry.payment_executed}")
        if entry.payment_executed:
            lines.append(f"- Transaction ID: {entry.transaction_id}")
            lines.append(f"- Tx hash: {entry.tx_hash}")
        lines.append(
            f"- Scores: trust={entry.scores.trust.value:.2f} "
            f"(conf {entry.scores.trust.confidence:.2f}), "
            f"urgency={entry.scores.urgency.value:.2f} "
            f"(conf {entry.scores.urgency.confidence:.2f}), "
            f"importance={entry.scores.importance.value:.2f} "
            f"(conf {entry.scores.importance.confidence:.2f})"
        )
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_markdown_report())
