"""Outbound events: signed, retried, org-scoped — and pointed only outward.

BRAINS takes untrusted text IN (a lead form) and now sends signed payloads OUT
to a URL a customer typed. Those are two different trust problems and this module
is the second one.

WHAT A CUSTOMER-SUPPLIED URL IS
-------------------------------
It is a request our server makes, with our server's network position, to an
address we did not choose. That is Server-Side Request Forgery in its textbook
form, and on GCP the payoff is concrete rather than theoretical:

    http://169.254.169.254/computeMetadata/v1/instance/service-accounts/
        default/token

is the metadata server. It answers from inside the instance, with no credential
of its own, and it hands back an OAuth token for the runtime service account —
the identity that reaches Cloud SQL, Secret Manager and Cloud Tasks. A webhook
endpoint of `http://169.254.169.254/...` would have us fetch that token and POST
it, HMAC-signed and neatly formatted, to whoever asked. The same shape reaches
`10.x` VPC peers, `127.0.0.1` (our own /internal/* handlers, from a source
address that is definitionally local), and the whole of RFC1918.

So `assert_public_url` runs before EVERY delivery, and it fails closed: an
address family it does not recognise is refused, not allowed.

WHAT THIS GUARD DOES NOT COVER — the honest part
------------------------------------------------
It resolves the hostname and validates the resulting addresses, then hands the
URL to httpx, which resolves it AGAIN when it connects. Between those two
resolutions a hostile DNS server can change its answer: validate a public IP,
connect to 169.254.169.254. That is DNS rebinding, and it is a real TOCTOU
window, not a hypothetical one.

Closing it properly means connecting to the validated IP directly and carrying
the hostname in the Host header and the TLS SNI — doable, and the right move if
this system ever serves untrusted tenants at scale. It is not done here, and
saying so is better than implying a completeness this does not have. What the
current guard does buy is that the OBVIOUS attack — a customer pasting the
metadata URL into the endpoint form, deliberately or because someone told them
to — is refused every time, at every attempt, including retries.

Two other things narrow the window. The check runs per ATTEMPT rather than at
registration, so an endpoint that resolved publicly on Monday and points at
169.254 on Tuesday is refused on Tuesday. And HTTPS is required off-localhost,
which means a rebind has to survive certificate validation for the original
hostname as well.

SIGNING
-------
    X-Brains-Signature = hex(HMAC-SHA256(secret, timestamp + "." + body))

The timestamp is INSIDE the signed string, not merely alongside it. Signing the
body alone produces a token that stays valid forever: anyone who captures one
delivery can replay it at any later time and the signature still verifies,
because nothing in the signed material says when it was made. With the timestamp
bound in, a receiver that rejects old timestamps gets replay protection it can
actually enforce — the attacker cannot advance the clock without invalidating
the MAC, and cannot keep the MAC without keeping the stale timestamp.

`hmac.compare_digest` is used on the verify path for the usual reason: `==` on
bytes short-circuits at the first differing byte, which leaks the length of the
correct prefix to anyone who can time it.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import uuid
from urllib.parse import urlparse

from psycopg.types.json import Json

import config  # noqa: F401  — loads .env before anything reads os.environ
from db import execute, query

logger = logging.getLogger("brains-webhooks")
if not logger.handlers:
    _h = logging.StreamHandler()  # stderr
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# --- The event vocabulary -----------------------------------------------------
#
# Exactly the decision transitions a human or a downstream automation would act
# on. A closed set, validated at registration, so a typo'd subscription is a 422
# at CRUD time rather than an endpoint that silently never fires.

EVENT_PROPOSED = "proposed"                # gate says a human must approve
EVENT_AUTO_EXECUTED = "auto_executed"      # gate acted, no human
EVENT_AUTO_DISCARDED = "auto_discarded"    # gate discarded, no human
EVENT_APPROVED = "approved"                # a human approved
EVENT_REJECTED = "rejected"                # a human rejected
EVENT_NEEDS_REVIEW = "needs_review"        # the run produced nothing trustworthy

KNOWN_EVENTS = (
    EVENT_PROPOSED, EVENT_AUTO_EXECUTED, EVENT_AUTO_DISCARDED,
    EVENT_APPROVED, EVENT_REJECTED, EVENT_NEEDS_REVIEW,
)

#: Subscribe to everything, including events added later.
EVENT_WILDCARD = "*"

#: decisions.status -> the event name it emits. 'pending_approval' is published
#: as 'proposed' because that is what it means to a receiver: the agent produced
#: a proposal and it is waiting on a human. The internal status name is a
#: database detail; the event name is a public contract.
STATUS_TO_EVENT = {
    "pending_approval": EVENT_PROPOSED,
    "auto_executed": EVENT_AUTO_EXECUTED,
    "auto_discarded": EVENT_AUTO_DISCARDED,
    "approved": EVENT_APPROVED,
    "rejected": EVENT_REJECTED,
    "needs_review": EVENT_NEEDS_REVIEW,
}

SECRET_PREFIX = "whsec_"
SECRET_BYTES = 32  # 256 bits, same budget as an API key

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"

#: How long a single delivery attempt may take. Short on purpose: a receiver
#: that hangs must not hold a worker, and the decision is already committed.
DELIVERY_TIMEOUT_SECONDS = 10


class SSRFRefused(Exception):
    """A delivery target resolved somewhere it must not reach."""


# --- The SSRF guard -----------------------------------------------------------

def _address_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address is off-limits, or None if it is a public one.

    Ordered so the most specific and most dangerous answer is the one reported:
    169.254.169.254 is link-local, but "link-local" is a weaker thing to read in
    a log than "the metadata server".
    """
    # The GCP/AWS/Azure metadata address, named explicitly. It is inside the
    # link-local check below and would be refused either way; it is called out
    # so the refusal says what was actually attempted.
    if ip in ipaddress.ip_network("169.254.169.254/32"):
        return "cloud metadata server (169.254.169.254) — holds service account tokens"
    if ip.is_link_local:            # 169.254.0.0/16, fe80::/10
        return "link-local address"
    if ip.is_loopback:              # 127.0.0.0/8, ::1
        return "loopback address"
    if ip.is_private:               # RFC1918 10/8 172.16/12 192.168/16, fc00::/7
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:           # 0.0.0.0, ::
        return "unspecified address"
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) would otherwise sail past every check
    # above, because the flags on the v6 object describe the v6 space.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        inner = _address_is_forbidden(ip.ipv4_mapped)
        if inner:
            return f"IPv4-mapped {inner}"
    return None


