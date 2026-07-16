"""Auth: the credential is the identity, and it is the ONLY source of identity.

The chain these tests pin down:

    X-API-Key -> sha256 -> api_keys.(org_id, role) -> build_mcp -> SQL

Earlier phases proved the MODEL cannot choose its role or tenant. This file
proves the CALLER cannot either: org_id is not validated-if-supplied, it is
absent from every request shape, so there is nowhere to supply it.
"""

import pytest
from fastapi.testclient import TestClient

import auth
import decisions
from db import execute, query


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")
pytestmark = needs_db


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


@pytest.fixture
def key_factory():
    """Mint throwaway keys; revoke-proof cleanup afterwards."""
    made = []

    def make(*, org_id=1, role="sales", name=None):
        name = name or f"test key org{org_id} {role} {len(made)}"
        raw, key_id = auth.create_key(org_id=org_id, role=role, name=name)
        made.append(key_id)
        return raw, key_id

    yield make
    for key_id in made:
        execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (key_id,))
        execute("DELETE FROM api_keys WHERE id = %s", (key_id,))


@pytest.fixture
def decision_factory():
    made = []

    def make(org_id=1, status="pending_approval"):
        did = decisions.create_processing(
            org_id=org_id, trigger_input={"email": "x@test.invalid"},
            identity={"role": "sales", "org_id": org_id,
                      "bound_at": "loop_construction"},
        )
        execute("UPDATE decisions SET status = %s, proposed_action = 'nurture' "
                "WHERE id = %s", (status, did))
        made.append(did)
        return did

    yield make
    for did in made:
        execute("DELETE FROM decisions WHERE id = %s", (did,))


# --- The key itself --------------------------------------------------------- #

def test_the_raw_key_is_never_stored(key_factory):
    """A dump of api_keys must not be replayable against the API."""
    raw, key_id = key_factory()
    row = query("SELECT key_hash FROM api_keys WHERE id = %s", (key_id,))[0]

    assert row["key_hash"] != raw
    assert raw not in row["key_hash"]
    assert row["key_hash"] == auth.hash_key(raw)
    assert len(row["key_hash"]) == 64, "sha256 hex"

    # And nothing anywhere in the row looks like a usable key.
    full = query("SELECT * FROM api_keys WHERE id = %s", (key_id,))[0]
    assert not any(
        isinstance(v, str) and v.startswith(auth.KEY_PREFIX) for v in full.values()
    )


def test_keys_are_unguessable_and_unique():
    keys = {auth.generate_key() for _ in range(200)}
    assert len(keys) == 200, "no collisions"
    assert all(k.startswith(auth.KEY_PREFIX) for k in keys)
    assert all(len(k) > 40 for k in keys), "256 bits of entropy, urlsafe-encoded"


def test_resolve_returns_the_bound_identity(key_factory):
    raw, key_id = key_factory(org_id=2, role="admin")
    p = auth.resolve(raw)
    assert p.api_key_id == key_id and p.org_id == 2 and p.role == "admin"


def test_unknown_key_resolves_to_nothing():
    assert auth.resolve("brains_sk_totally-made-up") is None
    assert auth.resolve("") is None


def test_revoked_key_stops_resolving_but_survives_for_audit(key_factory):
    raw, key_id = key_factory()
    assert auth.resolve(raw) is not None

    assert auth.revoke_key(key_id) is True
    assert auth.resolve(raw) is None, "a revoked key must authenticate nothing"
    assert auth.revoke_key(key_id) is False, "revocation is idempotent"

    # Still on file: "which credential triggered decision 41?" must stay answerable.
    assert query("SELECT id FROM api_keys WHERE id = %s", (key_id,))


def test_create_key_refuses_an_unknown_role():
    with pytest.raises(ValueError, match="unknown role"):
        auth.create_key(org_id=1, role="superuser", name="nope")


# --- 401s ------------------------------------------------------------------- #

def test_missing_key_401s(client):
    r = client.get("/decisions/pending")
    assert r.status_code == 401


def test_unknown_key_401s(client):
    r = client.get("/decisions/pending", headers={"X-API-Key": "brains_sk_nope"})
    assert r.status_code == 401


def test_revoked_key_401s(client, key_factory):
    raw, key_id = key_factory()
    assert client.get("/decisions/pending", headers={"X-API-Key": raw}).status_code == 200

    auth.revoke_key(key_id)
    assert client.get("/decisions/pending", headers={"X-API-Key": raw}).status_code == 401


