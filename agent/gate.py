"""The approval gate — pure decision logic. LLM proposes, code disposes.

The agent loop only gathers evidence and proposes an action. Nothing here
trusts that proposal: this module applies human-configured thresholds to the
*deterministic* evidence (the rule-based score plus CRM blockers) and decides
whether an action may auto-execute or must wait for a human.

`propose()` is a pure function of its input dict — no DB, no I/O — so the whole
policy is unit-testable in isolation. `approve()`/`reject()` are the only
DB-touching functions, and they transition atomically.

Phase 4 will move CONFIG into the DB as admin-editable settings; for now the
thresholds live here, in one place, not scattered through the code.
"""

from db import execute, query

# --- Human-configured thresholds (Phase 4 makes these admin-editable) --------
CONFIG = {
    "AUTO_EXECUTE_MIN_SCORE": 80,
    "AUTO_DISCARD_MAX_SCORE": 20,
}

# Auto-execute is BLOCKED if any of these hold. Each entry is
# (rule_name, predicate over the evidence dict). Order is the precedence in
# which a fired blocker is reported.
BLOCKERS = (
    ("blocker:no_company", lambda ev: not ev.get("has_company", False)),
    ("blocker:lost_deal", lambda ev: ev.get("lost_deals", 0) > 0),
    ("blocker:open_tickets", lambda ev: ev.get("open_tickets", 0) > 0),
)


def propose(decision: dict) -> dict:
    """Decide the disposition of a proposed action from deterministic evidence.

    Args:
        decision: must contain
            - "score": int (the rule-based lead score)
            - "proposed_action": str (the model's proposal, e.g. 'discard') —
              the floor only auto-discards when the model AGREES it's a discard.
            - "evidence": {
                  "has_company": bool,
                  "lost_deals": int,     # count of deals with stage == 'lost'
                  "open_tickets": int,   # count of tickets with status == 'open'
              }

    Returns a dict:
        {
          "status": "auto_executed" | "auto_discarded" | "pending_approval",
          "rule": <the rule that fired>,          # audit: WHICH rule decided
          "detail": <human-readable explanation>,
          "blockers": [<all blocker rule names that fired>],
        }

    Autonomy at both ends, human in the middle:
      - Blockers win over everything -> pending_approval.
      - score >= AUTO_EXECUTE_MIN_SCORE, no blockers            -> auto_executed.
      - score <= AUTO_DISCARD_MAX_SCORE AND proposed_action is
        'discard', no blockers                                  -> auto_discarded.
      - score <= floor but the model did NOT propose discard
        (model disagreement)                                    -> pending_approval.
      - everything else                                         -> pending_approval.
    """
    score = decision.get("score")
    action = decision.get("proposed_action")
    evidence = decision.get("evidence", {})
    min_score = CONFIG["AUTO_EXECUTE_MIN_SCORE"]
    max_discard = CONFIG["AUTO_DISCARD_MAX_SCORE"]

    fired = [name for name, pred in BLOCKERS if pred(evidence)]

    if fired:
        return {
            "status": "pending_approval",
            "rule": fired[0],
            "detail": f"blocked by {', '.join(fired)} (score {score})",
            "blockers": fired,
        }

    if score is not None and score >= min_score:
        return {
            "status": "auto_executed",
            "rule": "auto_execute:threshold_met",
            "detail": f"score {score} >= {min_score} and no blockers",
            "blockers": [],
        }

    if score is not None and score <= max_discard:
        if action == "discard":
            return {
                "status": "auto_discarded",
                "rule": "auto_discard:below_floor",
                "detail": (
                    f"score {score} <= {max_discard}, model proposed discard, "
                    "no blockers"
                ),
                "blockers": [],
            }
        # Deterministic floor says discard, but the model proposed something
        # else — the disagreement itself is a reason to gate to a human.
        return {
            "status": "pending_approval",
            "rule": "pending:floor_action_mismatch",
            "detail": (
                f"score {score} <= {max_discard} but proposed_action is "
                f"{action!r}, not discard"
            ),
            "blockers": [],
        }

    return {
        "status": "pending_approval",
        "rule": "pending:below_threshold",
        "detail": f"score {score} between {max_discard} and {min_score}, no blockers",
        "blockers": [],
    }