def _resolve(host: str) -> list[str]:
    """Every address `host` resolves to. Separated out so tests can mock it.

    ALL results are checked, not just the first: a hostname that returns one
    public address and one 169.254 address must be refused, and which one comes
    back first is not ours to depend on.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def assert_public_url(url: str, *, allow_localhost: bool = False,
                      resolve: bool = True) -> list[str]:
    """Refuse anything that is not a public HTTPS endpoint. Returns the IPs.

    Called before EVERY delivery attempt rather than once at registration:
    DNS is mutable, and an endpoint that was public when it was registered is
    not thereby public forever.

    `allow_localhost` is the local/test escape hatch and is driven by
    tasks.emulated() — an explicit opt-in that a deployed service cannot set
    (K_SERVICE overrides it). It is not inferred from the absence of config.

    `resolve=False` checks only what can be read off the URL itself: the scheme,
    the HTTPS requirement, and the address when the host is written as a literal
    IP. It exists for the REGISTRATION check, where resolving would be actively
    wrong on two counts. It would make endpoint registration a way to ask our
    server to look up arbitrary names — an oracle handed to the caller — and it
    would imply a durability the answer does not have, since what a name
    resolves to at registration says nothing about what it resolves to an hour
    later. Registration validates the literal; delivery validates the address.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    # Scheme first: file:///etc/passwd has no host either, and "not http(s)" is
    # the more useful thing to tell someone than "no host".
    if scheme not in ("http", "https"):
        raise SSRFRefused(
            f"webhook url scheme {scheme!r} is not http(s) — refusing {url!r}"
        )

    host = parsed.hostname
    if not host:
        raise SSRFRefused(f"no host in webhook url {url!r}")

    is_local_name = host in ("localhost", "127.0.0.1", "::1")

    # Plaintext leaks the payload AND the signature to anyone on the path. The
    # only exception is a local receiver in emulated mode, where there is no
    # path to be on and no certificate to be had.
    if scheme != "https" and not (allow_localhost and is_local_name):
        raise SSRFRefused(
            f"webhook url must be https (got {scheme!r} for host {host!r}); "
            "plaintext is permitted only for localhost in emulated mode"
        )

    if allow_localhost and is_local_name:
        return [host]

    # A host written as a bare IP needs no resolver, and must be checked even
    # when we are not resolving — otherwise `resolve=False` would wave through
    # exactly the URL the registration check exists to catch:
    # http://169.254.169.254/computeMetadata/v1/.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        reason = _address_is_forbidden(literal)
        if reason is not None:
            raise SSRFRefused(
                f"webhook url points directly at {host} — {reason}. A "
                "customer-supplied URL may not point this server at its own "
                "infrastructure."
            )
        return [host]

    if not resolve:
        return []

    try:
        addresses = _resolve(host)
    except socket.gaierror as e:
        # Fail closed: an address we cannot resolve is an address we cannot
        # vouch for. This also stops an attacker from using a deliberately
        # unresolvable name to skip the check.
        raise SSRFRefused(f"could not resolve webhook host {host!r}: {e}") from e

    if not addresses:
        raise SSRFRefused(f"webhook host {host!r} resolved to no addresses")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as e:
            raise SSRFRefused(
                f"webhook host {host!r} resolved to an unparseable address "
                f"{raw!r}"
            ) from e
        reason = _address_is_forbidden(ip)
        if reason is not None:
            raise SSRFRefused(
                f"webhook host {host!r} resolves to {raw} — {reason}. A "
                "customer-supplied URL may not point this server at its own "
                "infrastructure."
            )

    return addresses