def test_all_three_401s_are_indistinguishable(client, key_factory):
    """Missing / unknown / revoked must look identical to the caller.

    Telling them apart tells an attacker which guess was once real. The remedy
    is the same in all three cases, so the detail only helps the wrong audience.
    """
    raw, key_id = key_factory()
    auth.revoke_key(raw and key_id)

    bodies = {
        client.get("/decisions/pending").json()["detail"],
        client.get("/decisions/pending",
                   headers={"X-API-Key": "brains_sk_nope"}).json()["detail"],
        client.get("/decisions/pending", headers={"X-API-Key": raw}).json()["detail"],
    }
    assert len(bodies) == 1, f"401 bodies leak which failure it was: {bodies}"


@pytest.mark.parametrize("method,path", [
    ("post", "/decisions/trigger"),
    ("get", "/decisions/pending"),
    ("get", "/decisions/1"),
    ("post", "/decisions/1/approve"),
    ("post", "/decisions/1/reject"),
    ("post", "/decisions/1/retry"),
    ("post", "/decisions/1/dismiss"),
])
def test_every_endpoint_requires_a_credential(client, method, path):
    """No route may be reachable unauthenticated.

    401 must win over 422: auth is checked before the body is validated, so a
    caller cannot probe request shapes without a credential.
    """
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 401, f"{method.upper()} {path} is not authenticated"


def test_the_route_list_has_no_unauthenticated_endpoint():
    """Structural: EVERY route sits behind one of the two doors.

    The parametrized test above only covers routes someone remembered to list.
    This one fails when a NEW endpoint is added with no auth at all.

    Three doors, deliberately distinct:
      - require_principal  — customer API keys (org_id + role come from the key)
      - require_cloud_task — Cloud Tasks OIDC only; not public, not API-key'd
      - require_scheduler  — Cloud Scheduler OIDC only; a DIFFERENT service
                             account from the tasks one, so compromising the
                             cron does not hand over the loop
    A route must be behind exactly one. A customer credential must not reach the
    internals, and neither Cloud Tasks nor Scheduler has an API key.
    """
    from api.main import app
    from auth import require_cloud_task, require_principal, require_scheduler

    doors = {require_principal, require_cloud_task, require_scheduler}
    exempt = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None or route.path in exempt:
            continue
        calls = {d.call for d in dependant.dependencies}
        guards = calls & doors
        assert guards, f"{route.path} has NO authentication dependency"
        assert len(guards) == 1, (
            f"{route.path} mixes auth mechanisms: {guards}"
        )


def test_tasks_and_scheduler_have_separate_identities():
    """The cron and the task runner must not share a service account.

    If they did, whichever is easier to reach would grant the other's power.
    """
    import auth
    import tasks

    # Both read from env; in a deployed service they are two different SAs.
    assert auth.require_scheduler is not auth.require_cloud_task
    src_task = tasks.TASKS_SERVICE_ACCOUNT
    src_sched = auth.SCHEDULER_SERVICE_ACCOUNT
    if src_task and src_sched:
        assert src_task != src_sched, "tasks and scheduler share an identity"


def test_the_internal_endpoint_is_not_reachable_with_an_api_key():
    """/internal/process must be Cloud-Tasks-only — a customer key is not a key to it."""
    from api.main import app
    from auth import require_cloud_task, require_principal

    internal = [r for r in app.routes
                if getattr(r, "path", None) == "/internal/process"]
    assert internal, "the internal endpoint is missing"
    calls = {d.call for d in internal[0].dependant.dependencies}
    assert require_cloud_task in calls
    assert require_principal not in calls, (
        "the internal endpoint must not accept a customer API key"
    )


def test_internal_process_rejects_a_forged_token(monkeypatch):
    """With tasks configured (i.e. deployed), a bad token is a 403.

    The emulated path skips verification, so this test switches emulation OFF to
    exercise the real check — otherwise it would prove nothing about production.
    """
    import tasks
    from api.main import app

    monkeypatch.setattr(tasks, "emulated", lambda: False)
    monkeypatch.setattr(tasks, "TASKS_SERVICE_ACCOUNT", "tasks@example.iam.gserviceaccount.com")
    monkeypatch.setattr(tasks, "process_audience", lambda: "https://svc.example.com")

    c = TestClient(app)
    body = {"decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"}

    # No token at all.
    assert c.post("/internal/process", json=body).status_code == 403
    # A token that is not a Google-signed OIDC token.
    assert c.post("/internal/process", json=body,
                  headers={"Authorization": "Bearer not-a-real-token"}).status_code == 403
    # An API key is not a substitute.
    assert c.post("/internal/process", json=body,
                  headers={"X-API-Key": "brains_sk_whatever"}).status_code == 403


