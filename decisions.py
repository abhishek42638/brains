"""The one write path for the `decisions` table.

Every decision row — CLI or API — is created and completed here, through one
lifecycle:

    create_processing()  ->  'processing'
                             |
              process()  ->  'auto_executed' | 'auto_discarded'
                             | 'pending_approval'   (the gate decided)
                             | 'needs_review'       (the run did not produce a
                                                     decision we can stand behind)

Before this module the two callers had two lifecycles against the same table:
the CLI wrote a single INSERT already carrying its final status, while the API
inserted 'processing' and updated later. Same rows, two shapes, and every
change had to be made twice — including the evidence extraction, the gate call
and the reasoning blob, which were duplicated almost verbatim. `process()` is
now the only place any of that happens, so the CLI and the API cannot disagree
about what a decision record means.

NOTHING IS LEFT IN 'processing'
-------------------------------
'processing' is invisible to the human queue by design — it means "a machine is
still working on it". So a row that reaches a terminal state only via the happy
path is a row that vanishes when the worker dies: not pending, not decided, not
surfaced anywhere, and nobody is waiting for it because the caller already got
its 202. That is precisely the failure BRAINS exists to catch (master doc §2:
automations that break silently), reproduced inside BRAINS.

`process()` therefore never raises and never returns without moving the row out
of 'processing'. Any failure lands in 'needs_review' with the error and whatever
partial trace survived, which puts it in front of a human — a wrong decision a
person can see beats a correct one that disappeared.

Two failures get the same treatment, because they are the same thing to a human:
  - the loop RAISED (API error, network, restart mid-run);
  - the loop RETURNED but produced no usable proposal (unparseable JSON, an
    action outside ALLOWED_ACTIONS, or it hit an iteration/tool cap and never
    concluded). This used to be papered over with `proposal.get(...) or
    "nurture"` in both callers — a default that invents a proposal the model
    never made and files it as a real one. A fabricated 'nurture' is not a
    safe fallback; it is a silent break with a plausible face.

The approve/reject transitions still live in agent/gate.py: those are the
human's decision on a finished record, guarded by their own atomic UPDATE, and
are a different concern from producing the record in the first place.
"""

import logging

from psycopg.types.json import Json

from agent import gate
from db import execute

logger = logging.getLogger("brains-decisions")
if not logger.handlers:
    _h = logging.StreamHandler()  # stderr
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

STATUS_PROCESSING = "processing"
STATUS_NEEDS_REVIEW = "needs_review"

# The statuses that mean "a human must look at this".
#
# needs_review is served from GET /decisions/pending alongside pending_approval
# rather than from an endpoint of its own. A separate endpoint only helps
# someone who knows to poll it, and a broken run parked where nobody is looking
# is the same silent failure wearing a different status. The queue is where
# humans already look, so that is where things needing humans go; `status` is on
# every row, so a UI can still tell "approve this proposal" apart from "this run
# broke" and treat them differently.
HUMAN_QUEUE_STATUSES = ("pending_approval", STATUS_NEEDS_REVIEW)

# proposed_action is NOT NULL, but a row in 'processing' has no proposal yet and
# a failed one never will. This placeholder says "no proposal exists" instead of
# naming an action nobody chose.
NO_ACTION = "(none)"


def _emit(status: str, *, decision_id: int, org_id: int) -> None:
    """Announce a completed transition to the org's webhook endpoints.

    Called ONLY after an UPDATE that returned a row, so an event always
    corresponds to a transition that really happened.

    Belt AND braces on failure: `webhooks.emit` already swallows everything, and
    this wraps it again. The second layer is not redundancy for its own sake —
    it also covers the import itself failing, and it states the invariant at the
    call site, where the next person editing this module will read it: the
    decision is committed by the time we get here, and NOTHING about notifying
    an integration may change that outcome or fail the caller.
    """
    try:
        import webhooks

        webhooks.emit_for_status(status, decision_id=decision_id, org_id=org_id)
    except Exception:  # noqa: BLE001 — a decision must not fail over a webhook
        logger.exception("webhook emission failed for decision %s (%s); the "
                         "decision itself is unaffected", decision_id, status)


