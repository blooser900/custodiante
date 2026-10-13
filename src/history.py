"""What the system itself knows about a requester and a destination.

The decision engine is shown not only the request but also a record of how
past payments to this requester and this destination turned out, computed
here from the audit log. These are facts the system observed, unlike the
free text of a request, which anyone can write.

Two parties, with very different trustworthiness:

    destination   keyed by wallet address. An address cannot be faked, so
                  its history is solid evidence.
    requester     keyed by the name typed into the form. Anyone can type any
                  name and thereby inherit that name's good record, so this
                  history is WEAK evidence until requesters authenticate.
                  It is labelled `identity_basis: "self_declared"` so the
                  engine, and a human reading the log, can tell.

Outcomes counted per party (one per request, from its latest log entry):
    succeeded   money moved (direct transfer, or escrow released)
    failed      a payment was attempted and did not go through. This can be
                the destination's fault (a bad address) or ours (an outage);
                the log cannot tell them apart.
    refunded    an escrow was rejected and refunded
    denied      a human refused an escalated request
Requests still waiting, escalated, or skipped have no outcome yet.

`reliability_index` is (succeeded + 1) / (attempts + 2), Laplace's rule: 0.5
with no history, and it takes several clean payments to climb, so one lucky
payment cannot buy a high score. It is a summary for the engine; `attempts`
is shown beside it so the sample size is never hidden.
"""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:  # AuditEntry imports the engine, which imports this module
    from src.audit.logger import AuditEntry
    from src.requests.schema import PaymentRequest

RECENT_WINDOW = timedelta(hours=24)
_BAD_OUTCOMES = ("failed", "refunded", "denied")


class PartyHistory(BaseModel):
    identity_basis: Literal["self_declared", "wallet_address"]
    known: bool  # seen before in the log
    first_seen_days: float | None  # age of the oldest record
    attempts: int  # requests with a final outcome
    pending: int  # requests still in flight or awaiting a human
    succeeded: int
    failed: int
    refunded: int
    denied: int
    recent_bad_outcomes_24h: int  # failures, refunds and denials in the last day
    avg_amount_usdc: float | None  # average of the payments that succeeded
    reliability_index: float  # 0..1, see module docstring


class HistoryFeatures(BaseModel):
    requester: PartyHistory
    destination: PartyHistory | None  # None when the request names no wallet
    # Current amount divided by that party's average successful payment;
    # None when there is no average to compare against.
    amount_vs_requester_avg: float | None
    amount_vs_destination_avg: float | None


def reliability_index(succeeded: int, attempts: int) -> float:
    """Laplace's rule of succession: see the module docstring."""
    return round((succeeded + 1) / (attempts + 2), 3)


def build_history(
    amount_usdc: float, requester: PartyHistory, destination: PartyHistory | None
) -> HistoryFeatures:
    """Assemble the features for a request of `amount_usdc`, deriving the
    amount-versus-usual ratios from the two parties' averages."""
    return HistoryFeatures(
        requester=requester,
        destination=destination,
        amount_vs_requester_avg=_ratio(amount_usdc, requester.avg_amount_usdc),
        amount_vs_destination_avg=_ratio(
            amount_usdc, destination.avg_amount_usdc if destination else None
        ),
    )


def _outcome(entry: "AuditEntry") -> str | None:
    if entry.human_review == "denied":
        return "denied"
    if entry.payment_executed or entry.escrow_status == "released":
        return "succeeded"
    if entry.escrow_status == "rejected":
        return "refunded"
    if entry.payment_status == "failed":
        return "failed"
    return None


def _amount(entry: "AuditEntry") -> float:
    return entry.reduced_amount or entry.request.amount_usdc


def _when(entry: "AuditEntry") -> datetime:
    moment = datetime.fromisoformat(entry.timestamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _summarize(entries: list["AuditEntry"], basis: str, now: datetime) -> PartyHistory:
    outcomes = [(e, _outcome(e)) for e in entries]
    counts = {name: sum(1 for _, o in outcomes if o == name) for name in ("succeeded", *_BAD_OUTCOMES)}
    attempts = sum(counts.values())
    paid = [_amount(e) for e, o in outcomes if o == "succeeded"]
    recent_bad = sum(1 for e, o in outcomes if o in _BAD_OUTCOMES and now - _when(e) <= RECENT_WINDOW)
    first_seen = min((_when(e) for e in entries), default=None)
    return PartyHistory(
        identity_basis=basis,
        known=bool(entries),
        first_seen_days=round((now - first_seen).total_seconds() / 86400, 2) if first_seen else None,
        attempts=attempts,
        pending=sum(1 for _, o in outcomes if o is None),
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        refunded=counts["refunded"],
        denied=counts["denied"],
        recent_bad_outcomes_24h=recent_bad,
        avg_amount_usdc=round(sum(paid) / len(paid), 2) if paid else None,
        reliability_index=reliability_index(counts["succeeded"], attempts),
    )


def _ratio(amount: float, average: float | None) -> float | None:
    return round(amount / average, 2) if average else None


def compute_history(
    request: "PaymentRequest", entries: list["AuditEntry"], now: datetime | None = None
) -> HistoryFeatures:
    """Compute the history features for `request` from the audit log.

    `entries` must be the latest entry of each request, i.e. what
    read_latest_per_request() returns. The request being decided is not in
    the log yet, so it never counts toward its own history.
    """
    now = now or datetime.now(timezone.utc)
    requester_key = request.requester.strip().lower()
    mine = [e for e in entries if e.request.requester.strip().lower() == requester_key]
    requester = _summarize(mine, "self_declared", now)

    destination = None
    if request.counterparty_wallet:
        wallet = request.counterparty_wallet.strip().lower()
        theirs = [
            e for e in entries if (e.request.counterparty_wallet or "").strip().lower() == wallet
        ]
        destination = _summarize(theirs, "wallet_address", now)

    return build_history(request.amount_usdc, requester, destination)
