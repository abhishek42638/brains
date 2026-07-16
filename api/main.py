"""Phase 5 — authenticated FastAPI surface over the Phase 2/3 decision pipeline.

Endpoints trigger the agent loop, list/inspect decisions, drive the atomic
approval gate, and resolve broken runs. The trigger returns immediately and runs
the loop in the background so the caller never blocks on an LLM round-trip.

IDENTITY COMES FROM THE CREDENTIAL. Every endpoint depends on `require_principal`,
which resolves the X-API-Key header to (org_id, role). There is no unauthenticated
route, and — the part that matters — `org_id` appears in NO request body and NO
query parameter. It is not that a caller-supplied org_id is validated; it is that
there is nowhere to supply one. The earlier "org_id is trusted from the caller"
caveat is gone because the thing it described is gone.

The full chain, closed:

    X-API-Key -> sha256 -> api_keys.(org_id, role)   <- auth.py: the only source
              -> build_mcp(role=, org_id=)           <- server.py: closed over
              -> WHERE org_id / permitted_roles      <- the DB enforces it

Each hop takes identity further out of reach: the model cannot name a role
(no tool parameter), and now the caller cannot name an org (no request field).
A key with role=sales gets a sales-bound agent and cannot read admin docs; a key
for org 2 cannot see org 1's decisions. Both are enforced in SQL, not here.

Rate limiting is per credential — see auth.TRIGGER_RATE_LIMIT_PER_HOUR.
"""

import logging

import config  # noqa: F401  — loads .env before anything reads os.environ
import decisions
import tasks
from auth import (
    Principal,
    RateLimitExceeded,
    charge_trigger,
    require_cloud_task,
    require_principal,
    require_scheduler,
)
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from agent import gate
from db import query

# Fail closed at BOOT, before the app object exists and before anything can take
# traffic. A deployed service that is configured to emulate — or that is missing
# the config it needs to verify a Cloud Tasks token — must not serve at all: the
# internals spend Anthropic credits and are internet-reachable (Cloud Run's IAM
# check is per-service, and the public API needs it off). A crash here means
# Cloud Run keeps the previous, working revision serving. Silence would mean an
# open door that looks healthy.
tasks.assert_sane_config()

app = FastAPI(title="BRAINS decisions API", version="0.5.0")

logger = logging.getLogger("brains-api")


# --- Pydantic request/response models ----------------------------------------

class StrictRequest(BaseModel):
    """Request body that REJECTS unknown fields instead of ignoring them.

    Pydantic's default is to drop extras silently, so a caller POSTing
    {"role": "admin"} would get a cheerful 202 and no role change. That is safe
    but mute, and mute is how a `role` field gets added back later by someone
    who tried it, saw no error, and assumed it was wired up. Forbidding extras
    turns any attempt to supply identity in a request body into a loud 422.
    """

    model_config = ConfigDict(extra="forbid")


class TriggerRequest(StrictRequest):
    # NO `role` AND NO `org_id`. Both come from the credential, and there is no
    # field here to say otherwise — StrictRequest turns an attempt into a 422
    # rather than ignoring it, so "org_id in the body is ignored if present"
    # is enforced as "refused if present", which is the same guarantee said
    # louder. What the caller supplies is intent (which lead); who they are is
    # not theirs to assert.
    email: str


class TriggerResponse(BaseModel):
    decision_id: int
    status: str


class DecideRequest(StrictRequest):
    decided_by: str


class DismissRequest(StrictRequest):
    decided_by: str
    note: str


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


# --- The internal worker endpoint --------------------------------------------

class ProcessRequest(StrictRequest):
    """The task payload. Not a public shape — see `internal_process`."""

    decision_id: int
    email: str
    org_id: int
    role: str


