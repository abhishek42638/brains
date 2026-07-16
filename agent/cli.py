"""Thin CLI to drive the Phase 2 agent + gate.

    uv run python -m agent.cli qualify <email>
    uv run python -m agent.cli pending
    uv run python -m agent.cli show <id>
    uv run python -m agent.cli approve <id> --by <name>
    uv run python -m agent.cli reject <id> --by <name>

The CLI is deliberately thin: it wires the loop's evidence into the gate and
persists the decision. It makes no policy choices of its own.
"""

import argparse
import asyncio
import json
import os
import sys

from psycopg.types.json import Json

from agent import gate
from db import execute, query


def _last_result(run: dict, tool_name: str):
    """Most recent result payload for a given tool in the trace, or None."""
    for it in reversed(run["iterations"]):
        if it.get("tool") == tool_name and isinstance(it.get("result"), dict):
            return it["result"]
    return None


def _gather_evidence(run: dict) -> dict:
    """Reduce the loop's raw trace to the deterministic facts the gate needs.

    The gate never sees the model's opinion — only these facts, pulled from the
    actual tool results the loop captured.
    """
    lookup = _last_result(run, "lookup_lead") or {}
    crm = _last_result(run, "check_crm") or {}
    score_res = _last_result(run, "score_lead") or {}

    lead = lookup.get("lead") or {}
    company = lookup.get("company")  # None if the lead has no company on file

    lead_id = lead.get("id")
    has_company = company is not None

    lost_deals = sum(1 for d in crm.get("deals", []) if d.get("stage") == "lost")
    open_tickets = crm.get("open_tickets")
    if open_tickets is None:
        open_tickets = sum(
            1 for t in crm.get("tickets", []) if t.get("status") == "open"
        )

    return {
        "lead_id": lead_id,
        "score": score_res.get("score"),
        "band": score_res.get("band"),
        "rules_version": score_res.get("rules_version"),
        "evidence": {
            "has_company": has_company,
            "lost_deals": lost_deals,
            "open_tickets": open_tickets,
        },
    }


def cmd_qualify(args) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set — the qualify loop needs it "
              "to call the Anthropic API.", file=sys.stderr)
        return 2

    import anthropic  # lazy: only this path needs the SDK

    from agent.loop import run_qualification  # lazy: only this path needs the API

    try:
        run = asyncio.run(run_qualification(args.email))
    except anthropic.APIError as e:
        print(f"error: Anthropic API call failed: {e}", file=sys.stderr)
        return 2
    facts = _gather_evidence(run)

    proposal = run["proposal"]
    proposed_action = proposal.get("proposed_action") or "nurture"  # NOT NULL fallback

    gate_result = gate.propose({
        "score": facts["score"],
        "proposed_action": proposed_action,
        "evidence": facts["evidence"],
    })
    status = gate_result["status"]

    reasoning = {
        "iterations": run["iterations"],
        "final": run["final"],
        "stop_reason": run["stop_reason"],
        "tool_calls": run["tool_calls"],
        "halted": run["halted"],
        "model": run["model"],
        # Audit: the privilege the tools actually ran at.
        "role": run["role"],
        "org_id": run["org_id"],
        "evidence": facts["evidence"],
        "gate": gate_result,
    }

    rows = execute(
        "INSERT INTO decisions "
        "(org_id, lead_id, trigger_input, proposed_action, score, band, "
        " rules_version, reasoning, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, status",
        (
            1,
            facts["lead_id"],
            Json({"email": args.email}),
            proposed_action,
            facts["score"],
            facts["band"],
            facts["rules_version"],
            Json(reasoning),
            status,
        ),
    )
    row = rows[0]
    print(f"decision id={row['id']} status={row['status']}")
    print(f"  proposed_action={proposed_action} "
          f"score={facts['score']} band={facts['band']}")
    print(f"  gate rule: {gate_result['rule']} — {gate_result['detail']}")
    if proposal.get("parse_error"):
        print(f"  NOTE: proposal parse_error: {proposal['parse_error']}",
              file=sys.stderr)
    return 0


def cmd_pending(args) -> int:
    rows = query(
        "SELECT id, lead_id, proposed_action, score, band, status, created_at "
        "FROM decisions WHERE status = 'pending_approval' ORDER BY id"
    )
    if not rows:
        print("no pending decisions")
        return 0
    print(f"{'id':>3}  {'lead':>4}  {'action':<14}  {'score':>5}  "
          f"{'band':<5}  created_at")
    for r in rows:
        print(f"{r['id']:>3}  {str(r['lead_id']):>4}  "
              f"{r['proposed_action']:<14}  {str(r['score']):>5}  "
              f"{str(r['band'] or ''):<5}  {r['created_at']}")
    return 0


def cmd_show(args) -> int:
    rows = query("SELECT * FROM decisions WHERE id = %s", (args.id,))
    if not rows:
        print(f"no decision with id {args.id}", file=sys.stderr)
        return 1
    print(json.dumps(rows[0], indent=2, default=str))
    return 0


def cmd_approve(args) -> int:
    return _decide(gate.approve(args.id, args.by))


def cmd_reject(args) -> int:
    return _decide(gate.reject(args.id, args.by))


def _decide(result: dict) -> int:
    if result.get("ok"):
        print(f"decision {result['id']} -> {result['status']}")
        return 0
    print(f"error: {result['error']}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent.cli", description="Phase 2 agent driver")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("qualify", help="run the loop for a lead and write a decision")
    q.add_argument("email")
    q.set_defaults(func=cmd_qualify)

    pen = sub.add_parser("pending", help="list pending decisions")
    pen.set_defaults(func=cmd_pending)

    sh = sub.add_parser("show", help="full trace for a decision")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_show)

    ap = sub.add_parser("approve", help="approve a pending decision")
    ap.add_argument("id", type=int)
    ap.add_argument("--by", required=True, help="who is approving")
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="reject a pending decision")
    rj.add_argument("id", type=int)
    rj.add_argument("--by", required=True, help="who is rejecting")
    rj.set_defaults(func=cmd_reject)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
