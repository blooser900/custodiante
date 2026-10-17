"""HTTP API for the two audiences of the app.

    /                  requester portal (ui/requester.html): submit a payment
                       request and follow its status
    /manager           manager dashboard (ui/manager.html)
    /manager/settings  decision rules (ui/settings.html)

Routes are split by audience, and the split is enforced HERE, not in the UI:
every /api route except /api/requester/* requires the manager token (see
src/auth.py). The requester routes return only the allow-listed fields in
src/public_view.py.

Run with:
    uvicorn src.api:app --reload --port 8000
"""

import logging

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.audit.store import read_latest_per_request
from src.auth import access_code_required, require_manager, require_requester_access
from src.audit.logger import record_config_change
from src.audit.verify import verify_chain
from src.config import RulesConfig, load_config, save_config
from src.money import InvalidAmountError, parse_usdc, sum_usdc
from src.payments.escrow import ChainReader, EscrowError
from src.policy.ceiling import LimitAboveCeilingError, require_limits_within_balance
from src.pipeline import (
    AlreadyReviewedError,
    DuplicateRequestError,
    MilestoneError,
    MilestoneNotFoundError,
    PaymentInFlightError,
    PaymentLookupError,
    PaymentNotFoundError,
    PaymentStateError,
    advance_milestone,
    apply_human_override,
    provider_is_ours,
    reconcile_payment,
    retry_payment,
    run_pipeline,
)
from src.public_view import REQUESTER_ID_PREFIX, new_request_id, to_public_status
from src.requests.schema import PaymentRequest, RequestCategory

# Interactive docs (/docs, /redoc, /openapi.json) would publish the full route
# map to unauthenticated visitors, so they are disabled.
app = FastAPI(title="Tameion Payment Agent", docs_url=None, redoc_url=None, openapi_url=None)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

logger = logging.getLogger(__name__)

# Every route a manager needs. The dependency applies to all of them, so a
# route added to this router later cannot be left open by forgetting a check.
manager = APIRouter(dependencies=[Depends(require_manager)])
# Routes for people asking for payments. Nothing here may return an AuditEntry.
requester = APIRouter(prefix="/api/requester")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Without this, any failure inside the decision engine or Arc client
    # (a missing API key, a network error, Jev/Kev being unreachable) shows
    # up to the dashboard as an opaque "Internal Server Error" with no
    # detail, since FastAPI's default handler doesn't expose exception
    # messages. The manager dashboard's error handling expects a `detail`.
    if request.url.path.startswith("/api/requester"):
        # Requesters are unauthenticated: an exception message can contain
        # internal URLs, wallet ids or provider errors. Keep it server-side.
        logger.error("Unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "We could not process your request right now. Please try again later."},
        )
    return JSONResponse(status_code=502, content={"detail": str(exc)})


class EscalationDecision(BaseModel):
    action: Literal["approve", "deny"]
    reviewer: str


class MilestoneAction(BaseModel):
    reviewer: str = "operator"
    reason: str | None = None

# Escrow states in which funds are still locked or the job is still open.
ACTIVE_ESCROW_STATUSES = ("open", "funded", "submitted")


@app.get("/")
def requester_portal() -> FileResponse:
    return FileResponse(UI_DIR / "requester.html")


# The pages carry no data: everything sensitive is fetched from the
# token-protected API after login, so serving the HTML itself is safe.
@app.get("/manager")
def manager_dashboard() -> FileResponse:
    return FileResponse(UI_DIR / "manager.html")


@app.get("/manager/settings")
def settings_page() -> FileResponse:
    return FileResponse(UI_DIR / "settings.html")


@app.get("/ui/manager-auth.js")
def manager_auth_script() -> FileResponse:
    return FileResponse(UI_DIR / "manager-auth.js", media_type="application/javascript")


@manager.get("/api/config")
def get_config() -> dict:
    return load_config().model_dump()


def _config_actor(request: Request) -> str:
    """Who changed the rules, for the audit entry.

    Alpha scope: one shared manager token, so there is no per-user identity to
    read. The best available answer is an explicit header, falling back to the
    client address -- an operator can say who they are, and if they do not, the
    entry still records that *someone* with the token changed the rules from
    that address. When the token becomes per-user, only this function changes.
    """
    declared = request.headers.get("X-Operator", "").strip()
    if declared:
        return declared[:100]
    return f"manager@{request.client.host if request.client else 'unknown'}"


@manager.put("/api/config")
def update_config(config: RulesConfig, request: Request) -> dict:
    # RulesConfig's own validators (threshold ranges, 2-10 rubric levels)
    # already ran by the time FastAPI builds this parameter -- an invalid
    # body never reaches here, it gets a 422 automatically.
    #
    # The change goes into the audit log, with BOTH versions, BEFORE it is
    # persisted. A log that records every payment but not the rule change that
    # made the payment legal is auditing the wrong thing, and recording it first
    # means a crash during the save still leaves the intent on the log.
    #
    # The ceiling check is the last thing before the write: a category limit
    # above the funds available is not a limit. It fails open when the balance
    # cannot be read -- see src/policy/ceiling.py -- so an unreachable Circle
    # never makes the rules uneditable.
    try:
        require_limits_within_balance(config)
    except LimitAboveCeilingError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    before = load_config()
    record_config_change(before, config, actor=_config_actor(request))
    save_config(config)
    return config.model_dump()