# --- Atomic state transitions (the only DB writes in the gate) ---------------

def approve(decision_id: int, decided_by: str, org_id: int | None = None) -> dict:
    """Approve a pending decision. Atomic; see `_transition`."""
    return _transition(decision_id, "approved", decided_by, org_id)


def reject(decision_id: int, decided_by: str, org_id: int | None = None) -> dict:
    """Reject a pending decision. Atomic; see `_transition`."""
    return _transition(decision_id, "rejected", decided_by, org_id)


def _transition(
    decision_id: int, new_status: str, decided_by: str, org_id: int | None = None
) -> dict:
    """Atomically move a decision out of 'pending_approval'.

    Guards the transition in the WHERE clause so two concurrent approvals
    can't both win — the second sees rowcount 0. No read-then-write, so there
    is no TOCTOU window. A rowcount of 0 is returned as a clear error, not
    raised, because "already decided" is an expected outcome, not a bug.

    When org_id is provided (the API path), it is added to the same WHERE clause
    so the transition is org-scoped in one atomic statement — a caller cannot
    decide another org's decision. When None (the CLI path), no org clause is
    added.
    """
    if org_id is None:
        sql = (
            "UPDATE decisions SET status=%s, decided_by=%s, decided_at=now() "
            "WHERE id=%s AND status='pending_approval' RETURNING id"
        )
        params = (new_status, decided_by, decision_id)
    else:
        sql = (
            "UPDATE decisions SET status=%s, decided_by=%s, decided_at=now() "
            "WHERE id=%s AND org_id=%s AND status='pending_approval' RETURNING id"
        )
        params = (new_status, decided_by, decision_id, org_id)
    rows = execute(sql, params)
    if not rows:
        return {
            "ok": False,
            "error": (
                f"decision {decision_id} is not pending_approval "
                "(already decided, or does not exist)"
            ),
        }

    # Outbound event, emitted here rather than in the API handler because this
    # is the ONE place both doors converge: `api/main.py` approves over HTTP and
    # `agent/cli.py` approves from a terminal, and a webhook that only fired for
    # one of them would be a subscription that lies about how it works.
    #
    # Only on the winning path — `rows` empty means another approval got there
    # first, and that caller already emitted. Exactly one event per transition.
    _emit_transition(decision_id, new_status, org_id)

    return {"ok": True, "id": rows[0]["id"], "status": new_status}


def _emit_transition(decision_id: int, new_status: str,
                     org_id: int | None) -> None:
    """Fire the outbound webhook for an approve/reject. Never raises.

    The decision is committed before this runs. A registered endpoint being
    unreachable, misconfigured or hostile is not a reason to fail a human's
    approval, so every failure is swallowed and logged.

    The CLI path passes org_id=None (no tenant scoping on that door), so the org
    is read back from the row — the event still has to be delivered to exactly
    one tenant's endpoints, and guessing 1 would cross-post another org's
    decision to whoever happened to be org 1.
    """
    try:
        import webhooks

        target_org = org_id
        if target_org is None:
            rows = query(
                "SELECT org_id FROM decisions WHERE id = %s", (decision_id,)
            )
            if not rows:
                return
            target_org = rows[0]["org_id"]

        webhooks.emit_for_status(new_status, decision_id=decision_id,
                                 org_id=target_org)
    except Exception:  # noqa: BLE001 — a webhook must not undo an approval
        import logging

        logging.getLogger("brains-gate").exception(
            "webhook emission failed for decision %s (%s); the transition "
            "itself stands", decision_id, new_status,
        )