def create_processing(*, org_id: int, trigger_input: dict, identity: dict) -> int:
    """Create a decision in 'processing'. Returns its id.

    The identity is stamped at birth so that even a row whose worker never runs
    records the privilege it was created under.
    """
    rows = execute(
        "INSERT INTO decisions "
        "(org_id, trigger_input, proposed_action, reasoning, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (org_id, Json(trigger_input), NO_ACTION,
         Json({"phase": STATUS_PROCESSING, "identity": identity}),
         STATUS_PROCESSING),
    )
    return rows[0]["id"]


# How long a row may sit in 'processing' before we call it abandoned. Generous:
# a real run is seconds, but a slow model turn plus retries can stretch, and
# rescuing a row that is still being worked on would be its own kind of wrong.
STUCK_AFTER_MINUTES = 15


def sweep_stuck(*, org_id: int | None = None,
                older_than_minutes: int = STUCK_AFTER_MINUTES) -> list[dict]:
    """Rescue rows abandoned in 'processing'. Returns the rows it moved.

    process()'s try/except covers a run that FAILS. It cannot cover a process
    that never comes back: SIGKILL, an OOM kill, a container eviction or a power
    loss run no handler at all, and the row is left mid-flight with nobody
    holding it. That is the same silent disappearance, arrived at from the one
    direction Python cannot intercept, so it needs a check from outside the run.

    This is deliberately time-based and idempotent, so it is safe to call from a
    scheduler or at API startup. NOT wired to either yet — see the note in
    README/handoff; today it is reachable via `python -m agent.cli sweep`.
    """
    rows = execute(
        "UPDATE decisions SET status = %s, reasoning = reasoning || %s::jsonb "
        "WHERE status = %s "
        "  AND created_at < now() - make_interval(mins => %s) "
        "  AND (%s::int IS NULL OR org_id = %s::int) "
        "RETURNING id, org_id, created_at",
        (STATUS_NEEDS_REVIEW,
         Json({"error": f"abandoned in '{STATUS_PROCESSING}' for over "
                        f"{older_than_minutes}m — the worker never finished "
                        f"(process killed, restarted, or evicted)",
               "phase": STATUS_NEEDS_REVIEW,
               "swept": True}),
         STATUS_PROCESSING, older_than_minutes, org_id, org_id),
    )
    rows = rows if isinstance(rows, list) else []
    for r in rows:
        logger.warning("swept stuck decision %s (created %s) -> %s",
                       r["id"], r["created_at"], STATUS_NEEDS_REVIEW)
    return rows


STATUS_DISMISSED = "dismissed"


