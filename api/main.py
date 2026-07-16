"""Phase 4 — FastAPI surface over the Phase 2/3 decision pipeline.

Endpoints trigger the agent loop, list/inspect decisions, and drive the atomic
approval gate. The trigger returns immediately and runs the loop in the
background so the caller never blocks on an LLM round-trip.

SECURITY NOTE — AUTH IS NOT YET IMPLEMENTED. Every endpoint takes `org_id` and
filters on it, but that org_id is currently TRUSTED FROM THE CALLER. There is
no authentication and no verification that the caller belongs to that org. A
later phase must replace the trusted org_id with one derived from an
authenticated principal. Until then, org_id is a scoping key, not a security
boundary.

That caveat is about the CALLER->API edge, and it is a different (weaker) claim
than the API->model edge. Whatever identity the API decides on, it BINDS into
the agent's tool gateway (run_qualification(role=..., org_id=...)); the model
running underneath cannot widen it. So a caller can currently lie about which
org it is — but the LLM still cannot escalate beyond what the caller was given.
Fixing auth tightens the first edge without touching the second.
"""

import asyncio

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from psycopg.types.json import Json
from pydantic import BaseModel

from agent import gate
from agent.cli import _gather_evidence
from db import execute, query

app = FastAPI(title="BRAINS decisions API", version="0.4.0")

# The privilege level the API binds into the agent's tool gateway. Least
# privilege: a background qualification run reads attacker-influenced lead text
# and has no business reading admin-only docs. When real auth lands, this
# becomes the authenticated principal's role — derived from a verified session,
# never from the request body, and never from the model.
CALLER_ROLE = "sales"


# --- Pydantic request/response models ----------------------------------------

class TriggerRequest(BaseModel):
    email: str
    org_id: int


class TriggerResponse(BaseModel):
    decision_id: int
    status: str


class DecideRequest(BaseModel):
    decided_by: str


class DecisionSummary(BaseModel):
    id: int
    lead_id: int | None
    proposed_action: str
    score: int | None
    band: str | None
    status: str
    created_at: str


class DecisionDetail(DecisionSummary):
    org_id: int
    trigger_input: dict
    rules_version: str | None
    reasoning: dict
    decided_by: str | None
    decided_at: str | None


# --- Background worker: run the loop, then update the row in place -----------

def _process_decision(decision_id: int, email: str, org_id: int) -> None:
    """Run the agent loop and fold the result into the pre-created row.

    Runs in a threadpool thread (FastAPI runs sync background tasks off the
    event loop), so `asyncio.run` here is safe and does not block the server.

    Identity is bound HERE, by the caller, and passed explicitly into the loop:
    the org_id this decision is scoped to (trusted from the request — see the
    module docstring) and CALLER_ROLE. The model in the loop cannot widen either.
    """
    from agent.loop import run_qualification  # lazy: pulls in the Anthropic SDK

    try:
        run = asyncio.run(
            run_qualification(email, role=CALLER_ROLE, org_id=org_id)
        )
    except Exception as e:  # noqa: BLE001 — record failure, never crash the worker
        execute(
            "UPDATE decisions SET status='error', reasoning=%s "
            "WHERE id=%s AND org_id=%s",
            (Json({"error": f"{type(e).__name__}: {e}"}), decision_id, org_id),
        )
        return

    facts = _gather_evidence(run)
    proposal = run["proposal"]
    action = proposal.get("proposed_action") or "nurture"
    g = gate.propose({
        "score": facts["score"],
        "proposed_action": action,
        "evidence": facts["evidence"],
    })
    reasoning = {
        "iterations": run["iterations"], "final": run["final"],
        "stop_reason": run["stop_reason"], "tool_calls": run["tool_calls"],
        "halted": run["halted"], "model": run["model"],
        # Audit: the privilege the tools actually ran at. Recorded from the run,
        # not re-derived, so the trace answers "at what role?" on its own.
        "role": run["role"], "org_id": run["org_id"],
        "evidence": facts["evidence"], "gate": g,
    }
    execute(
        "UPDATE decisions SET lead_id=%s, proposed_action=%s, score=%s, "
        "band=%s, rules_version=%s, reasoning=%s, status=%s "
        "WHERE id=%s AND org_id=%s",
        (facts["lead_id"], action, facts["score"], facts["band"],
         facts["rules_version"], Json(reasoning), g["status"],
         decision_id, org_id),
    )


