"""Append-only decision log.

Every decision the system makes is written here: what was seen, what the
engine scored, what rule applied, and what happened as a result. This is
the audit trail a reviewer should be able to replay without needing to
reconstruct anything from bank statements.
"""

import hashlib
import json
from datetime import datetime, timezone

from pydantic import BaseModel

from src.config import RulesConfig, load_config
from src.decision_engine.engine import DecisionScores, ScoreResult
from src.policy.rules import PolicyDecision
from src.requests.schema import PaymentRequest, RequestCategory


class FailedAttempt(BaseModel):
    """An escrow action that was tried and failed.

    Recorded on a copy of the milestone's current entry, so the escrow's
    state is unchanged (funds are exactly where they were) while the audit
    log still shows who tried what, when, and why it did not go through.
    """

    action: str  # "fund" | "deliver" | "release" | "reject"
    error: str
    # The failed transaction, when one was actually sent. A transaction that
    # reverts on-chain still has a hash and still cost gas.
    transaction_id: str | None = None
    tx_hash: str | None = None


def config_snapshot(config: RulesConfig) -> dict:
    """The config as it should be stored on an entry: JSON-ready and complete.

    `mode="json"` so what lands in the log is exactly what the API and the
    dashboard would return -- enums and paths already reduced to plain values,
    nothing that needs a Python type to be read back.
    """
    return config.model_dump(mode="json")


def config_fingerprint(snapshot: dict) -> str:
    """A short, stable fingerprint of a snapshot.

    Canonical JSON with sorted keys so the same config always gives the same
    string, whatever order the fields were built in. SHA-256 truncated to 16
    hex characters: this is a label for humans to compare and group by, not a
    security boundary -- the snapshot itself is right there next to it.
    """
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _current_config_snapshot() -> tuple[dict, str]:
    """The rules in force right now, and their fingerprint."""
    snapshot = config_snapshot(load_config())
    return snapshot, config_fingerprint(snapshot)


class AuditEntry(BaseModel):
    timestamp: str
    request: PaymentRequest
    scores: DecisionScores
    decision: str
    reduced_amount: float | None
    reason: str
    payment_executed: bool
    # Populated only when payment_executed is True. Without these, the audit
    # trail cannot actually be verified against the chain or against Circle's
    # own console -- which defeats the point of having an audit trail at all.
    transaction_id: str | None = None
    tx_hash: str | None = None
    # Set only on an entry created by a human override of an escalated
    # request (see src/pipeline.py:apply_human_override). None means this
    # entry reflects the automatic pipeline only.
    human_review: str | None = None  # "approved" | "denied" | None
    reviewer: str | None = None
    # Which decision engine produced `scores` (e.g. "kev", "jev", "mock").
    # None on entries written before this field existed.
    engine_name: str | None = None
    # What happened to the on-chain payment, in a form a reviewer can read
    # without digging through stdout:
    #   "executed"       -- transfer completed
    #   "failed"         -- a transfer was attempted and errored (see payment_detail)
    #   "skipped"        -- approved, but not attempted (see payment_detail)
    #   "not_applicable" -- no payment was due (escalated or denied)
    # None on entries written before this field existed; fall back to
    # `payment_executed` when reading those.
    payment_status: str | None = None
    payment_detail: str | None = None
    # Milestone escrow (see src/payments/escrow.py). `escrow_job_id` is the
    # ERC-8183 job holding (or about to hold) the funds; `escrow_status`
    # tracks our side of its lifecycle: open -> funded -> submitted ->
    # released | rejected. Both None for requests that were paid directly.
    escrow_job_id: int | None = None
    escrow_status: str | None = None
    # Crash-recovery marker (see src/pipeline.py:_pending_entry). Set on the
    # row written BEFORE any money moves, describing what we are about to do,
    # and cleared by the row that records the outcome. While it is present the
    # payment is unaccounted for: a retry must reconcile, not re-pay.
    payment_attempt: str | None = None
    payment_amount_usdc: float | None = None
    # Which try at paying this request the row belongs to (None = the first, and
    # on rows written before this field existed). It selects the Circle
    # idempotency key (`payment_idempotency_key`): a retry after a transfer that
    # definitely failed bumps it, a resend of one whose fate is unknown does not.
    # Optional on purpose: the hash chain is computed over the raw line, so older
    # rows without it verify exactly as they were written.
    attempt_number: int | None = None
    # The most recent failed escrow action, cleared by the next success.
    failed_attempt: FailedAttempt | None = None
    # ---- R2-09: the rules in force -------------------------------------------
    # The FULL normalized config this decision was made under, so a reviewer can
    # reconstruct it without the rules file as it was that day -- which nobody
    # kept, and which `load_config()` re-reads on every request. A hash alone
    # would prove the rules changed without saying what they were, which is why
    # the whole snapshot is stored (see docs/revisao-priorizada.md, R2-09).
    #
    # The EFFECTIVE config, after defaults and coercion, not the raw file text:
    # that is what the policy actually read.
    config_snapshot: dict | None = None
    # Fingerprint of `config_snapshot`, for grouping and comparing at a glance.
    config_hash: str | None = None
    # Set only on a `config_change` entry: the config it replaced, so the change
    # itself is auditable in both directions.
    previous_config_snapshot: dict | None = None
    previous_config_hash: str | None = None
    # Hash chain (set by src/audit/store.py at write time, never by callers):
    # `entry_hash` covers this entry's content plus `prev_hash`, and
    # `prev_hash` is the previous entry's `entry_hash`. Editing or removing
    # any past line breaks every hash after it. None on legacy entries.
    prev_hash: str | None = None
    entry_hash: str | None = None