@app.post("/internal/process", status_code=200)
def internal_process(
    req: ProcessRequest,
    claims: dict = Depends(require_cloud_task),
    x_cloudtasks_taskretrycount: str | None = Header(default=None),
) -> dict:
    """Run the agent loop for one decision. Cloud Tasks only.

    NOT public and NOT API-key'd: `require_cloud_task` accepts only an OIDC
    token signed by the dedicated tasks service account, with our URL as the
    audience. A customer credential must not be able to reach the internals, and
    neither must anyone else.

    Note this endpoint takes org_id and role from the TASK payload rather than
    from a credential. That is not the caller asserting identity: the task was
    minted by `trigger`, which took both from the authenticated principal, and
    the queue is only writable by our own service account. The identity was
    fixed before the task existed; this is transport, not assertion.

    RETRY EXHAUSTION. decisions.process() never raises and always moves the row
    out of 'processing', so a failed LOOP already lands in needs_review without
    any help from Cloud Tasks. What retries are for is failure of this handler
    itself — the DB being unreachable, the instance being evicted mid-run. If
    that keeps happening we must not simply stop trying and leave the row in
    'processing' forever, so on the LAST attempt we park it in needs_review and
    return 200 (a 500 would just burn another retry to reach the same place).

    sweep_stuck remains the backstop for the case no handler can cover: the
    instance dying so hard that neither branch below ever runs.
    """
    attempt = int(x_cloudtasks_taskretrycount or 0)
    is_final_attempt = attempt >= tasks.TASKS_MAX_ATTEMPTS - 1

    try:
        result = decisions.process(
            req.decision_id, req.email, org_id=req.org_id, role=req.role,
        )
        return {"decision_id": req.decision_id, "status": result.get("status"),
                "attempt": attempt}
    except BaseException as e:  # noqa: BLE001 — the row must not be abandoned
        if not is_final_attempt:
            # Let Cloud Tasks retry with backoff. 5xx is how you ask for that.
            raise HTTPException(
                status_code=500,
                detail=f"processing failed (attempt {attempt + 1} of "
                       f"{tasks.TASKS_MAX_ATTEMPTS}); will retry",
            ) from e
        # Out of retries: park it where a human will see it rather than let the
        # row sit in 'processing' with nothing left to move it.
        from agent.loop import identity_of

        decisions.needs_review(
            req.decision_id, org_id=req.org_id,
            identity=identity_of(req.role, req.org_id),
            error=f"task retries exhausted after {tasks.TASKS_MAX_ATTEMPTS} "
                  f"attempts; last error: {type(e).__name__}: {e}",
        )
        return {"decision_id": req.decision_id, "status": decisions.STATUS_NEEDS_REVIEW,
                "attempt": attempt, "retries_exhausted": True}


@app.post("/internal/sweep", status_code=200)
def internal_sweep(claims: dict = Depends(require_scheduler)) -> dict:
    """Rescue decisions abandoned in 'processing'. Cloud Scheduler only.

    The last line of defence. /internal/process files a needs_review row when
    the loop fails and even when Cloud Tasks runs out of retries — but none of
    that code runs if the instance is SIGKILLed, OOM-killed or evicted
    mid-request. Nothing inside a process can handle its own death, so the check
    has to come from outside it, on a clock.

    Sweeps ALL orgs: this is an operator function, not a tenant-scoped one, and
    a row abandoned in org 2 is just as invisible as one in org 1.
    """
    swept = decisions.sweep_stuck()
    if swept:
        logger.warning("sweep rescued %d abandoned decision(s): %s",
                       len(swept), [r["id"] for r in swept])
    return {"swept": len(swept), "decision_ids": [r["id"] for r in swept]}


# --- Endpoints ---------------------------------------------------------------