def test_internal_process_rejects_a_token_from_the_wrong_service_account(monkeypatch):
    """Audience alone is not enough — the issuer must be OUR tasks SA.

    Any Google-issued token minted for our URL passes signature+audience. The
    email pin is what distinguishes Cloud Tasks from any other caller who knows
    our address.
    """
    import tasks
    from api.main import app

    monkeypatch.setattr(tasks, "emulated", lambda: False)
    monkeypatch.setattr(tasks, "TASKS_SERVICE_ACCOUNT", "tasks@ours.iam.gserviceaccount.com")
    monkeypatch.setattr(tasks, "process_audience", lambda: "https://svc.example.com")

    # A token that verifies cleanly but belongs to someone else.
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_token", lambda *a, **k: {
        "email": "attacker@evil.iam.gserviceaccount.com", "email_verified": True,
    })

    c = TestClient(app)
    r = c.post("/internal/process",
               json={"decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"},
               headers={"Authorization": "Bearer valid-but-wrong-sa"})
    assert r.status_code == 403, "a token from another SA must not be accepted"


def test_internal_process_accepts_our_tasks_service_account(monkeypatch):
    """The positive case — else the tests above pass vacuously."""
    import decisions
    import tasks
    from api.main import app

    monkeypatch.setattr(tasks, "emulated", lambda: False)
    monkeypatch.setattr(tasks, "TASKS_SERVICE_ACCOUNT", "tasks@ours.iam.gserviceaccount.com")
    monkeypatch.setattr(tasks, "process_audience", lambda: "https://svc.example.com")
    monkeypatch.setattr(decisions, "process",
                        lambda *a, **k: {"id": 1, "status": "pending_approval"})

    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_token", lambda *a, **k: {
        "email": "tasks@ours.iam.gserviceaccount.com", "email_verified": True,
    })

    c = TestClient(app)
    r = c.post("/internal/process",
               json={"decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"},
               headers={"Authorization": "Bearer good"})
    assert r.status_code == 200, r.text


def test_audience_is_passed_to_the_verifier(monkeypatch):
    """google-auth SKIPS audience validation when audience=None.

    The Cloud Run doc's own sample omits it. Without it, a token minted for
    someone else's service verifies here. This pins that we pass it.
    """
    import tasks
    from api.main import app

    monkeypatch.setattr(tasks, "emulated", lambda: False)
    monkeypatch.setattr(tasks, "TASKS_SERVICE_ACCOUNT", "tasks@ours.iam.gserviceaccount.com")
    monkeypatch.setattr(tasks, "process_audience", lambda: "https://svc.example.com")

    seen = {}

    from google.oauth2 import id_token

    def spy(token, request, audience=None, **kw):
        seen["audience"] = audience
        return {"email": "tasks@ours.iam.gserviceaccount.com", "email_verified": True}

    monkeypatch.setattr(id_token, "verify_token", spy)
    import decisions

    monkeypatch.setattr(decisions, "process", lambda *a, **k: {"status": "x"})

    TestClient(app).post(
        "/internal/process",
        json={"decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"},
        headers={"Authorization": "Bearer good"})

    assert seen.get("audience") == "https://svc.example.com", (
        "audience must be passed explicitly or verification is skipped"
    )


# --- org_id is not caller input any more ------------------------------------ #

def test_no_request_model_has_an_org_id_field():
    from api import main

    for model in (main.TriggerRequest, main.DecideRequest, main.DismissRequest):
        assert "org_id" not in model.model_fields, f"{model.__name__} exposes org_id"
        assert "role" not in model.model_fields, f"{model.__name__} exposes role"


def test_no_endpoint_takes_an_org_id_query_param():
    """org_id must not be reachable through the URL either."""
    from api.main import app

    for route in app.routes:
        params = getattr(route, "dependant", None)
        if params is None:
            continue
        names = {p.name for p in route.dependant.query_params}
        assert "org_id" not in names, f"{route.path} still takes ?org_id="
        assert "role" not in names, f"{route.path} still takes ?role="


