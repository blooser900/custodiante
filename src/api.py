"""HTTP API for the dashboard (ui/dashboard.html).

Run with:
    uvicorn src.api:app --reload --port 8000

Then open http://127.0.0.1:8000/
"""

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.audit.store import read_latest_per_request
from src.audit.verify import verify_chain
from src.config import RulesConfig, load_config, save_config
from src.payments.escrow import ChainReader, EscrowError
from src.pipeline import (
    AlreadyReviewedError,
    DuplicateRequestError,
    MilestoneError,
    MilestoneNotFoundError,
    advance_milestone,
    apply_human_override,
    provider_is_ours,
    run_pipeline,
)
from src.requests.schema import PaymentRequest

app = FastAPI(title="Tameion Payment Agent")

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Without this, any failure inside the decision engine or Arc client
    # (a missing API key, a network error, Jev/Kev being unreachable) shows
    # up to the dashboard as an opaque "Internal Server Error" with no
    # detail, since FastAPI's default handler doesn't expose exception
    # messages. The dashboard's error handling expects a `detail` field.
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
def dashboard() -> FileResponse:
    return FileResponse(UI_DIR / "dashboard.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse(UI_DIR / "settings.html")


@app.get("/api/config")
def get_config() -> dict:
    return load_config().model_dump()


@app.put("/api/config")
def update_config(config: RulesConfig) -> dict:
    # RulesConfig's own validators (threshold ranges, 2-10 rubric levels)
    # already ran by the time FastAPI builds this parameter -- an invalid
    # body never reaches here, it gets a 422 automatically.
    save_config(config)
    return config.model_dump()


@app.get("/api/stats")
def stats() -> dict:
    entries = read_latest_per_request()
    pending = [e for e in entries if e.decision == "escalate" and e.human_review is None]
    paid_total = sum(
        (e.reduced_amount or e.request.amount_usdc) for e in entries if e.payment_executed
    )
    return {
        "total_requests": len(entries),
        "pending_count": len(pending),
        "paid_total_usdc": round(paid_total, 2),
        "payments_executed": sum(1 for e in entries if e.payment_executed),
        # Approved requests whose on-chain payment was attempted and failed:
        # these need a human to look at them.
        "payments_failed": sum(1 for e in entries if e.payment_status == "failed"),
        # Milestone escrow: USDC currently locked in contracts (funded, not
        # yet released or refunded), and how many milestones are in flight.
        "escrowed_total_usdc": round(
            sum(
                (e.reduced_amount or e.request.amount_usdc)
                for e in entries
                if e.payment_status == "escrowed"
            ),
            2,
        ),
        "milestones_active": sum(1 for e in entries if e.escrow_status in ACTIVE_ESCROW_STATUSES),
    }


@app.get("/api/audit/verify")
def verify_audit_log() -> dict:
    """Check the audit log's hash chain; `ok` is False if it was tampered with."""
    return asdict(verify_chain())


@app.get("/api/requests/latest")
def latest_requests() -> list[dict]:
    return [e.model_dump() for e in read_latest_per_request()]


@app.get("/api/escalations/pending")
def pending_escalations() -> list[dict]:
    entries = read_latest_per_request()
    pending = [e for e in entries if e.decision == "escalate" and e.human_review is None]
    return [e.model_dump() for e in pending]


@app.post("/api/requests")
def submit_request(request: PaymentRequest) -> dict:
    try:
        entry = run_pipeline(request, verbose=False)
    except DuplicateRequestError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return entry.model_dump()


@app.post("/api/escalations/{request_id}/decision")
def decide_escalation(request_id: str, decision: EscalationDecision) -> dict:
    entries = read_latest_per_request()
    original = next((e for e in entries if e.request.request_id == request_id), None)

    if original is None:
        raise HTTPException(status_code=404, detail=f"No request found with id {request_id!r}.")
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
    except AlreadyReviewedError as e:
        # Lost a race with another reviewer between the check above and the
        # lock inside apply_human_override.
        raise HTTPException(status_code=409, detail=str(e)) from e
    return entry.model_dump()


def _chain_reader() -> ChainReader:
    """Read-only chain access; a function so tests can substitute it."""
    return ChainReader()


@app.get("/api/milestones/active")
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


@app.post("/api/milestones/{request_id}/{action}")
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