def begin_retry(decision_id: int, *, org_id: int, role: str) -> dict:
    """Move a needs_review row back to 'processing' for another attempt.

    Atomic: the status guard is in the WHERE, so two concurrent retries cannot
    both start one (the second sees rowcount 0 and gets a 409). Without that,
    two workers would run the same lead and race to write the result.

    The FAILED attempt is not discarded — it is appended to reasoning.attempts
    before the row is reset. A retry that erased the thing it was retrying would
    destroy the evidence of an intermittent failure, which is exactly the
    evidence worth keeping: three attempts recorded is a story, one success is
    not.

    Returns {"ok": True, "email": <the original trigger's email>} so the caller
    re-runs on the ORIGINAL input rather than anything a client re-supplied —
    the retry re-executes the same decision, it does not accept a new one.
    """
    from agent.loop import identity_of  # lazy: Anthropic SDK

    rows = execute(
        "UPDATE decisions SET "
        "  status = %s, "
        "  proposed_action = %s, "
        # Archive this attempt, then clear the working fields. jsonb_build_array
        # || coalesce(...) appends to any attempts already there, so attempt 3
        # does not lose attempts 1 and 2.
        # ::text on the phase literal: jsonb_build_object gives Postgres no
        # context to infer a bare parameter's type ("could not determine data
        # type of parameter"), so every scalar going into it says what it is.
        "  reasoning = jsonb_build_object("
        "      'phase', %s::text, "
        "      'identity', %s::jsonb, "
        "      'attempts', coalesce(reasoning->'attempts', '[]'::jsonb) "
        "                  || jsonb_build_array(reasoning - 'attempts')"
        "  ) "
        "WHERE id = %s AND org_id = %s AND status = %s "
        "RETURNING id, trigger_input",
        (STATUS_PROCESSING, NO_ACTION, STATUS_PROCESSING,
         Json(identity_of(role, org_id)), decision_id, org_id,
         STATUS_NEEDS_REVIEW),
    )
    if not rows:
        return {
            "ok": False,
            "error": (
                f"decision {decision_id} is not {STATUS_NEEDS_REVIEW} "
                "(already resolved, still running, or does not exist)"
            ),
        }

    trigger_input = rows[0]["trigger_input"] or {}
    email = trigger_input.get("email")
    if not email:
        # The row cannot be re-run without its original input. Put it straight
        # back rather than leaving it in 'processing' with nothing to process.
        needs_review(
            decision_id, org_id=org_id, identity=identity_of(role, org_id),
            error="cannot retry: trigger_input has no email",
        )
        return {"ok": False, "error": "trigger_input has no email to retry"}

    logger.info("decision %s -> %s (retry)", decision_id, STATUS_PROCESSING)
    return {"ok": True, "email": email}


def dismiss(decision_id: int, *, org_id: int, decided_by: str, note: str) -> dict:
    """Close a needs_review decision that a human judges not worth rerunning.

    Terminal and atomic, guarded on status like the gate's transitions, so it
    cannot dismiss a row that is mid-retry or already resolved. The note is
    required by the request model: "why did a human close this?" is the whole
    value of the record.
    """
    rows = execute(
        "UPDATE decisions SET status = %s, decided_by = %s, decided_at = now(), "
        "  reasoning = reasoning || jsonb_build_object("
        "      'dismissal', jsonb_build_object('note', %s::text, 'by', %s::text)) "
        "WHERE id = %s AND org_id = %s AND status = %s "
        "RETURNING id",
        (STATUS_DISMISSED, decided_by, note, decided_by, decision_id, org_id,
         STATUS_NEEDS_REVIEW),
    )
    if not rows:
        return {
            "ok": False,
            "error": (
                f"decision {decision_id} is not {STATUS_NEEDS_REVIEW} "
                "(already resolved, still running, or does not exist)"
            ),
        }
    logger.info("decision %s -> %s by %s", decision_id, STATUS_DISMISSED, decided_by)
    return {"ok": True, "id": decision_id, "status": STATUS_DISMISSED}


def _last_result(run: dict, tool_name: str):
    """Most recent result payload for a given tool in the trace, or None."""
    for it in reversed(run.get("iterations") or []):
        if it.get("tool") == tool_name and isinstance(it.get("result"), dict):
            return it["result"]
    return None


def gather_evidence(run: dict) -> dict:
    """Reduce the loop's raw trace to the deterministic facts the gate needs.

    The gate never sees the model's opinion — only these facts, pulled from the
    actual tool results the loop captured.
    """
    lookup = _last_result(run, "lookup_lead") or {}
    crm = _last_result(run, "check_crm") or {}
    score_res = _last_result(run, "score_lead") or {}

    lead = lookup.get("lead") or {}
    company = lookup.get("company")  # None if the lead has no company on file

    lost_deals = sum(1 for d in crm.get("deals", []) if d.get("stage") == "lost")
    open_tickets = crm.get("open_tickets")
    if open_tickets is None:
        open_tickets = sum(
            1 for t in crm.get("tickets", []) if t.get("status") == "open"
        )

    return {
        "lead_id": lead.get("id"),
        "score": score_res.get("score"),
        "band": score_res.get("band"),
        "rules_version": score_res.get("rules_version"),
        "evidence": {
            "has_company": company is not None,
            "lost_deals": lost_deals,
            "open_tickets": open_tickets,
        },
    }


