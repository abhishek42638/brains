"""API-key authentication: the last link in the identity chain.

The chain now runs end to end, with no trusted-from-caller gap left in it:

    X-API-Key header
      -> sha256                       (the raw key is never stored)
      -> api_keys row                 (org_id, role)  <- the ONLY source of identity
      -> build_mcp(role=, org_id=)    (closed over; no tool schema exposes them)
      -> SQL WHERE org_id / permitted_roles

Every earlier phase pushed identity one step further from the model. This phase
removes the last place a *caller* could assert it: `org_id` is gone from request
bodies and query params entirely. You are the org your credential says you are.

WHY A HASH, NOT THE KEY. `key_hash` is sha256 of the raw key and the raw key is
printed exactly once, at creation. A leak of the database is then not a leak of
usable credentials. sha256 (not bcrypt/argon2) is the right tool here *because*
these are high-entropy random keys, not passwords: there is no dictionary to
attack, so the slow-KDF property buys nothing, and this runs on every request.

WHY CONSTANT-TIME MATTERS LESS THAN IT LOOKS. The lookup is an indexed equality
match on the hash, and an attacker cannot influence the stored side, so there is
no secret to leak through comparison timing — Postgres compares hashes, not the
key. What we do NOT do is log the raw key or return it in errors.

REVOKED IS NOT DELETED. A revoked key still resolves for audit ("which
credential triggered decision 41?") but authenticates nothing: `revoked_at IS
NOT NULL` is a 401, indistinguishable to the caller from a key that never
existed.
"""

import hashlib
import logging
import os
import secrets

import config  # noqa: F401  — loads .env before we read os.environ
from fastapi import Header, HTTPException
from pydantic import BaseModel

from db import execute, query, transaction

# The Cloud Scheduler service account allowed to call /internal/sweep. Distinct
# from the Cloud Tasks account on purpose — see require_scheduler.
SCHEDULER_SERVICE_ACCOUNT = os.environ.get("SCHEDULER_SERVICE_ACCOUNT")

logger = logging.getLogger("brains-auth")
if not logger.handlers:
    _h = logging.StreamHandler()  # stderr
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

KEY_PREFIX = "brains_sk_"
KEY_BYTES = 32  # 256 bits of entropy from secrets.token_urlsafe

# Requests per key per hour to POST /decisions/trigger.
#
# 20/hour. Each trigger is an agent loop: up to MAX_ITERATIONS (8) model turns
# plus tool calls, so this is the knob that bounds what one leaked credential
# can spend on Anthropic. 20 is comfortably above any demo or human-paced use
# (a person triggering a lead every 3 minutes for an hour never notices it) and
# far below what a script could burn unattended. It is a per-credential ceiling,
# not per-IP: an IP is not an identity, and every caller here has one.
#
# Deliberately NOT applied to reads or to approve/reject — those are cheap, and
# throttling a human trying to clear their queue is the wrong failure.
TRIGGER_RATE_LIMIT_PER_HOUR = 20


class Principal(BaseModel):
    """Who is calling. The sole source of org_id and role for a request."""

    api_key_id: int
    org_id: int
    role: str
    name: str


def hash_key(raw_key: str) -> str:
    """sha256 hex of a raw API key. The only form we ever persist."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """A fresh random API key. Shown once, then only its hash survives."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def create_key(*, org_id: int, role: str, name: str) -> tuple[str, int]:
    """Mint a key for (org_id, role). Returns (raw_key, api_key_id).

    The raw key is returned to the CALLER of this function and never written
    down: print it once, then it is unrecoverable by design. Losing it means
    minting another, which is the correct trade.
    """
    from server import KNOWN_ROLES  # local: avoids importing the MCP server eagerly

    if role not in KNOWN_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {KNOWN_ROLES}")

    raw = generate_key()
    rows = execute(
        "INSERT INTO api_keys (org_id, role, key_hash, name) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (org_id, role, hash_key(raw), name),
    )
    return raw, rows[0]["id"]


def revoke_key(api_key_id: int) -> bool:
    """Revoke a key. Idempotent; returns True if this call did the revoking."""
    rows = execute(
        "UPDATE api_keys SET revoked_at = now() "
        "WHERE id = %s AND revoked_at IS NULL RETURNING id",
        (api_key_id,),
    )
    return bool(rows)