# --- Endpoints ---------------------------------------------------------------

@app.post("/decisions/trigger", response_model=TriggerResponse, status_code=202)
def trigger(req: TriggerRequest, background_tasks: BackgroundTasks) -> TriggerResponse:
    """Create a decision in 'processing' and run the loop in the background.

    Returns immediately with the decision id; the caller polls GET
    /decisions/{id} until the status leaves 'processing'.
    """
    rows = execute(
        "INSERT INTO decisions "
        "(org_id, trigger_input, proposed_action, reasoning, status) "
        "VALUES (%s, %s, %s, %s, 'processing') RETURNING id",
        (req.org_id, Json({"email": req.email, "org_id": req.org_id}),
         "(processing)", Json({"phase": "processing"})),
    )
    decision_id = rows[0]["id"]
    background_tasks.add_task(_process_decision, decision_id, req.email, req.org_id)
    return TriggerResponse(decision_id=decision_id, status="processing")


@app.get("/decisions/pending", response_model=list[DecisionSummary])
def pending(org_id: int = Query(...)) -> list[DecisionSummary]:
    rows = query(
        "SELECT id, lead_id, proposed_action, score, band, status, created_at "
        "FROM decisions WHERE org_id = %s AND status = 'pending_approval' "
        "ORDER BY id",
        (org_id,),
    )
    return [_summary(r) for r in rows]


@app.get("/decisions/{decision_id}", response_model=DecisionDetail)
def get_decision(decision_id: int, org_id: int = Query(...)) -> DecisionDetail:
    rows = query(
        "SELECT * FROM decisions WHERE id = %s AND org_id = %s",
        (decision_id, org_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="decision not found in this org")
    r = rows[0]
    return DecisionDetail(
        **_summary(r).model_dump(),
        org_id=r["org_id"],
        trigger_input=r["trigger_input"],
        rules_version=r["rules_version"],
        reasoning=r["reasoning"],
        decided_by=r["decided_by"],
        decided_at=r["decided_at"].isoformat() if r["decided_at"] else None,
    )


@app.post("/decisions/{decision_id}/approve", response_model=DecisionSummary)
def approve(decision_id: int, req: DecideRequest, org_id: int = Query(...)) -> DecisionSummary:
    return _decide(gate.approve(decision_id, req.decided_by, org_id), decision_id, org_id)


@app.post("/decisions/{decision_id}/reject", response_model=DecisionSummary)
def reject(decision_id: int, req: DecideRequest, org_id: int = Query(...)) -> DecisionSummary:
    return _decide(gate.reject(decision_id, req.decided_by, org_id), decision_id, org_id)


# --- Helpers -----------------------------------------------------------------

def _summary(r: dict) -> DecisionSummary:
    return DecisionSummary(
        id=r["id"], lead_id=r["lead_id"], proposed_action=r["proposed_action"],
        score=r["score"], band=r["band"], status=r["status"],
        created_at=r["created_at"].isoformat(),
    )


def _decide(result: dict, decision_id: int, org_id: int) -> DecisionSummary:
    """Map the gate's atomic-transition result to HTTP. 409 if not pending."""
    if not result.get("ok"):
        # Not pending: already decided, wrong org, or missing -> conflict.
        raise HTTPException(status_code=409, detail=result["error"])
    rows = query(
        "SELECT id, lead_id, proposed_action, score, band, status, created_at "
        "FROM decisions WHERE id = %s AND org_id = %s",
        (decision_id, org_id),
    )
    return _summary(rows[0])