def build_entry(
    request: PaymentRequest,
    scores: DecisionScores,
    policy_decision: PolicyDecision,
    payment_executed: bool,
    payment_result: dict | None = None,
    payment_status: str | None = None,
    payment_detail: str | None = None,
    engine_name: str | None = None,
    escrow_job_id: int | None = None,
    escrow_status: str | None = None,
    payment_attempt: str | None = None,
    payment_amount_usdc: float | None = None,
) -> AuditEntry:
    """Assemble a single audit entry from one full decision cycle.

    `payment_result` is whatever ArcPaymentClient.send_payment returned
    (transaction_id, tx_hash, etc.), if a payment was attempted and
    succeeded. Pass None if no payment was executed.

    `payment_attempt` marks a row written before the money moved (see
    src/pipeline.py:_pending_entry); it must be cleared by the row that
    records the outcome.

    Every entry built here carries the rules that were in force (R2-09), read
    fresh: `load_config` is cheap and the file is tiny, and a decision whose
    rules are not reconstructible is not auditable. The rows derived from this
    one (the outcome row, a retry) inherit the snapshot by being `model_copy`s
    of it, so they cannot disagree about which rules applied.
    """
    snapshot, fingerprint = _current_config_snapshot()
    return AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=request,
        scores=scores,
        decision=policy_decision.decision.value,
        reduced_amount=policy_decision.reduced_amount,
        reason=policy_decision.reason,
        payment_executed=payment_executed,
        transaction_id=(payment_result or {}).get("transaction_id"),
        tx_hash=(payment_result or {}).get("tx_hash"),
        engine_name=engine_name,
        payment_status=payment_status,
        payment_detail=payment_detail,
        escrow_job_id=escrow_job_id,
        escrow_status=escrow_status,
        payment_attempt=payment_attempt,
        payment_amount_usdc=payment_amount_usdc,
        config_snapshot=snapshot,
        config_hash=fingerprint,
    )


def record_config_change(
    before: RulesConfig,
    after: RulesConfig,
    actor: str = "operator",
    note: str | None = None,
    request_id: str | None = None,
) -> AuditEntry:
    """Append a config CHANGE to the audit log, with both sides of the change.

    `PUT /api/config` used to rewrite the rules with no entry in the log at all:
    a system that recorded every payment but not the rule change that made the
    payment legal was auditing the wrong thing. The entry goes into the SAME
    chain, so deleting a change is detected exactly like deleting a payment --
    a separate file would just be one more thing to forget to verify.

    The change is not a payment, so it gets a `decision` of its own
    ("config_change") that no rule, queue or counter matches, and every
    payment-shaped field stays at its "nothing happened" value.

    Handling of `request`: a config change has no payment request, but the log's
    per-request view keys on `request.request_id`, so this carries a placeholder
    that says what it is (`system:config`) rather than inventing a fake payment.
    `request_id` is unique per change (defaults to a timestamp) so two changes
    never collapse into one row in that view.
    """
    if request_id is None:
        request_id = f"config-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"

    snapshot = config_snapshot(after)
    fingerprint = config_fingerprint(snapshot)
    previous_snapshot = config_snapshot(before)
    previous_fingerprint = config_fingerprint(previous_snapshot)

    changed = fingerprint != previous_fingerprint
    reason = (
        f"The payment rules were changed by {actor}."
        if changed
        else f"The payment rules were saved by {actor} (no effective change)."
    )
    if note:
        reason += f" Note: {note}"
    reason += (
        f" Rules in force before: {previous_fingerprint}; after: {fingerprint}."
        " Both versions are stored in full on this entry."
    )

    placeholder = ScoreResult(value=0.0, confidence=0.0)
    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        request=PaymentRequest(
            request_id=request_id,
            category=RequestCategory.OTHER,
            # A config change moves no money; the model requires a positive
            # amount, so this is a documented sentinel, not an amount.
            amount_usdc=1.0,
            requester=f"system:config/{actor}",
            description="Configuration change. This entry is not a payment request.",
        ),
        scores=DecisionScores(
            trust=placeholder, urgency=placeholder, importance=placeholder
        ),
        decision="config_change",
        reduced_amount=None,
        reason=reason,
        payment_executed=False,
        payment_status="not_applicable",
        reviewer=actor,
        human_review=None,
        config_snapshot=snapshot,
        config_hash=fingerprint,
        previous_config_snapshot=previous_snapshot,
        previous_config_hash=previous_fingerprint,
    )

    # Imported here, not at the top: `store` imports this module for the entry
    # model, so a module-level import would be circular.
    from src.audit.store import append_entry

    append_entry(entry)
    return entry
