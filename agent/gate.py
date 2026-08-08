"""The approval gate — pure decision logic. LLM proposes, code disposes.

The agent loop only gathers evidence and proposes an action. Nothing here
trusts that proposal: this module applies human-configured thresholds to the
*deterministic* evidence (the rule-based score plus CRM blockers) and decides
whether an action may auto-execute or must wait for a human.

`propose()` is a pure function of its input dict — no DB, no I/O — so the whole
policy is unit-testable in isolation. That purity is preserved now that policy
is per-org: the CONFIG is *passed in*, and reading it from the database is a
separate function (`gate_config_for`). The decision logic never learns what an
org_id is.

`gate_config_for()`, `approve()` and `reject()` are the only DB-touching
functions here, and the transitions are atomic.
"""

import logging

from db import execute, query
from rules import DEFAULT_DECISION_TYPE

logger = logging.getLogger("brains-gate")

# --- Policy provenance -------------------------------------------------------
#
# Every gate result carries WHICH policy decided it, for the same reason the
# decision row carries `rules_version`: an argument about a ruling should be a
# query, not an archaeology exercise. "It auto-executed at 80" is a different
# fact from "it auto-executed at 80 because this org's settings were unreadable
# and it fell back", and only one of them is a bug.
POLICY_ORG_SETTINGS = "org_settings"   # at least one field came from the org's row
POLICY_DEFAULTS = "defaults"           # no row, or a row that overrides nothing
POLICY_UNREADABLE = "fallback_unreadable"  # the read FAILED; see FALLBACK_CONFIG

# --- Default thresholds ------------------------------------------------------
#
# These remain the policy for any org that has not chosen its own. They are the
# DEFAULT, not the setting: `gate_config_for` overlays an org's `org_settings`
# row on top of this dict, field by field, so an org that customises one
# threshold does not silently inherit a hardcoded value for the other.
CONFIG = {
    "AUTO_EXECUTE_MIN_SCORE": 80,
    "AUTO_DISCARD_MAX_SCORE": 20,
}

# Every blocker this system knows how to evaluate, by name.
#
# PREDICATES STAY IN CODE, DELIBERATELY. A per-org blocker is a CHOICE AMONG
# THESE, never a new expression supplied by a tenant: a settings row that could
# introduce arbitrary predicate logic would be a code-execution surface reachable
# by anyone who can write an org's settings, to gain the ability to express
# conditions over three integers. The registry is the safe half of that trade.
BLOCKER_REGISTRY = {
    "blocker:no_company": lambda ev: not ev.get("has_company", False),
    "blocker:lost_deal": lambda ev: ev.get("lost_deals", 0) > 0,
    "blocker:open_tickets": lambda ev: ev.get("open_tickets", 0) > 0,
}

# The default set, in the precedence order a fired blocker is reported in.
DEFAULT_BLOCKERS = (
    "blocker:no_company",
    "blocker:lost_deal",
    "blocker:open_tickets",
)

# Auto-execute is BLOCKED if any of these hold. Each entry is
# (rule_name, predicate over the evidence dict). Order is the precedence in
# which a fired blocker is reported. Kept as a resolved tuple because it is the
# shape `propose` wants and the shape the original tests read.
BLOCKERS = tuple((name, BLOCKER_REGISTRY[name]) for name in DEFAULT_BLOCKERS)

# What the gate falls back to when the settings read FAILS. Not the defaults:
# BOTH AUTONOMY ENDS ARE SWITCHED OFF and every decision is held for a human.
#
# The shipped defaults look conservative and are not. They are conservative only
# for an org that LOOSENED its policy; for an org sitting at min_score=95, a
# failed read would auto-execute at 80 — a silent policy loosening, which is
# precisely the failure class this product exists to prevent. Nor is the window
# narrow: a missing `org_settings` table is a bad migration with the decision
# path otherwise healthy, so this is reachable without a database outage.
#
# The cost is a full pending queue while settings are unreadable. That is
# visible and annoying, and it beats invisible and wrong.
#
# None thresholds mean "this autonomy end does not exist", not "zero" — see
# `propose`, which refuses to compare against them.
FALLBACK_CONFIG = {
    "AUTO_EXECUTE_MIN_SCORE": None,
    "AUTO_DISCARD_MAX_SCORE": None,
    "BLOCKERS": BLOCKERS,
    "POLICY_SOURCE": POLICY_UNREADABLE,
}