def resolve(raw_key: str) -> Principal | None:
    """Raw key -> Principal, or None if unknown/revoked.

    One statement: the revocation check is part of the WHERE, so a revoked key
    cannot be resolved-then-checked (and cannot slip through a window between
    the two).
    """
    rows = query(
        "SELECT id, org_id, role, name FROM api_keys "
        "WHERE key_hash = %s AND revoked_at IS NULL",
        (hash_key(raw_key),),
    )
    if not rows:
        return None
    r = rows[0]
    return Principal(api_key_id=r["id"], org_id=r["org_id"], role=r["role"],
                     name=r["name"])


# --- FastAPI dependency ------------------------------------------------------

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="missing or invalid API key (send it in the X-API-Key header)",
    headers={"WWW-Authenticate": "X-API-Key"},
)


def require_principal(x_api_key: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: authenticate the caller or 401.

    Missing, unknown, and revoked keys all produce the SAME 401 with the same
    body. Distinguishing them would tell an attacker which of their guesses was
    once a real key — the caller's remedy is identical in all three cases, so
    the extra detail is a gift to the wrong audience. The server log records
    which it was.
    """
    if not x_api_key:
        logger.info("auth: request with no X-API-Key header")
        raise _UNAUTHORIZED

    principal = resolve(x_api_key)
    if principal is None:
        # Never log the key itself — a rejected key may be a valid key for
        # somewhere else, or a typo of one. The hash prefix is enough to
        # correlate repeated attempts without recording the credential.
        logger.warning("auth: rejected key (sha256 prefix %s...)",
                       hash_key(x_api_key)[:12])
        raise _UNAUTHORIZED

    logger.info("auth: %s (org=%s role=%s)", principal.name, principal.org_id,
                principal.role)
    return principal


# --- Rate limiting -----------------------------------------------------------

# --- Cloud Tasks OIDC: the internal endpoint's only door --------------------

_FORBIDDEN = HTTPException(status_code=403, detail="not a valid Cloud Tasks request")


def _verify_google_oidc(authorization: str | None, expected_sa: str | None,
                        what: str) -> dict:
    """Verify a Google-signed OIDC token from ONE expected service account.

    Shared by the Cloud Tasks and Cloud Scheduler doors: same three checks, a
    different expected issuer. Written once so the two callers cannot drift —
    an endpoint whose auth is a near-copy of another endpoint's auth is how one
    of them ends up missing the audience check.

    Three checks, and all three are load-bearing:

      1. SIGNATURE + EXPIRY — id_token.verify_token. Note this is verify_token,
         NOT verify_oauth2_token: the latter is for Google-sign-in ID tokens
         where the audience is an OAuth client ID, not a service identity token
         where the audience is our URL. (Verified: docs.cloud.google.com/run/
         docs/authenticating/service-to-service uses verify_token.)

      2. AUDIENCE — passed explicitly. google-auth SKIPS audience validation
         entirely when audience=None, and the Cloud Run doc's own sample omits
         it. A token minted for SOMEONE ELSE'S service would then sail through
         signature validation and be accepted here. The audience is what ties a
         token to us.

      3. ISSUER SERVICE ACCOUNT — pinned by email. Audience alone is not enough:
         ANY Google-issued OIDC token minted for our URL passes (1) and (2),
         including one from an unrelated service account in an unrelated project
         that happens to know our address. The email claim is what says "Cloud
         Tasks, acting as the account we granted this to."

    Dropping any one leaves a door: no signature check = anyone can forge; no
    audience = any Google token works; no email pin = any Google *caller* works.

    Returns the decoded claims so the handler can log who invoked it.
    """
    import tasks  # local: avoids a circular import at module load

    if tasks.emulated():
        # Local/dev: there is no Cloud Tasks and no metadata server to verify
        # against. Chosen by configuration (see tasks.emulated), never by
        # sniffing the environment — a deployed service always has TASKS_QUEUE
        # set, so this branch cannot be reached in the cloud by accident.
        logger.info("%s: OIDC verification skipped (tasks emulated)", what)
        return {"email": "emulated", "emulated": True}

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("%s: request with no bearer token", what)
        raise _FORBIDDEN

    token = authorization.removeprefix("Bearer ").strip()
    audience = tasks.process_audience()

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_token(
            token, google_requests.Request(), audience=audience,
        )
    except Exception as e:  # noqa: BLE001 — any verification failure is a 403
        logger.warning("%s: OIDC verification failed: %s: %s", what,
                       type(e).__name__, e)
        raise _FORBIDDEN from e

    email = claims.get("email")
    if not email or email != expected_sa:
        logger.warning("%s: token from unexpected principal %r (want %r)", what,
                       email, expected_sa)
        raise _FORBIDDEN
    if claims.get("email_verified") is not True:
        logger.warning("%s: token email not verified: %r", what, email)
        raise _FORBIDDEN

    logger.info("%s: authenticated call from %s", what, email)
    return claims


def require_cloud_task(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: accept ONLY a Cloud Tasks OIDC token. Else 403.

    /internal/process runs the agent loop, so it must not be reachable by an API
    key (a customer's credential has no business invoking internals) and must
    not be public. Its only caller is Cloud Tasks, presenting a token signed by
    the dedicated tasks service account.
    """
    import tasks

    return _verify_google_oidc(authorization, tasks.TASKS_SERVICE_ACCOUNT,
                               "internal/process")


def require_scheduler(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: accept ONLY a Cloud Scheduler OIDC token. Else 403.

    A DIFFERENT service account from the tasks one, and pinned separately: the
    scheduler may sweep, the task runner may process, and neither inherits the
    other's door. If they shared an identity, compromising the cron would hand
    over the loop as well.
    """
    return _verify_google_oidc(authorization, SCHEDULER_SERVICE_ACCOUNT,
                               "internal/sweep")


class RateLimitExceeded(Exception):
    """Raised by `charge_trigger`/`charge_triggers` when a key is over budget.

    `count` is what the counter would stand at had the charge been allowed, so
    a caller can always report it as "you are at N of LIMIT". `requested` and
    `used` are populated only by the batch path, which knows something the
    single path does not: how many were asked for, and how many were left. A
    batch refusal wants to say "you asked for 8 and had 3" rather than "you are
    over" — the caller's next move depends on the size of the gap.
    """

    def __init__(self, count: int, limit: int, *, requested: int | None = None,
                 used: int | None = None):
        self.count = count
        self.limit = limit
        self.requested = requested
        self.used = used
        super().__init__(f"{count}/{limit} triggers used this hour")

    @property
    def remaining(self) -> int:
        """How much budget was actually left. Batch path only; else 0."""
        if self.used is None:
            return 0
        return max(0, self.limit - self.used)


def charge_triggers(api_key_id: int, n: int, *, limit: int | None = None) -> int:
    """Charge N triggers against this key's hourly budget, ALL OR NOTHING.

    Returns the new count. Raises RateLimitExceeded — having charged NOTHING —
    when the key does not have N left this hour.

    THE ALL-OR-NOTHING PART IS THE WHOLE POINT. A batch of N charges N against
    the same per-key hourly limit a single trigger charges 1 against; there is
    no separate batch ceiling, because the limit exists to bound what one
    credential can spend and a batch budget of its own would be a way around it
    rather than a use of it. Given that, a partial charge would be the worst of
    both: the caller is refused, and their budget is gone anyway.

    WHY THIS IS NOT `charge_trigger` IN A LOOP. Two reasons, and the second is
    the one that matters. First, N round trips. Second, `charge_trigger`
    increments and THEN checks, so a loop that refused on the 4th of 8 would
    leave 4 charges spent on a batch that never ran — exactly the partial state
    this must not produce. Rolling them back afterwards is not available either:
    another request may have charged in between, so "subtract 4" is not the
    inverse of what we did.

    WHY SELECT ... FOR UPDATE RATHER THAN ONE CLEVER UPSERT. The single-trigger
    UPSERT can be atomic in one statement because it always applies; this one
    must decide whether to apply at all, and the decision needs the post-
    roll-over count. FOR UPDATE serialises concurrent charges on the same key,
    so two batches of 15 against a 20 budget cannot both read "0 used" and both
    proceed — the second blocks, then sees 15 and is refused. The whole thing
    is one transaction, so a refusal rolls back the seed INSERT too.

    The window is the same fixed hour as `charge_trigger` (date_trunc), with the
    same known looseness across a boundary, and deliberately the same counter
    row: singles and batches share one budget.
    """
    if n < 1:
        raise ValueError(f"cannot charge {n} triggers")

    limit = TRIGGER_RATE_LIMIT_PER_HOUR if limit is None else limit

    with transaction() as cur:
        # Ensure a row exists to lock. DO NOTHING rather than an upsert of the
        # count: this statement establishes the lock target, it does not charge.
        cur.execute(
            "INSERT INTO api_key_rate_limit (api_key_id, window_start, count) "
            "VALUES (%s, date_trunc('hour', now()), 0) "
            "ON CONFLICT (api_key_id) DO NOTHING",
            (api_key_id,),
        )
        cur.execute(
            "SELECT count, window_start < date_trunc('hour', now()) AS stale "
            "FROM api_key_rate_limit WHERE api_key_id = %s FOR UPDATE",
            (api_key_id,),
        )
        row = cur.fetchone()

        # A stale window is a spent budget that has already expired: treat it as
        # zero here and let the UPDATE below roll the window forward, which is
        # the same rule the single-trigger UPSERT applies in its CASE.
        used = 0 if row["stale"] else row["count"]

        if used + n > limit:
            logger.warning("rate limit: key %s asked for %d with %d/%d used "
                           "this hour; batch refused whole", api_key_id, n,
                           used, limit)
            raise RateLimitExceeded(used + n, limit, requested=n, used=used)

        cur.execute(
            "UPDATE api_key_rate_limit "
            "SET count = %s, window_start = date_trunc('hour', now()) "
            "WHERE api_key_id = %s",
            (used + n, api_key_id),
        )

    return used + n


def charge_trigger(api_key_id: int, *, limit: int | None = None) -> int:
    """Count one trigger against this key's hourly budget. Returns the new count.

    `limit=None` reads TRIGGER_RATE_LIMIT_PER_HOUR at CALL time, not at import
    time. Writing `limit: int = TRIGGER_RATE_LIMIT_PER_HOUR` in the signature
    would evaluate the constant once when this module is first imported and bake
    the value into the function object forever — so changing the constant later
    (a config reload, a test, an operator raising the ceiling during an
    incident) would silently do nothing. A ceiling that cannot be changed
    without a redeploy is not much of a control.

    Raises RateLimitExceeded when the key is over. One atomic UPSERT does the
    increment AND the window roll-over, so two concurrent triggers cannot both
    read "19" and both write "20": the second one sees 21 and is refused. A
    read-then-write here would be a race that lets the limit be exceeded exactly
    when it matters (a script hammering the endpoint).

    The window is a fixed hour (date_trunc), not a sliding one — a caller can
    therefore spend 2x the limit across a window boundary. That is a known and
    acceptable looseness: this is a cost ceiling, not a security control, and
    the simpler statement is easier to reason about than a sliding window that
    still approximates.
    """
    limit = TRIGGER_RATE_LIMIT_PER_HOUR if limit is None else limit
    rows = execute(
        "INSERT INTO api_key_rate_limit (api_key_id, window_start, count) "
        "VALUES (%s, date_trunc('hour', now()), 1) "
        "ON CONFLICT (api_key_id) DO UPDATE SET "
        "  count = CASE "
        "      WHEN api_key_rate_limit.window_start < date_trunc('hour', now()) "
        "      THEN 1 ELSE api_key_rate_limit.count + 1 END, "
        "  window_start = CASE "
        "      WHEN api_key_rate_limit.window_start < date_trunc('hour', now()) "
        "      THEN date_trunc('hour', now()) "
        "      ELSE api_key_rate_limit.window_start END "
        "RETURNING count",
        (api_key_id,),
    )
    count = rows[0]["count"]
    if count > limit:
        logger.warning("rate limit: key %s at %d/%d this hour", api_key_id,
                       count, limit)
        raise RateLimitExceeded(count, limit)
    return count
