"""Thin CLI to drive the Phase 2 agent + gate.

    uv run python -m agent.cli qualify <email>
    uv run python -m agent.cli pending
    uv run python -m agent.cli show <id>
    uv run python -m agent.cli approve <id> --by <name>
    uv run python -m agent.cli reject <id> --by <name>

The CLI is deliberately thin: it binds identity, then hands off to
decisions.process() — the same write path the API uses. It makes no policy
choices of its own and owns no SQL for creating decisions.
"""

import argparse
import json
import os
import sys

import config  # noqa: F401  — loads .env before anything reads os.environ
import decisions
from agent import gate
from db import query


# The CLI's own binding. Least privilege, same as the API's: this is caller
# code, so it is where identity is decided. See server.py's module docstring.
CLI_ROLE = "sales"
CLI_ORG_ID = 1


def cmd_qualify(args) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set — the qualify loop needs it "
              "to call the Anthropic API.", file=sys.stderr)
        return 2

    from agent.loop import identity_of  # lazy: only this path needs the SDK

    identity = identity_of(CLI_ROLE, CLI_ORG_ID)

    # Same two-phase lifecycle as the API — create in 'processing', then let
    # decisions.process() drive it to a terminal state. The CLI is synchronous
    # so it could have gone straight to a final INSERT (it used to), but then
    # this table would have two shapes of row again, and a CLI run killed
    # mid-flight would leave no trace at all rather than a reviewable one.
    decision_id = decisions.create_processing(
        org_id=CLI_ORG_ID,
        trigger_input={"email": args.email, "source": "cli"},
        identity=identity,
    )
    result = decisions.process(  # never raises; never leaves 'processing'
        decision_id, args.email, org_id=CLI_ORG_ID, role=CLI_ROLE,
    )

    print(f"decision id={result['id']} status={result['status']}")
    if result["status"] == decisions.STATUS_NEEDS_REVIEW:
        print(f"  NEEDS REVIEW: {result['error']}", file=sys.stderr)
        print("  the run did not produce a decision — it is in the pending "
              "queue for a human, not silently dropped", file=sys.stderr)
        return 1
    print(f"  proposed_action={result['proposed_action']} "
          f"score={result['score']} band={result['band']}")
    print(f"  gate rule: {result['gate']['rule']} — {result['gate']['detail']}")
    return 0


def cmd_pending(args) -> int:
    # Same queue as GET /decisions/pending — including needs_review, so a broken
    # run is visible here too rather than only to whoever reads the API.
    rows = query(
        "SELECT id, lead_id, proposed_action, score, band, status, created_at "
        "FROM decisions WHERE status = ANY(%s) ORDER BY id",
        (list(decisions.HUMAN_QUEUE_STATUSES),),
    )
    if not rows:
        print("no decisions awaiting a human")
        return 0
    print(f"{'id':>3}  {'lead':>4}  {'action':<14}  {'score':>5}  "
          f"{'band':<5}  {'status':<16}  created_at")
    for r in rows:
        print(f"{r['id']:>3}  {str(r['lead_id']):>4}  "
              f"{r['proposed_action']:<14}  {str(r['score']):>5}  "
              f"{str(r['band'] or ''):<5}  {r['status']:<16}  {r['created_at']}")
    return 0


def cmd_sweep(args) -> int:
    """Rescue rows abandoned in 'processing' by a killed/restarted worker."""
    swept = decisions.sweep_stuck(older_than_minutes=args.older_than)
    if not swept:
        print(f"no decisions stuck in 'processing' for over {args.older_than}m")
        return 0
    print(f"swept {len(swept)} abandoned decision(s) -> "
          f"{decisions.STATUS_NEEDS_REVIEW}:")
    for r in swept:
        print(f"  id={r['id']} org={r['org_id']} created_at={r['created_at']}")
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

    pen = sub.add_parser("pending", help="list decisions awaiting a human")
    pen.set_defaults(func=cmd_pending)

    sw = sub.add_parser(
        "sweep", help="rescue decisions abandoned in 'processing' by a dead worker")
    sw.add_argument("--older-than", type=int, default=decisions.STUCK_AFTER_MINUTES,
                    metavar="MINUTES", help="age threshold (default: %(default)s)")
    sw.set_defaults(func=cmd_sweep)

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