def _proposal_problem(run: dict) -> str | None:
    """Why this run's proposal cannot be acted on, or None if it is fine."""
    from agent.loop import ALLOWED_ACTIONS  # local: keeps the SDK import lazy

    proposal = run.get("proposal") or {}
    if proposal.get("parse_error"):
        return f"unusable proposal: {proposal['parse_error']}"
    action = proposal.get("proposed_action")
    if action not in ALLOWED_ACTIONS:
        return f"proposed_action {action!r} is not one of {ALLOWED_ACTIONS}"
    if run.get("halted"):
        return f"loop halted early ({run['halted']}) without concluding"
    return None


def needs_review(
    decision_id: int, *, org_id: int, identity: dict, error: str,
    partial_trace: list | None = None, run: dict | None = None,
) -> dict:
    """Move a row out of 'processing' and into a human's queue. Never raises.

    Records the error and whatever trace survived, so the reviewer can see how
    far the run got rather than just that it failed.
    """
    reasoning = {
        "error": error,
        "identity": identity,
        "iterations": (run or {}).get("iterations") or partial_trace or [],
        "phase": STATUS_NEEDS_REVIEW,
    }
    if run is not None:
        reasoning.update({
            "final": run.get("final"),
            "stop_reason": run.get("stop_reason"),
            "tool_calls": run.get("tool_calls"),
            "halted": run.get("halted"),
            "model": run.get("model"),
        })

    rows = execute(
        # Carry `attempts` across rather than overwriting reasoning wholesale.
        # This row may be on its second or third go (begin_retry archives each
        # failure there), and a straight replace would drop the earlier ones —
        # so a lead that failed three times would look like it failed once, and
        # the intermittency, which is the whole diagnosis, would be invisible.
        "UPDATE decisions SET status=%s, proposed_action=%s, "
        "  reasoning = %s::jsonb || (CASE "
        "      WHEN reasoning ? 'attempts' "
        "      THEN jsonb_build_object('attempts', reasoning->'attempts') "
        "      ELSE '{}'::jsonb END) "
        # RETURNING so we can tell whether this UPDATE actually matched. Without
        # it the statement reports a rowcount nobody reads, and the webhook
        # emission below would fire for a row in another org, or for no row at
        # all — announcing a transition that never happened.
        "WHERE id=%s AND org_id=%s RETURNING id",
        (STATUS_NEEDS_REVIEW, NO_ACTION, Json(reasoning), decision_id, org_id),
    )
    logger.warning("decision %s -> %s: %s", decision_id, STATUS_NEEDS_REVIEW, error)
    if rows:
        _emit(STATUS_NEEDS_REVIEW, decision_id=decision_id, org_id=org_id)
    return {"id": decision_id, "status": STATUS_NEEDS_REVIEW, "error": error}