def test_org_id_in_the_body_is_refused_not_obeyed(client, key_factory):
    """A caller supplying org_id gets a 422 — louder than being ignored.

    'Ignored if present' and 'refused if present' both close the hole; refusing
    also stops anyone concluding the field works and building on it.
    """
    raw, _ = key_factory(org_id=1)
    r = client.post("/decisions/trigger",
                    headers={"X-API-Key": raw},
                    json={"email": "x@test.invalid", "org_id": 2})
    assert r.status_code == 422
    assert "org_id" in str(r.json())


def test_role_in_the_body_is_refused(client, key_factory):
    raw, _ = key_factory(org_id=1, role="sales")
    r = client.post("/decisions/trigger",
                    headers={"X-API-Key": raw},
                    json={"email": "x@test.invalid", "role": "admin"})
    assert r.status_code == 422


# --- Cross-tenant ----------------------------------------------------------- #

def test_an_org2_key_cannot_see_org1_decisions(client, key_factory, decision_factory):
    org1_decision = decision_factory(org_id=1)
    org2_raw, _ = key_factory(org_id=2)
    org1_raw, _ = key_factory(org_id=1)

    # Org 1's own key sees it...
    assert client.get(f"/decisions/{org1_decision}",
                      headers={"X-API-Key": org1_raw}).status_code == 200
    # ...org 2's key does not, and cannot say otherwise.
    r = client.get(f"/decisions/{org1_decision}", headers={"X-API-Key": org2_raw})
    assert r.status_code == 404, "org 2 read org 1's decision"


def test_an_org2_key_sees_only_its_own_queue(client, key_factory, decision_factory):
    org1_decision = decision_factory(org_id=1)
    org2_decision = decision_factory(org_id=2)
    org2_raw, _ = key_factory(org_id=2)

    ids = [d["id"] for d in client.get("/decisions/pending",
                                       headers={"X-API-Key": org2_raw}).json()]
    assert org2_decision in ids
    assert org1_decision not in ids, "org 1's decision leaked into org 2's queue"


def test_an_org2_key_cannot_approve_org1_decisions(client, key_factory,
                                                   decision_factory):
    org1_decision = decision_factory(org_id=1)
    org2_raw, _ = key_factory(org_id=2)

    r = client.post(f"/decisions/{org1_decision}/approve",
                    headers={"X-API-Key": org2_raw},
                    json={"decided_by": "attacker"})
    assert r.status_code == 409
    row = query("SELECT status, decided_by FROM decisions WHERE id = %s",
                (org1_decision,))[0]
    assert row["status"] == "pending_approval" and row["decided_by"] is None


# --- The role binds the agent ----------------------------------------------- #

def test_the_credentials_role_binds_the_gateway(client, key_factory, monkeypatch):
    """A sales key must produce a sales-bound agent; an admin key an admin one.

    Same build_mcp binding as always — only the SOURCE changed, from a hardcoded
    constant to the authenticated caller. Captures what the API actually passes
    to decisions.process.
    """
    import api.main as main

    seen = {}

    def spy(decision_id, email, *, org_id, role):
        seen["org_id"] = org_id
        seen["role"] = role
        return {"id": decision_id, "status": "pending_approval"}

    monkeypatch.setattr(decisions, "process", spy)

    for role, org in (("sales", 1), ("admin", 1), ("sales", 2)):
        raw, _ = key_factory(org_id=org, role=role)
        seen.clear()
        r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                        json={"email": "x@test.invalid"})
        assert r.status_code == 202
        # Background tasks run on TestClient teardown of the request.
        assert seen == {"org_id": org, "role": role}, (
            f"key(org={org}, role={role}) bound the gateway to {seen}"
        )
        execute("DELETE FROM decisions WHERE id = %s", (r.json()["decision_id"],))