# --- Signing ------------------------------------------------------------------

def sign(secret: str, timestamp: str, body: bytes) -> str:
    """hex(HMAC-SHA256(secret, timestamp + "." + body)).

    The signed material is `timestamp.body`, so the timestamp cannot be edited
    without breaking the MAC. See the module docstring on replay.
    """
    signed_payload = timestamp.encode("utf-8") + b"." + body
    return hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Constant-time check of a delivery signature. What a receiver runs."""
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)


def generate_secret() -> str:
    import secrets

    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)


# --- Endpoint registry (all org-scoped) ---------------------------------------

def create_endpoint(*, org_id: int, url: str, events: list[str]) -> dict:
    """Register an endpoint. Returns the row INCLUDING the secret, once."""
    secret = generate_secret()
    rows = execute(
        "INSERT INTO webhook_endpoints (org_id, url, secret, events) "
        "VALUES (%s, %s, %s, %s) "
        "RETURNING id, org_id, url, events, active, created_at",
        (org_id, url, secret, events),
    )
    row = rows[0]
    row["secret"] = secret
    logger.info("org %s registered webhook endpoint %s for %s",
                org_id, row["id"], events)
    return row


def list_endpoints(*, org_id: int) -> list[dict]:
    """This org's endpoints. The secret is NOT selected — see the schema note."""
    return query(
        "SELECT id, org_id, url, events, active, created_at "
        "FROM webhook_endpoints WHERE org_id = %s ORDER BY id",
        (org_id,),
    )


def get_endpoint(endpoint_id: int, *, org_id: int) -> dict | None:
    rows = query(
        "SELECT id, org_id, url, events, active, created_at "
        "FROM webhook_endpoints WHERE id = %s AND org_id = %s",
        (endpoint_id, org_id),
    )
    return rows[0] if rows else None


def update_endpoint(endpoint_id: int, *, org_id: int, url: str | None = None,
                    events: list[str] | None = None,
                    active: bool | None = None) -> dict | None:
    """Patch an endpoint. org_id is in the WHERE, so cross-org edits find nothing.

    COALESCE lets one statement handle any subset of fields: a NULL parameter
    means "leave it alone" rather than "set it to NULL". One UPDATE, no
    read-modify-write, so two concurrent patches cannot lose each other's field.
    """
    rows = execute(
        "UPDATE webhook_endpoints SET "
        "  url = COALESCE(%s, url), "
        "  events = COALESCE(%s, events), "
        "  active = COALESCE(%s, active) "
        "WHERE id = %s AND org_id = %s "
        "RETURNING id, org_id, url, events, active, created_at",
        (url, events, active, endpoint_id, org_id),
    )
    return rows[0] if rows else None


