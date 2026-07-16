"""Mint the demo API keys. Prints each raw key ONCE; stores only its sha256.

    uv run python seed_keys.py            # create the two demo keys
    uv run python seed_keys.py --list     # show existing keys (never the secret)
    uv run python seed_keys.py --revoke N # revoke key id N

The raw keys scroll past exactly once because that is the only moment they
exist in a readable form: `api_keys` holds sha256(key), so nothing here — and
nothing in a database dump — can recover them afterwards. Losing one means
minting another, which is the correct trade for a credential.

Re-running is safe: the demo keys are matched by name and skipped if present,
because minting a second 'demo sales key' on every run would leave a pile of
live credentials nobody is tracking.
"""

import argparse
import sys

import config  # noqa: F401  — loads .env before db reads DATABASE_URL
from auth import create_key, revoke_key
from db import query

DEMO_KEYS = [
    {"org_id": 1, "role": "sales", "name": "demo sales key"},
    {"org_id": 1, "role": "admin", "name": "demo admin key"},
]


def cmd_list() -> int:
    rows = query(
        "SELECT id, org_id, role, name, created_at, revoked_at "
        "FROM api_keys ORDER BY id"
    )
    if not rows:
        print("no api keys — run `uv run python seed_keys.py` to create the demo pair")
        return 0
    print(f"{'id':>3}  {'org':>3}  {'role':<6}  {'name':<20}  {'state':<8}  created_at")
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        print(f"{r['id']:>3}  {r['org_id']:>3}  {r['role']:<6}  {r['name']:<20}  "
              f"{state:<8}  {r['created_at']}")
    print("\n(raw keys are not stored and cannot be shown — only their sha256)")
    return 0


def cmd_revoke(key_id: int) -> int:
    if revoke_key(key_id):
        print(f"key {key_id} revoked — it now 401s like any unknown key")
        return 0
    print(f"key {key_id} not found, or already revoked", file=sys.stderr)
    return 1


def cmd_seed() -> int:
    existing = {r["name"] for r in query("SELECT name FROM api_keys")}
    minted = []
    for spec in DEMO_KEYS:
        if spec["name"] in existing:
            print(f"  {spec['name']:<20} already exists — skipped "
                  f"(revoke and re-run to rotate)")
            continue
        raw, key_id = create_key(**spec)
        minted.append((spec, raw, key_id))

    if not minted:
        print("\nnothing new to mint. `--list` shows what exists.")
        return 0

    print("\n" + "=" * 78)
    print("RAW API KEYS — SHOWN ONCE, NOT RECOVERABLE. Copy them now.")
    print("=" * 78)
    for spec, raw, key_id in minted:
        print(f"\n  {spec['name']}  (id={key_id}, org={spec['org_id']}, "
              f"role={spec['role']})")
        print(f"    {raw}")
    print("\n" + "=" * 78)
    print("Use as:  curl -H 'X-API-Key: <key>' ...")
    print("The sales key gets a sales-bound agent (no admin knowledge chunks);")
    print("the admin key gets an admin-bound one. Same URL, different privilege —")
    print("decided by the credential, never by the request or the model.")
    print("=" * 78)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="mint/inspect BRAINS API keys")
    p.add_argument("--list", action="store_true", help="list keys (never secrets)")
    p.add_argument("--revoke", type=int, metavar="ID", help="revoke a key by id")
    args = p.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.revoke is not None:
        return cmd_revoke(args.revoke)
    return cmd_seed()


if __name__ == "__main__":
    raise SystemExit(main())