def test_a_sales_key_cannot_read_admin_knowledge_chunks(key_factory):
    """The end of the chain: credential role -> SQL permission filter.

    Goes through _search_knowledge with the role the credential resolves to,
    rather than a hardcoded one, so this fails if the credential's role stops
    reaching the query.
    """
    import server

    sales_raw, _ = key_factory(org_id=1, role="sales")
    admin_raw, _ = key_factory(org_id=1, role="admin")

    sales = auth.resolve(sales_raw)
    admin = auth.resolve(admin_raw)

    _FAKE = [0.0] * server.KNOWLEDGE_DIM

    class _Fake:
        def embed(self, texts, **kw):
            return type("R", (), {"embeddings": [_FAKE]})()

    orig = server._voyage
    server._voyage = lambda: _Fake()
    try:
        s = server._search_knowledge("vertex postmortem", role=sales.role,
                                     org_id=sales.org_id)
        a = server._search_knowledge("vertex postmortem", role=admin.role,
                                     org_id=admin.org_id)
    finally:
        server._voyage = orig

    s_docs = [r["doc_name"] for r in s.get("results", [])]
    a_docs = [r["doc_name"] for r in a.get("results", [])]

    assert "deal-postmortems.md" not in s_docs, "sales key read admin-only chunks"
    assert "deal-postmortems.md" in a_docs, (
        "admin key cannot read admin docs — this test would pass vacuously"
    )
    for r in s.get("results", []):
        assert "sales" in r["permitted_roles"]


# --- Rate limiting ---------------------------------------------------------- #

def test_trigger_is_rate_limited_per_key(client, key_factory, monkeypatch):
    monkeypatch.setattr(decisions, "process",
                        lambda *a, **k: {"id": 0, "status": "x"})
    monkeypatch.setattr(auth, "TRIGGER_RATE_LIMIT_PER_HOUR", 3)

    raw, _ = key_factory()
    codes = []
    for _ in range(5):
        r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                        json={"email": "x@test.invalid"})
        codes.append(r.status_code)
        if r.status_code == 202:
            execute("DELETE FROM decisions WHERE id = %s",
                    (r.json()["decision_id"],))

    assert codes == [202, 202, 202, 429, 429], codes


def test_the_limit_is_per_key_not_global(client, key_factory, monkeypatch):
    """One key exhausting its budget must not lock everyone else out."""
    monkeypatch.setattr(decisions, "process",
                        lambda *a, **k: {"id": 0, "status": "x"})
    monkeypatch.setattr(auth, "TRIGGER_RATE_LIMIT_PER_HOUR", 1)

    a_raw, _ = key_factory(name="rate key A")
    b_raw, _ = key_factory(name="rate key B")

    for raw in (a_raw, a_raw):
        r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                        json={"email": "x@test.invalid"})
        if r.status_code == 202:
            execute("DELETE FROM decisions WHERE id = %s",
                    (r.json()["decision_id"],))
    assert r.status_code == 429, "key A should be exhausted"

    r = client.post("/decisions/trigger", headers={"X-API-Key": b_raw},
                    json={"email": "x@test.invalid"})
    assert r.status_code == 202, "key B must be unaffected by key A"
    execute("DELETE FROM decisions WHERE id = %s", (r.json()["decision_id"],))


def test_rate_limit_is_charged_before_any_work(client, key_factory, monkeypatch):
    """A 429 must not have spent an Anthropic call or created a row."""
    called = []
    monkeypatch.setattr(decisions, "process", lambda *a, **k: called.append(1))
    monkeypatch.setattr(auth, "TRIGGER_RATE_LIMIT_PER_HOUR", 0)

    raw, _ = key_factory()
    before = query("SELECT count(*) AS n FROM decisions")[0]["n"]
    r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                    json={"email": "x@test.invalid"})

    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "3600"
    assert not called, "the loop ran despite the rate limit"
    assert query("SELECT count(*) AS n FROM decisions")[0]["n"] == before, (
        "a rate-limited trigger created a decision row"
    )


def test_reads_are_not_rate_limited(client, key_factory, monkeypatch):
    """Throttling someone clearing their queue is the wrong failure."""
    monkeypatch.setattr(auth, "TRIGGER_RATE_LIMIT_PER_HOUR", 1)
    raw, _ = key_factory()
    for _ in range(5):
        assert client.get("/decisions/pending",
                          headers={"X-API-Key": raw}).status_code == 200


def test_charge_trigger_rolls_the_window_over(key_factory):
    raw, key_id = key_factory()
    for _ in range(3):
        auth.charge_trigger(key_id, limit=10)

    with pytest.raises(auth.RateLimitExceeded):
        auth.charge_trigger(key_id, limit=3)

    # Backdate the window: the next call starts a fresh one.
    execute("UPDATE api_key_rate_limit SET window_start = window_start - "
            "interval '2 hours' WHERE api_key_id = %s", (key_id,))
    assert auth.charge_trigger(key_id, limit=3) == 1