def delete_endpoint(endpoint_id: int, *, org_id: int) -> bool:
    result = execute(
        "DELETE FROM webhook_endpoints WHERE id = %s AND org_id = %s",
        (endpoint_id, org_id),
    )
    return bool(result)


def endpoints_for_event(*, org_id: int, event: str) -> list[dict]:
    """Active endpoints in this org subscribed to this event.

    The subscription match is in SQL (`&&` — array overlap) rather than in
    Python, for the same reason the knowledge-base permission filter is: the
    rows that do not match are never fetched.
    """
    return query(
        "SELECT id, org_id, url, secret, events FROM webhook_endpoints "
        "WHERE org_id = %s AND active = TRUE AND events && %s::text[] "
        "ORDER BY id",
        (org_id, [event, EVENT_WILDCARD]),
    )


# --- Delivery records ---------------------------------------------------------

def record_delivery(*, org_id: int, endpoint_id: int, decision_id: int | None,
                    event: str) -> dict:
    """Create the 'pending' row. Written BEFORE the POST is attempted.

    So a delivery that dies mid-flight — the worker evicted, the process killed —
    still left evidence that it was owed. "Did the webhook fire?" has to be
    answerable when the answer is no.
    """
    rows = execute(
        "INSERT INTO webhook_deliveries "
        "(org_id, endpoint_id, decision_id, event, delivery_uuid, status) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "RETURNING id, delivery_uuid",
        (org_id, endpoint_id, decision_id, event, str(uuid.uuid4()),
         DELIVERY_PENDING),
    )
    return rows[0]


def finish_delivery(delivery_id: int, *, status: str, attempts: int,
                    status_code: int | None = None,
                    error: str | None = None) -> None:
    execute(
        "UPDATE webhook_deliveries SET status = %s, attempts = %s, "
        "  status_code = %s, error = %s, "
        "  delivered_at = CASE WHEN %s = %s THEN now() ELSE delivered_at END "
        "WHERE id = %s",
        (status, attempts, status_code, error, status, DELIVERY_DELIVERED,
         delivery_id),
    )


def deliveries_for_decision(decision_id: int, *, org_id: int) -> list[dict]:
    return query(
        "SELECT id, endpoint_id, event, status, attempts, status_code, error, "
        "       delivery_uuid, created_at, delivered_at "
        "FROM webhook_deliveries WHERE decision_id = %s AND org_id = %s "
        "ORDER BY id",
        (decision_id, org_id),
    )


# --- The payload --------------------------------------------------------------

def decision_payload(decision_id: int, *, org_id: int) -> dict | None:
    """The full decision record, reasoning included.

    Reasoning is the point: a receiver that only learns the status has to come
    back and ask why. The whole argument of this system is that the trail
    travels with the decision.
    """
    rows = query(
        "SELECT id, org_id, lead_id, trigger_input, proposed_action, score, "
        "       band, rules_version, reasoning, status, decided_by, "
        "       created_at, decided_at "
        "FROM decisions WHERE id = %s AND org_id = %s",
        (decision_id, org_id),
    )
    if not rows:
        return None
    r = dict(rows[0])
    for field in ("created_at", "decided_at"):
        if r.get(field) is not None:
            r[field] = r[field].isoformat()
    return r