@app.post("/decisions/trigger", response_model=TriggerResponse, status_code=202)
def trigger(
    req: TriggerRequest,
    principal: Principal = Depends(require_principal),
) -> TriggerResponse:
    """Create a decision in 'processing' and run the loop in the background.

    Returns immediately with the decision id; the caller polls GET
    /decisions/{id} until the status leaves 'processing'.

    Identity is the credential's, full stop: `principal.org_id` scopes the row
    and `principal.role` binds the agent's tool gateway. A sales key gets a
    sales-bound agent that cannot read admin docs; an admin key gets an
    admin-bound one. Same build_mcp binding as always — only the source changed,
    from a hardcoded constant to the authenticated caller.

    The loop is DISPATCHED, not run here: this returns in milliseconds and a
    Cloud Task delivers the work to /internal/process, which gets its own CPU
    allocation and its own timeout. See tasks.py for why a BackgroundTask is
    not an option on Cloud Run.

    Charged against the key's hourly budget BEFORE any work starts: the point of
    the limit is to not spend Anthropic credits, so it has to come first.
    """
    from agent.loop import identity_of  # lazy: keep the SDK import off this path

    try:
        used = charge_trigger(principal.api_key_id)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: {e.count - 1}/{e.limit} triggers used "
                   f"this hour for this API key",
            headers={"Retry-After": "3600"},
        ) from e

    # Stamp the binding on the row at birth, so even a row whose background
    # worker never runs still records the privilege it was created under.
    identity = identity_of(principal.role, principal.org_id)
    decision_id = decisions.create_processing(
        org_id=principal.org_id,
        trigger_input={
            "email": req.email,
            "source": "api",
            # Audit: which credential asked for this. The key id, never the key.
            "api_key_id": principal.api_key_id,
            "triggers_used_this_hour": used,
        },
        identity=identity,
    )
    # The row exists before the task does, so a failed enqueue would strand it in
    # 'processing' with nothing that will ever move it: no task means no retry,
    # no /internal/process, no needs_review. The sweeper would eventually rescue
    # it, but only after STUCK_AFTER_MINUTES — 15 minutes of a row that is
    # already known-dead sitting where no human can see it.
    #
    # Observed for real: a missing iam.serviceAccounts.actAs grant made every
    # enqueue raise PermissionDenied, and the 500 left exactly such an orphan.
    # The row is created first ON PURPOSE (an id must exist to return), so the
    # fix is to close it here rather than to reorder.
    try:
        tasks.enqueue_process(
            decision_id, req.email, org_id=principal.org_id, role=principal.role,
        )
    except Exception as e:  # noqa: BLE001 — never leave the row orphaned
        decisions.needs_review(
            decision_id, org_id=principal.org_id, identity=identity,
            error=f"could not enqueue the processing task: {type(e).__name__}: {e}",
        )
        logger.exception("enqueue failed for decision %s; parked in %s",
                         decision_id, decisions.STATUS_NEEDS_REVIEW)
        raise HTTPException(
            status_code=503,
            detail=f"could not enqueue processing for decision {decision_id}; "
                   f"it has been parked for review rather than lost",
        ) from e

    return TriggerResponse(decision_id=decision_id, status=decisions.STATUS_PROCESSING)


@app.get("/decisions/pending", response_model=list[DecisionSummary])
def pending(
    principal: Principal = Depends(require_principal),
) -> list[DecisionSummary]:
    """Everything awaiting a human: proposals to approve AND broken runs.

    'needs_review' is served here rather than from an endpoint of its own. A
    separate route would only reach someone who knew to poll it, and a failed
    run parked where nobody looks is the same silent failure with a new status —
    which is the thing this system exists to prevent. This queue is where humans
    already look, so it is where work for humans goes. `status` distinguishes
    the two: 'pending_approval' means "the gate wants you to approve a
    proposal", 'needs_review' means "this run produced no proposal worth
    trusting". A UI can style them differently; neither can hide.

    Scoped to the credential's org — there is no parameter to widen it.
    Uses the (org_id, status) index via = ANY(...).
    """
    rows = query(
        "SELECT id, lead_id, proposed_action, score, band, status, created_at "
        "FROM decisions WHERE org_id = %s AND status = ANY(%s) "
        "ORDER BY id",
        (principal.org_id, list(decisions.HUMAN_QUEUE_STATUSES)),
    )
    return [_summary(r) for r in rows]