class UnknownBlocker(ValueError):
    """An org's settings named a blocker this build does not implement."""


def resolve_blockers(names) -> tuple:
    """Turn a sequence of blocker names into (name, predicate) pairs.

    Order is the caller's, because order IS the precedence in which a fired
    blocker is reported — an org that considers an open ticket the headline
    problem can say so by listing it first.

    An unknown name RAISES rather than being skipped. Silently ignoring it would
    mean a typo in a settings row quietly disables a block an admin believes is
    switched on, which is the failure you least want to be silent.
    """
    resolved = []
    for name in names:
        if name not in BLOCKER_REGISTRY:
            raise UnknownBlocker(
                f"unknown blocker {name!r}; known: "
                f"{', '.join(sorted(BLOCKER_REGISTRY))}"
            )
        resolved.append((name, BLOCKER_REGISTRY[name]))
    return tuple(resolved)


def gate_config_for(org_id: int,
                    decision_type: str = DEFAULT_DECISION_TYPE) -> dict:
    """Read one org's gate policy FOR ONE DECISION TYPE, defaults field by field.

    The key is (org_id, decision_type), not org_id: two decision types in one
    tenant are two different judgements, and a per-org key would force one to
    inherit the other's autonomy settings. An org that has tightened the gate on
    a high-stakes type must not thereby tighten a routine one, and — the
    direction that actually matters — loosening a routine type must not loosen
    the high-stakes one along with it.

    Returns a dict shaped for `propose(decision, config=...)`:

        {"AUTO_EXECUTE_MIN_SCORE": int | None,
         "AUTO_DISCARD_MAX_SCORE": int | None,
         "BLOCKERS": ((name, predicate), ...),
         "POLICY_SOURCE": one of the POLICY_* constants,
         "POLICY_ERROR": str, only when the read failed}

    THE FALLBACK IS PER-FIELD, NOT PER-ROW. An org_settings row with only
    `auto_execute_min_score` set must not drag the discard floor along with it;
    each NULL means "use the default for this one thing". A row-level fallback
    would make customising one threshold silently freeze the other at whatever
    the default happened to be when the row was written.

    NO ROW AT ALL IS NORMAL, not an error: an org that has never opened the
    settings page gets exactly today's behaviour, and the defaults ARE that
    org's policy.

    A FAILED READ IS NOT THAT. It returns FALLBACK_CONFIG — every decision held
    for a human — rather than the defaults, because for an org that raised its
    threshold the defaults are a silent LOOSENING of a policy it chose. See
    FALLBACK_CONFIG for the full argument. This function never raises; it
    reports the failure through POLICY_SOURCE instead, so the fallback is
    visible on every row it gated rather than only in the logs.
    """
    config = {
        "AUTO_EXECUTE_MIN_SCORE": CONFIG["AUTO_EXECUTE_MIN_SCORE"],
        "AUTO_DISCARD_MAX_SCORE": CONFIG["AUTO_DISCARD_MAX_SCORE"],
        "BLOCKERS": BLOCKERS,
        "POLICY_SOURCE": POLICY_DEFAULTS,
    }

    try:
        rows = query(
            "SELECT auto_execute_min_score, auto_discard_max_score, blockers "
            "FROM org_settings WHERE org_id = %s AND decision_type = %s",
            (org_id, decision_type),
        )
    except Exception as e:  # noqa: BLE001 — reported as policy, never raised
        logger.exception(
            "could not read org_settings for org %s / type %s; holding every "
            "decision for a human until it is readable", org_id, decision_type,
        )
        return {**FALLBACK_CONFIG, "POLICY_ERROR": type(e).__name__}

    if not rows:
        return config

    row = rows[0]
    if row["auto_execute_min_score"] is not None:
        config["AUTO_EXECUTE_MIN_SCORE"] = row["auto_execute_min_score"]
        config["POLICY_SOURCE"] = POLICY_ORG_SETTINGS
    if row["auto_discard_max_score"] is not None:
        config["AUTO_DISCARD_MAX_SCORE"] = row["auto_discard_max_score"]
        config["POLICY_SOURCE"] = POLICY_ORG_SETTINGS
    if row["blockers"] is not None:
        # An explicit empty list is a real choice — "this org blocks on
        # nothing" — and is distinct from NULL, which means "not configured".
        config["BLOCKERS"] = resolve_blockers(row["blockers"])
        config["POLICY_SOURCE"] = POLICY_ORG_SETTINGS

    # A row that exists but overrides nothing leaves POLICY_SOURCE at
    # 'defaults', because provenance names the policy that ACTUALLY applied —
    # and an all-NULL row applied none of itself.
    return config