def _payment_requests() -> list:
    """The log's per-request view, minus events that are not requests.

    A config change lives in the same chain and therefore in the same
    per-request view, keyed by its own id. It is an audit event, not a payment
    request: counting it as one would inflate `total_requests` and could put it
    in the escalation queue. The exclusion is explicit and in one place so a
    future event type has one thing to update.
    """
    return [e for e in read_latest_per_request() if e.decision != "config_change"]


@manager.get("/api/stats")
def stats() -> dict:
    entries = _payment_requests()
    pending = [e for e in entries if e.decision == "escalate" and e.human_review is None]
    paid_total = sum_usdc(
        (e.reduced_amount or e.request.amount_usdc) for e in entries if e.payment_executed
    )
    return {
        "total_requests": len(entries),
        "pending_count": len(pending),
        "paid_total_usdc": float(round(paid_total, 2)),
        "payments_executed": sum(1 for e in entries if e.payment_executed),
        # Approved requests whose on-chain payment was attempted and failed:
        # these need a human to look at them.
        "payments_failed": sum(1 for e in entries if e.payment_status == "failed"),
        # Payments recorded as sent but never confirmed (a crash between the
        # transfer and the outcome row). These need reconciliation against
        # Circle/Arc before anything can be retried -- see pipeline._in_flight.
        "payments_in_flight": sum(1 for e in entries if e.payment_attempt is not None),
        # Milestone escrow: USDC currently locked in contracts (funded, not
        # yet released or refunded), and how many milestones are in flight.
        "escrowed_total_usdc": float(
            round(
                sum_usdc(
                    (e.reduced_amount or e.request.amount_usdc)
                    for e in entries
                    if e.payment_status == "escrowed"
                ),
                2,
            )
        ),
        "milestones_active": sum(1 for e in entries if e.escrow_status in ACTIVE_ESCROW_STATUSES),
        # Active milestones whose latest escrow action failed and still
        # awaits a retry or a decision.
        "milestones_needing_attention": sum(
            1
            for e in entries
            if e.failed_attempt is not None and e.escrow_status in ACTIVE_ESCROW_STATUSES
        ),
    }


@manager.get("/api/audit/verify")
def verify_audit_log() -> dict:
    """Check the audit log's hash chain; `ok` is False if it was tampered with."""
    return asdict(verify_chain())


@manager.get("/api/requests/latest")
def latest_requests() -> list[dict]:
    return [e.model_dump() for e in _payment_requests()]


@manager.get("/api/escalations/pending")
def pending_escalations() -> list[dict]:
    entries = _payment_requests()
    pending = [e for e in entries if e.decision == "escalate" and e.human_review is None]
    return [e.model_dump() for e in pending]


@manager.post("/api/requests")
def submit_request(request: PaymentRequest) -> dict:
    try:
        entry = run_pipeline(request, verbose=False)
    except (DuplicateRequestError, PaymentInFlightError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidAmountError as e:
        # Not a payable amount (infinite, or finer than USDC's 6 decimals).
        raise HTTPException(status_code=422, detail=str(e)) from e
    return entry.model_dump()


@manager.post("/api/escalations/{request_id}/decision")
def decide_escalation(request_id: str, decision: EscalationDecision) -> dict:
    entries = read_latest_per_request()
    original = next((e for e in entries if e.request.request_id == request_id), None)

    if original is None:
        raise HTTPException(status_code=404, detail=f"No request found with id {request_id!r}.")
    if original.payment_attempt is not None:
        # Checked first: an approval whose payment is unconfirmed is no longer
        # "escalated" (it reads decision="approve"), and the useful answer is
        # to reconcile the payment, not that there is nothing to review.
        raise HTTPException(
            status_code=409,
            detail=f"Request {request_id!r} has a payment in flight "
            f"({original.payment_attempt}). Refusing to pay again: "
            "reconcile it against Circle/Arc first.",
        )
    if original.decision != "escalate":
        raise HTTPException(
            status_code=409,
            detail=f"Request {request_id!r} is not in an escalated state (current: "
            f"{original.decision!r}); nothing to review.",
        )
    if original.human_review is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Request {request_id!r} was already {original.human_review} "
            f"by {original.reviewer!r}.",
        )

    try:
        entry = apply_human_override(original, decision.action, decision.reviewer)
    except (AlreadyReviewedError, PaymentInFlightError) as e:
        # AlreadyReviewedError: lost a race with another reviewer between the
        # check above and the lock inside apply_human_override.
        # PaymentInFlightError: a payment is recorded but unconfirmed, so
        # approving again would pay twice.
        raise HTTPException(status_code=409, detail=str(e)) from e
    return entry.model_dump()


def _chain_reader() -> ChainReader:
    """Read-only chain access; a function so tests can substitute it."""
    return ChainReader()