def build_body(*, event: str, delivery_uuid: str, decision: dict) -> bytes:
    """Serialise once, sign the exact bytes we send.

    `sort_keys` and a fixed separator are not cosmetic: the signature covers
    these bytes, so anything that re-serialises the payload between signing and
    sending would invalidate it. Signing the serialised form and sending that
    same object is the only version of this that is not a latent bug.
    """
    return json.dumps(
        {
            "event": event,
            "delivery_id": delivery_uuid,
            "decision": decision,
        },
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


# --- Emission: the decision side ---------------------------------------------

class Retryable(Exception):
    """A delivery failure that another attempt might survive.

    Distinguished from a permanent one (an SSRF refusal, a deactivated endpoint,
    a decision that no longer exists) because only this kind is worth asking
    Cloud Tasks to try again. A permanent failure that raised would burn five
    attempts to arrive at the same refusal.
    """


def emit(event: str, *, decision_id: int, org_id: int) -> list[int]:
    """Fan a decision transition out to this org's subscribed endpoints.

    NEVER RAISES. This is called from inside the decision write path, and a
    decision that succeeded must not be reported as failed because a customer's
    webhook registry was unreachable or their URL was nonsense. The decision is
    the product; the notification is a consequence of it. Every failure here is
    logged and, where a row exists, recorded — but it is swallowed, because the
    alternative is letting an integration take down qualification.

    That also closes a re-entrancy trap. `decisions.needs_review` is called from
    the handler that runs when ENQUEUEING ITSELF failed — so if emitting from
    that path raised, or worse re-entered needs_review, a broken queue would
    turn one parked row into a loop. Emission swallows, and never calls back
    into the decision module.

    Returns the delivery ids created, for tests and for the caller's log.
    """
    try:
        endpoints = endpoints_for_event(org_id=org_id, event=event)
    except Exception:  # noqa: BLE001 — the decision is already committed
        logger.exception("could not read webhook endpoints for org %s; "
                         "decision %s is unaffected", org_id, decision_id)
        return []

    if not endpoints:
        return []

    delivery_ids = []
    for endpoint in endpoints:
        try:
            record = record_delivery(
                org_id=org_id, endpoint_id=endpoint["id"],
                decision_id=decision_id, event=event,
            )
            delivery_ids.append(record["id"])
        except Exception:  # noqa: BLE001
            logger.exception("could not record delivery for endpoint %s",
                             endpoint["id"])
            continue

        # Enqueue AFTER the row exists, same ordering as trigger/decisions: a
        # task with no row would deliver something we have no record of owing.
        # If the enqueue fails, the row is closed as failed rather than left
        # 'pending' forever with nothing to move it — the identical orphan
        # problem the trigger path solves, and the same answer.
        try:
            import tasks

            tasks.enqueue_delivery(record["id"], org_id=org_id)
        except Exception as e:  # noqa: BLE001 — never propagate into a decision
            logger.exception("could not enqueue delivery %s", record["id"])
            try:
                finish_delivery(
                    record["id"], status=DELIVERY_FAILED, attempts=0,
                    error=f"could not enqueue: {type(e).__name__}: {e}",
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not even record the failed enqueue")

    return delivery_ids


def emit_for_status(status: str, *, decision_id: int, org_id: int) -> list[int]:
    """Emit whatever event a decisions.status corresponds to, if any.

    Statuses with no event — 'processing', 'dismissed' — return [] rather than
    raising, so callers can hand this any status without knowing the map.
    """
    event = STATUS_TO_EVENT.get(status)
    if event is None:
        return []
    return emit(event, decision_id=decision_id, org_id=org_id)


# --- Delivery: the HTTP side --------------------------------------------------

def deliver(delivery_id: int, *, attempt: int = 0,
            is_final_attempt: bool = False) -> dict:
    """Perform one delivery attempt. Raises _Undeliverable only when retrying is futile.

    Called from /internal/deliver (Cloud Tasks, with retries) and from the
    in-process thread locally. The contract mirrors /internal/process: a
    RETRYABLE failure raises so the caller can return 5xx and let Cloud Tasks
    back off; on the last attempt nothing raises, because a 5xx there would
    just burn a retry to reach the same place. Either way the row ends in a
    terminal state — 'pending' is not somewhere a delivery may come to rest,
    for the same reason 'processing' is not somewhere a decision may.
    """
    rows = query(
        "SELECT d.id, d.org_id, d.decision_id, d.event, d.delivery_uuid, "
        "       d.status, e.url, e.secret, e.active "
        "FROM webhook_deliveries d "
        "JOIN webhook_endpoints e ON e.id = d.endpoint_id "
        "WHERE d.id = %s",
        (delivery_id,),
    )
    if not rows:
        logger.warning("delivery %s no longer exists", delivery_id)
        return {"status": "gone"}

    d = rows[0]

    # A retry of an attempt that actually landed must not send it twice. Cloud
    # Tasks guarantees at-least-once, so this check is what turns that into
    # roughly-once; the X-Brains-Delivery uuid is what lets the receiver close
    # the remaining gap on its own side.
    if d["status"] == DELIVERY_DELIVERED:
        return {"status": DELIVERY_DELIVERED, "already": True}

    if not d["active"]:
        finish_delivery(delivery_id, status=DELIVERY_FAILED, attempts=attempt + 1,
                        error="endpoint was deactivated before delivery")
        return {"status": DELIVERY_FAILED, "reason": "inactive"}

    decision = decision_payload(d["decision_id"], org_id=d["org_id"])
    if decision is None:
        finish_delivery(delivery_id, status=DELIVERY_FAILED, attempts=attempt + 1,
                        error=f"decision {d['decision_id']} not found in org "
                              f"{d['org_id']}")
        return {"status": DELIVERY_FAILED, "reason": "no decision"}

    body = build_body(event=d["event"], delivery_uuid=d["delivery_uuid"],
                      decision=decision)
    timestamp = str(int(_now()))
    signature = sign(d["secret"], timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "BRAINS-Webhook/1.0",
        "X-Brains-Event": d["event"],
        "X-Brains-Delivery": d["delivery_uuid"],
        "X-Brains-Timestamp": timestamp,
        "X-Brains-Signature": signature,
    }

    # THE GUARD. Before every attempt, not once at registration — see the module
    # docstring. A refusal is PERMANENT: the URL is what it is, and retrying it
    # four more times only repeats the same refusal while pretending the
    # customer's configuration might fix itself.
    import tasks

    try:
        assert_public_url(d["url"], allow_localhost=tasks.emulated())
    except SSRFRefused as e:
        logger.error("REFUSED delivery %s to %s: %s", delivery_id, d["url"], e)
        finish_delivery(delivery_id, status=DELIVERY_FAILED,
                        attempts=attempt + 1, error=f"ssrf guard: {e}")
        return {"status": DELIVERY_FAILED, "reason": "ssrf_refused",
                "error": str(e)}

    try:
        import httpx

        response = httpx.post(d["url"], content=body, headers=headers,
                              timeout=DELIVERY_TIMEOUT_SECONDS,
                              follow_redirects=False)
    except Exception as e:  # noqa: BLE001 — connection errors are retryable
        error = f"{type(e).__name__}: {e}"
        if is_final_attempt:
            finish_delivery(delivery_id, status=DELIVERY_FAILED,
                            attempts=attempt + 1, error=error)
            logger.warning("delivery %s exhausted retries: %s", delivery_id, error)
            return {"status": DELIVERY_FAILED, "reason": "exhausted",
                    "error": error}
        finish_delivery(delivery_id, status=DELIVERY_PENDING,
                        attempts=attempt + 1, error=error)
        raise Retryable(error) from e

    if 200 <= response.status_code < 300:
        finish_delivery(delivery_id, status=DELIVERY_DELIVERED,
                        attempts=attempt + 1, status_code=response.status_code)
        logger.info("delivery %s -> %s %s", delivery_id, d["url"],
                    response.status_code)
        return {"status": DELIVERY_DELIVERED,
                "status_code": response.status_code}

    error = f"receiver returned HTTP {response.status_code}"
    if is_final_attempt:
        finish_delivery(delivery_id, status=DELIVERY_FAILED,
                        attempts=attempt + 1,
                        status_code=response.status_code, error=error)
        logger.warning("delivery %s exhausted retries: %s", delivery_id, error)
        return {"status": DELIVERY_FAILED, "reason": "exhausted", "error": error}

    finish_delivery(delivery_id, status=DELIVERY_PENDING, attempts=attempt + 1,
                    status_code=response.status_code, error=error)
    raise Retryable(error)


def _now() -> float:
    """Wall clock, in one place so tests can pin it."""
    import time

    return time.time()