def propose(decision: dict, config: dict | None = None) -> dict:
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
        config: the org's policy, as returned by `gate_config_for`. None means
            the shipped defaults — which keeps this callable as `propose(d)` by
            anything that has no org in hand (tests, the CLI, a REPL) and keeps
            the default policy in exactly one place rather than duplicated into
            every caller that does not care.

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

    # Read through to the module defaults per field, so a caller may pass a
    # partial config (a test overriding one threshold) without having to restate
    # the rest of the policy to leave it alone.
    config = config or {}
    min_score = config.get("AUTO_EXECUTE_MIN_SCORE",
                           CONFIG["AUTO_EXECUTE_MIN_SCORE"])
    max_discard = config.get("AUTO_DISCARD_MAX_SCORE",
                             CONFIG["AUTO_DISCARD_MAX_SCORE"])
    blockers = config.get("BLOCKERS", BLOCKERS)
    source = config.get("POLICY_SOURCE", POLICY_DEFAULTS)
    policy_error = config.get("POLICY_ERROR")

    def ruled(status: str, rule: str, detail: str, fired: list | None = None) -> dict:
        """Stamp provenance onto every exit. One helper so none can miss it."""
        out = {
            "status": status,
            "rule": rule,
            "detail": detail,
            "blockers": fired or [],
            "policy_source": source,
        }
        if policy_error is not None:
            out["policy_error"] = policy_error
        return out

    fired = [name for name, pred in blockers if pred(evidence)]

    if fired:
        return ruled("pending_approval", fired[0],
                     f"blocked by {', '.join(fired)} (score {score})", fired)

    # FAIL CLOSED. The policy could not be read, so neither autonomy end is
    # available: applying the shipped defaults here would gate this org by a
    # policy it did not choose, and for an org that raised its threshold that
    # is a silent loosening. Held for a human, and the row says why.
    if source == POLICY_UNREADABLE:
        return ruled(
            "pending_approval", "pending:policy_unreadable",
            f"gate policy could not be read ({policy_error}); score {score} is "
            "held for a human rather than judged by a policy this org did not "
            "choose",
        )

    if min_score is not None and score is not None and score >= min_score:
        return ruled("auto_executed", "auto_execute:threshold_met",
                     f"score {score} >= {min_score} and no blockers")

    if max_discard is not None and score is not None and score <= max_discard:
        if action == "discard":
            return ruled(
                "auto_discarded", "auto_discard:below_floor",
                f"score {score} <= {max_discard}, model proposed discard, "
                "no blockers",
            )
        # Deterministic floor says discard, but the model proposed something
        # else — the disagreement itself is a reason to gate to a human.
        return ruled(
            "pending_approval", "pending:floor_action_mismatch",
            f"score {score} <= {max_discard} but proposed_action is "
            f"{action!r}, not discard",
        )

    return ruled(
        "pending_approval", "pending:below_threshold",
        f"score {score} between {max_discard} and {min_score}, no blockers",
    )


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
        logger.exception(
            "webhook emission failed for decision %s (%s); the transition "
            "itself stands", decision_id, new_status,
        )