@manager.get("/api/milestones/active")
def active_milestones() -> list[dict]:
    """Milestones whose escrow is still open or holding funds, each with the
    job's live on-chain status (the chain, not our log, is the truth: a real
    contractor submits their deliverable without telling us)."""
    entries = [e for e in read_latest_per_request() if e.escrow_status in ACTIVE_ESCROW_STATUSES]
    if not entries:
        return []
    reader = _chain_reader()
    items = []
    for entry in entries:
        item = entry.model_dump()
        # Lets the UI offer "simulate delivery" only when we hold the
        # contractor's key (a demo setup); a real contractor submits alone.
        item["demo_contractor"] = provider_is_ours(entry.request)
        try:
            job = reader.get_job(entry.escrow_job_id)
            item["chain_status"] = job.status
            item["chain_budget_usdc"] = job.budget_usdc
        except Exception as e:  # noqa: BLE001 -- an RPC hiccup must not hide the milestone
            item["chain_status"] = None
            item["chain_error"] = str(e)
        items.append(item)
    return items


@manager.post("/api/milestones/{request_id}/{action}")
def milestone_action(
    request_id: str,
    action: Literal["fund", "deliver", "release", "reject"],
    body: MilestoneAction = MilestoneAction(),
) -> dict:
    try:
        entry = advance_milestone(request_id, action, reviewer=body.reviewer, reason=body.reason)
    except MilestoneNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (MilestoneError, EscrowError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return entry.model_dump()


class PaymentRecoveryAction(BaseModel):
    reviewer: str = Field("operator", max_length=100)
    # Only for retry-payment: resend a payment that is still "in flight" when
    # Circle has no record of it. It reuses the same idempotency key.
    resend_unconfirmed: bool = False


def _recovery_http_error(e: Exception) -> HTTPException:
    if isinstance(e, PaymentNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, PaymentLookupError):
        # Circle could not be asked: a gateway problem, and nothing was changed.
        return HTTPException(status_code=502, detail=str(e))
    return HTTPException(status_code=409, detail=str(e))  # wrong state / in flight


@manager.post("/api/requests/{request_id}/reconcile")
def reconcile_request_payment(request_id: str, body: PaymentRecoveryAction = PaymentRecoveryAction()) -> dict:
    """Ask Circle how an in-flight payment ended and record it. Never sends money."""
    try:
        result = reconcile_payment(request_id, reviewer=body.reviewer)
    except (PaymentNotFoundError, PaymentStateError, PaymentLookupError) as e:
        raise _recovery_http_error(e) from e
    return {
        "changed": result.changed,
        "observed": result.observed,
        "message": result.message,
        "entry": result.entry.model_dump(),
    }


@manager.post("/api/requests/{request_id}/retry-payment")
def retry_request_payment(request_id: str, body: PaymentRecoveryAction = PaymentRecoveryAction()) -> dict:
    """Pay again an approved request whose payment failed, was skipped or stayed unconfirmed."""
    try:
        entry = retry_payment(
            request_id, reviewer=body.reviewer, resend_unconfirmed=body.resend_unconfirmed
        )
    except (PaymentNotFoundError, PaymentStateError, PaymentLookupError, PaymentInFlightError) as e:
        raise _recovery_http_error(e) from e
    return entry.model_dump()


# --------------------------------------------------------------------------
# Requester routes (no manager token; allow-listed responses only)
# --------------------------------------------------------------------------


class RequesterSubmission(BaseModel):
    """What a requester may supply. Anything else is rejected, so they cannot
    pick their own request_id or set fields the server decides."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    category: RequestCategory
    amount_usdc: float = Field(gt=0, allow_inf_nan=False)
    requester: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    counterparty_wallet: str | None = Field(default=None, max_length=100)

    @field_validator("amount_usdc")
    @classmethod
    def _amount_must_be_payable(cls, v: float) -> float:
        """Refuse at the door what could never be paid, with a message the
        requester can act on (e.g. "more than 6 decimal places") instead of a
        failure after scoring."""
        try:
            parse_usdc(v)
        except InvalidAmountError as e:
            raise ValueError(str(e)) from e
        return v


@requester.get("/info")
def requester_info() -> dict:
    return {"access_code_required": access_code_required()}


@requester.post("/requests", dependencies=[Depends(require_requester_access)])
def submit_requester_request(body: RequesterSubmission) -> dict:
    # The id is generated here, never accepted from the client.
    request = PaymentRequest(request_id=new_request_id(), **body.model_dump())
    entry = run_pipeline(request, verbose=False)
    return to_public_status(entry).model_dump()


@requester.get("/requests/{request_id}")
def requester_request_status(request_id: str) -> dict:
    entry = None
    if request_id.startswith(REQUESTER_ID_PREFIX):
        entry = next(
            (e for e in read_latest_per_request() if e.request.request_id == request_id), None
        )
    if entry is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    return to_public_status(entry).model_dump()


# Registered last: include_router copies the routes defined so far.
app.include_router(manager)
app.include_router(requester)