def _complete(decision_id: int, *, org_id: int, run: dict, facts: dict,
              gate_result: dict) -> dict:
    """Fold a successful run into the row and apply the gate's status."""
    reasoning = {
        "iterations": run["iterations"],
        "final": run["final"],
        "stop_reason": run["stop_reason"],
        "tool_calls": run["tool_calls"],
        "halted": run["halted"],
        "model": run["model"],
        # Audit: taken from the run itself, not re-derived from the caller's
        # intent, so the record reports what the tools were ACTUALLY bound to.
        "identity": run["identity"],
        "evidence": facts["evidence"],
        "gate": gate_result,
    }
    action = run["proposal"]["proposed_action"]
    rows = execute(
        # org_id is SET from the binding as well as matched in the WHERE: the row
        # is filed under the tenant whose data actually produced the evidence.
        #
        # `attempts` is carried across for the same reason needs_review carries
        # it: this may be a RETRY, and "succeeded on attempt 3" is a materially
        # different record from "succeeded" — the earlier failures are why
        # anyone would look at this row twice. Success is not a reason to forget
        # what it took.
        "UPDATE decisions SET lead_id=%s, proposed_action=%s, score=%s, band=%s, "
        "rules_version=%s, status=%s, org_id=%s, "
        "  reasoning = %s::jsonb || (CASE "
        "      WHEN reasoning ? 'attempts' "
        "      THEN jsonb_build_object('attempts', reasoning->'attempts') "
        "      ELSE '{}'::jsonb END) "
        # RETURNING for the same reason as needs_review: an outbound event must
        # follow a transition that actually happened, not one we assumed.
        "WHERE id=%s AND org_id=%s RETURNING id",
        (facts["lead_id"], action, facts["score"], facts["band"],
         facts["rules_version"], gate_result["status"],
         run["identity"]["org_id"], Json(reasoning), decision_id, org_id),
    )
    if rows:
        # The row was filed under the RUN's org (see the SET above), so the
        # event is emitted for that org too — the tenant whose data actually
        # produced the evidence is the tenant whose endpoints hear about it.
        _emit(gate_result["status"], decision_id=decision_id,
              org_id=run["identity"]["org_id"])
    return {
        "id": decision_id,
        "status": gate_result["status"],
        "proposed_action": action,
        "score": facts["score"],
        "band": facts["band"],
        "gate": gate_result,
    }


def process(decision_id: int, email: str, *, org_id: int, role: str) -> dict:
    """Run the loop for one lead and record the outcome. NEVER raises.

    Guarantees, in order of how much they matter:
      1. the row does not stay in 'processing' — every path out of here writes a
         terminal status;
      2. a failure is visible to a human ('needs_review'), not just to stderr;
      3. no proposal is invented on the model's behalf.

    Identity is bound HERE, by caller code, before the model runs, and is
    recorded either way.
    """
    from agent.loop import identity_of, run_qualification  # lazy: Anthropic SDK
    import asyncio

    identity = identity_of(role, org_id)

    try:
        run = asyncio.run(run_qualification(email, role=role, org_id=org_id))
    except BaseException as e:  # noqa: BLE001 — a dead run must still be reviewable
        # BaseException, not Exception, deliberately: a SIGINT/SIGTERM landing
        # mid-run is one of the ways this strands a row, and KeyboardInterrupt
        # and SystemExit are not Exceptions. We record the row and then RE-RAISE
        # anything that isn't a normal Exception, so an interrupt still stops the
        # process — we are borrowing the moment to file the row, not swallowing
        # the shutdown.
        result = needs_review(
            decision_id, org_id=org_id,
            # Prefer the identity the loop actually bound; fall back to ours if
            # it died before binding.
            identity=getattr(e, "identity", identity),
            error=f"{type(e).__name__}: {e}",
            partial_trace=getattr(e, "partial_trace", None),
        )
        if not isinstance(e, Exception):
            raise
        return result

    problem = _proposal_problem(run)
    if problem is not None:
        # The loop finished but gave us nothing to act on. Do NOT default the
        # action — file it for a human with the trace intact.
        return needs_review(
            decision_id, org_id=org_id, identity=run["identity"],
            error=problem, run=run,
        )

    facts = gather_evidence(run)
    # The policy is the ORG's, read here and passed in, so `propose` stays a
    # pure function of (evidence, policy) with no idea that tenants exist. An
    # org with no settings row gets the shipped defaults, so this changes
    # nothing for anyone who has not configured anything.
    gate_result = gate.propose(
        {
            "score": facts["score"],
            "proposed_action": run["proposal"]["proposed_action"],
            "evidence": facts["evidence"],
        },
        config=gate.gate_config_for(org_id),
    )
    return _complete(decision_id, org_id=org_id, run=run, facts=facts,
                     gate_result=gate_result)