@app.get("/decisions/{decision_id}", response_model=DecisionDetail)
def get_decision(
    decision_id: int, principal: Principal = Depends(require_principal),
) -> DecisionDetail:
    """One decision, from the credential's org only.

    A decision in another org is a 404, not a 403: "exists but not yours" and
    "does not exist" are the same fact to a caller who may not know it exists,
    and 403 would confirm the id is real.
    """
    rows = query(
        "SELECT * FROM decisions WHERE id = %s AND org_id = %s",
        (decision_id, principal.org_id),
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
def approve(
    decision_id: int, req: DecideRequest,
    principal: Principal = Depends(require_principal),
) -> DecisionSummary:
    return _decide(gate.approve(decision_id, req.decided_by, principal.org_id),
                   decision_id, principal.org_id)


@app.post("/decisions/{decision_id}/reject", response_model=DecisionSummary)
def reject(
    decision_id: int, req: DecideRequest,
    principal: Principal = Depends(require_principal),
) -> DecisionSummary:
    return _decide(gate.reject(decision_id, req.decided_by, principal.org_id),
                   decision_id, principal.org_id)


# --- Resolving a broken run --------------------------------------------------
#
# needs_review used to be a dead end: those rows 409 on approve/reject (there is
# no proposal to approve), so the queue only ever grew. A queue that cannot be
# emptied stops being read, and a queue nobody reads is the silent failure this
# system exists to prevent — arrived at by paperwork instead of by a crash.
# Two exits, both atomic, both only from needs_review:
#   retry   — run it again on the original trigger_input (the usual case: the
#             failure was transient, a 429 or a blip).
#   dismiss — a human says this one is not worth rerunning, with a note saying
#             why. Terminal, and auditable.

@app.post("/decisions/{decision_id}/retry", response_model=TriggerResponse,
          status_code=202)
def retry(
    decision_id: int,
    principal: Principal = Depends(require_principal),
) -> TriggerResponse:
    """Re-run a needs_review decision on its ORIGINAL trigger_input.

    Re-uses the same row rather than creating a new one: the decision is the
    same decision, and a retry that forked a fresh id would leave the failed
    attempt orphaned in the queue and hide that this took two goes. The previous
    attempt is preserved under reasoning.attempts.

    Charged against the hourly budget like any trigger — a retry costs exactly
    what a trigger costs, so it cannot be a way around the ceiling.
    """
    try:
        charge_trigger(principal.api_key_id)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded: {e.count - 1}/{e.limit} triggers used "
                   f"this hour for this API key",
            headers={"Retry-After": "3600"},
        ) from e

    result = decisions.begin_retry(
        decision_id, org_id=principal.org_id, role=principal.role,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result["error"])

    # Same orphan risk as `trigger`, and worse here: begin_retry has already
    # moved this row OUT of needs_review and back into 'processing'. A failed
    # enqueue would take a row a human could see and hide it — the retry would
    # have made things strictly worse than not retrying.
    from agent.loop import identity_of

    try:
        tasks.enqueue_process(
            decision_id, result["email"], org_id=principal.org_id,
            role=principal.role,
        )
    except Exception as e:  # noqa: BLE001 — put it back where a human can see it
        decisions.needs_review(
            decision_id, org_id=principal.org_id,
            identity=identity_of(principal.role, principal.org_id),
            error=f"retry could not enqueue its task: {type(e).__name__}: {e}",
        )
        logger.exception("retry enqueue failed for decision %s; back to %s",
                         decision_id, decisions.STATUS_NEEDS_REVIEW)
        raise HTTPException(
            status_code=503,
            detail=f"could not enqueue the retry for decision {decision_id}; "
                   f"it remains parked for review",
        ) from e

    return TriggerResponse(decision_id=decision_id,
                           status=decisions.STATUS_PROCESSING)


@app.post("/decisions/{decision_id}/dismiss", response_model=DecisionSummary)
def dismiss(
    decision_id: int, req: DismissRequest,
    principal: Principal = Depends(require_principal),
) -> DecisionSummary:
    """Close a needs_review decision a human has judged not worth rerunning."""
    result = decisions.dismiss(
        decision_id, org_id=principal.org_id, decided_by=req.decided_by,
        note=req.note,
    )
    return _decide(result, decision_id, principal.org_id)


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
