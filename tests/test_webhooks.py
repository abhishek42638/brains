"""Events out: signed, retried, org-scoped — and pointed only outward.

Three separate guarantees are under test here, and they fail in different ways.

  1. THE SSRF GUARD. A customer-supplied URL is a request our server makes with
     our server's network position. On GCP the payoff is concrete: 169.254.169.254
     hands back an OAuth token for the runtime service account to anyone inside
     the instance who asks. These tests mock DNS resolution rather than relying
     on the real thing, because the guarantee is "whatever this name resolves
     to, an internal address is refused" — and a test that needed a real hostile
     domain would be a test that only ran when someone else's DNS was up.

  2. THE SIGNATURE. A receiver's only defence against a forged payload. The
     timestamp is inside the signed material, so these tests check both that a
     good signature verifies and that a captured delivery cannot be replayed
     with an advanced clock.

  3. DELIVERY NEVER BLOCKS A DECISION. A customer's dead endpoint, hostile URL
     or unreachable registry must not fail, delay or roll back qualification.
     This is the guarantee most likely to be broken by a well-meaning refactor
     that decides an error ought to propagate, so it is tested from the outside:
     drive a real decision to a terminal state with a broken endpoint attached
     and assert the decision landed anyway.
"""

import socket

import pytest
from fastapi.testclient import TestClient

import auth
import decisions
import tasks
import webhooks
from db import execute, query


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")

IDENTITY = {"role": "sales", "org_id": 1, "bound_at": "loop_construction"}


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def key_factory():
    made = []

    def make(*, org_id=1, role="sales", name=None):
        name = name or f"webhook test org{org_id} {role} {len(made)}"
        raw, key_id = auth.create_key(org_id=org_id, role=role, name=name)
        made.append(key_id)
        return raw

    yield make
    for key_id in made:
        execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (key_id,))
        execute("DELETE FROM api_keys WHERE id = %s", (key_id,))


@pytest.fixture
def endpoint_factory():
    """Register endpoints directly, bypassing the registration-time URL check.

    Deliberate: several tests below need an endpoint whose URL the CRUD layer
    would refuse, precisely so they can prove the DELIVERY-time guard refuses it
    too. If the only way to get such a row were through the API, those tests
    could not exist — and the delivery-time guard is the one that matters, since
    DNS can change after registration.
    """
    made = []

    def make(*, org_id=1, url="https://receiver.test/hook", events=None):
        row = webhooks.create_endpoint(
            org_id=org_id, url=url, events=events or [webhooks.EVENT_WILDCARD],
        )
        made.append(row["id"])
        return row

    yield make
    for endpoint_id in made:
        execute("DELETE FROM webhook_deliveries WHERE endpoint_id = %s",
                (endpoint_id,))
        execute("DELETE FROM webhook_endpoints WHERE id = %s", (endpoint_id,))


@pytest.fixture
def decision_factory():
    made = []

    def make(*, org_id=1, status="pending_approval"):
        did = decisions.create_processing(
            org_id=org_id, trigger_input={"email": "wh@test.invalid"},
            identity=IDENTITY,
        )
        execute("UPDATE decisions SET status = %s WHERE id = %s", (status, did))
        made.append(did)
        return did

    yield make
    for did in made:
        execute("DELETE FROM webhook_deliveries WHERE decision_id = %s", (did,))
        execute("DELETE FROM decisions WHERE id = %s", (did,))


def _resolves_to(monkeypatch, *addresses):
    """Pin DNS resolution. The guarantee is about the ADDRESS, not the name."""
    monkeypatch.setattr(webhooks, "_resolve", lambda host: list(addresses))


# =========================================================================== #
# 1. THE SSRF GUARD
# =========================================================================== #

@pytest.mark.parametrize("address,label", [
    ("169.254.169.254", "the GCP metadata server — SA tokens live here"),
    ("169.254.0.1", "link-local"),
    ("127.0.0.1", "loopback — our own /internal/* handlers"),
    ("127.0.0.53", "loopback"),
    ("10.0.0.5", "RFC1918 — VPC peers"),
    ("10.128.0.2", "RFC1918"),
    ("172.16.0.1", "RFC1918"),
    ("192.168.1.1", "RFC1918"),
    ("0.0.0.0", "unspecified"),
    ("::1", "IPv6 loopback"),
    ("fe80::1", "IPv6 link-local"),
    ("fc00::1", "IPv6 unique-local"),
    ("::ffff:169.254.169.254", "IPv4-mapped metadata server"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
])
def test_an_internal_address_is_refused(monkeypatch, address, label):
    """THE test. A customer URL must not point our server at our own network.

    Resolution is mocked, so this asserts the property that actually matters:
    whatever `evil.test` resolves to, an internal address is refused. A guard
    that only inspected the literal string would pass a URL whose *hostname*
    looks innocent and whose *address* is the metadata server — which is the
    realistic attack, since the attacker controls their own DNS.
    """
    _resolves_to(monkeypatch, address)

    with pytest.raises(webhooks.SSRFRefused):
        webhooks.assert_public_url("https://evil.test/hook")


def test_the_metadata_server_is_named_in_the_refusal(monkeypatch):
    """169.254.169.254 says what it is, not merely 'link-local'.

    An operator reading this line at 3am should not have to know that the
    metadata server lives in the link-local range.
    """
    _resolves_to(monkeypatch, "169.254.169.254")

    with pytest.raises(webhooks.SSRFRefused, match="metadata"):
        webhooks.assert_public_url("https://evil.test/hook")


def test_a_public_address_is_allowed(monkeypatch):
    """The positive case — else every test above passes vacuously.

    A guard that refused everything would satisfy the whole section above while
    making the feature useless.
    """
    _resolves_to(monkeypatch, "93.184.216.34")

    assert webhooks.assert_public_url("https://receiver.test/hook") == [
        "93.184.216.34"
    ]


def test_one_bad_address_among_several_refuses_the_whole_name(monkeypatch):
    """ALL resolved addresses are checked, not just the first.

    A hostname returning one public address and one internal one must be
    refused. Which record comes back first is up to the resolver, and a guard
    that checked only [0] would pass or fail at random — the worst possible
    behaviour for a security control.
    """
    _resolves_to(monkeypatch, "93.184.216.34", "169.254.169.254")

    with pytest.raises(webhooks.SSRFRefused, match="metadata"):
        webhooks.assert_public_url("https://evil.test/hook")


def test_an_unresolvable_host_is_refused(monkeypatch):
    """Fail closed: an address we cannot vouch for is not one we deliver to.

    Otherwise a deliberately unresolvable name would be a way to skip the check.
    """
    def boom(host):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(webhooks, "_resolve", boom)

    with pytest.raises(webhooks.SSRFRefused, match="resolve"):
        webhooks.assert_public_url("https://nope.test/hook")


def test_a_host_resolving_to_nothing_is_refused(monkeypatch):
    """An empty answer is not permission."""
    _resolves_to(monkeypatch)

    with pytest.raises(webhooks.SSRFRefused):
        webhooks.assert_public_url("https://empty.test/hook")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://evil.test/",
    "ftp://evil.test/",
    "redis://10.0.0.1:6379",
])
def test_a_non_http_scheme_is_refused(url):
    """Only http(s) is a webhook. No DNS needed to know that."""
    with pytest.raises(webhooks.SSRFRefused, match="scheme"):
        webhooks.assert_public_url(url)


def test_plaintext_http_is_refused_off_localhost(monkeypatch):
    """Plaintext leaks the payload AND the signature to anyone on the path."""
    _resolves_to(monkeypatch, "93.184.216.34")

    with pytest.raises(webhooks.SSRFRefused, match="https"):
        webhooks.assert_public_url("http://receiver.test/hook")


def test_localhost_http_is_allowed_only_when_explicitly_emulated():
    """The local escape hatch is an opt-in, never an absence.

    Same rule as tasks.emulated(): a deployed service cannot reach this branch,
    because K_SERVICE overrides the flag. Absence is not consent.
    """
    assert webhooks.assert_public_url("http://localhost:9000/hook",
                                      allow_localhost=True) == ["localhost"]

    with pytest.raises(webhooks.SSRFRefused):
        webhooks.assert_public_url("http://localhost:9000/hook",
                                   allow_localhost=False)


def test_a_url_with_no_host_is_refused():
    with pytest.raises(webhooks.SSRFRefused, match="no host"):
        webhooks.assert_public_url("https:///hook")


@needs_db
def test_registration_refuses_an_obviously_internal_url(client, key_factory):
    """A courtesy 422 at registration, so the mistake surfaces immediately.

    NOT the real guard — that runs per delivery attempt, because DNS is mutable.
    But a customer pasting the metadata URL deserves an error now rather than a
    201 followed by deliveries that silently never arrive.
    """
    r = client.post("/webhooks", headers={"X-API-Key": key_factory()},
                    json={"url": "http://169.254.169.254/computeMetadata/v1/",
                          "events": ["proposed"]})
    assert r.status_code == 422
    assert "metadata" in str(r.json()).lower()


# =========================================================================== #
# 2. THE SIGNATURE
# =========================================================================== #

def test_a_signature_verifies_with_the_shared_secret():
    """What a receiver runs. The 10 lines in the README must actually work."""
    secret, timestamp, body = "whsec_test", "1770000000", b'{"event":"proposed"}'

    signature = webhooks.sign(secret, timestamp, body)

    assert webhooks.verify(secret, timestamp, body, signature) is True


def test_the_wrong_secret_fails():
    """The whole point: possession of the secret is what authenticates."""
    timestamp, body = "1770000000", b'{"event":"proposed"}'
    signature = webhooks.sign("whsec_real", timestamp, body)

    assert webhooks.verify("whsec_wrong", timestamp, body, signature) is False


def test_a_tampered_body_fails():
    """The signature covers the bytes, so editing them breaks it."""
    secret, timestamp = "whsec_test", "1770000000"
    signature = webhooks.sign(secret, timestamp, b'{"action":"nurture"}')

    assert webhooks.verify(secret, timestamp, b'{"action":"route_to_sales"}',
                           signature) is False


def test_a_replayed_delivery_cannot_have_its_timestamp_advanced():
    """The timestamp is INSIDE the signed material, not merely beside it.

    Signing the body alone produces a token that is valid forever: anyone who
    captures one delivery can replay it at any later time and the signature
    still verifies, because nothing signed says when it was made. Binding the
    timestamp in means an attacker cannot advance the clock without invalidating
    the MAC, and cannot keep the MAC without keeping the stale timestamp — which
    is what makes a receiver's freshness check enforceable.
    """
    secret, body = "whsec_test", b'{"event":"approved"}'
    captured = webhooks.sign(secret, "1770000000", body)

    assert webhooks.verify(secret, "1770000000", body, captured) is True
    assert webhooks.verify(secret, "1799999999", body, captured) is False, (
        "the signature survived a changed timestamp — a captured delivery can "
        "be replayed forever and no receiver-side freshness check can stop it"
    )


def test_the_signed_body_is_the_body_that_is_sent():
    """Serialise once. Re-serialising between signing and sending is a latent bug."""
    body = webhooks.build_body(
        event="proposed", delivery_uuid="abc",
        decision={"id": 1, "status": "pending_approval"},
    )
    assert isinstance(body, bytes)
    assert webhooks.build_body(
        event="proposed", delivery_uuid="abc",
        decision={"status": "pending_approval", "id": 1},
    ) == body, "key order changes the bytes — the signature would not survive it"


def test_generated_secrets_are_unique_and_prefixed():
    secrets = {webhooks.generate_secret() for _ in range(50)}
    assert len(secrets) == 50
    assert all(s.startswith(webhooks.SECRET_PREFIX) for s in secrets)


# =========================================================================== #
# 3. ORG SCOPING — an org manages and receives only its own
# =========================================================================== #

@needs_db
def test_an_org_cannot_see_another_orgs_endpoints(client, key_factory,
                                                  endpoint_factory):
    org1_endpoint = endpoint_factory(org_id=1)
    org2_key = key_factory(org_id=2)

    listed = client.get("/webhooks", headers={"X-API-Key": org2_key}).json()
    assert org1_endpoint["id"] not in [e["id"] for e in listed]

    r = client.get(f"/webhooks/{org1_endpoint['id']}",
                   headers={"X-API-Key": org2_key})
    assert r.status_code == 404, "org 2 read org 1's endpoint"


@needs_db
def test_an_org_cannot_edit_or_delete_another_orgs_endpoint(
    client, key_factory, endpoint_factory,
):
    """A 404 on write, and the row must be genuinely unchanged."""
    org1_endpoint = endpoint_factory(org_id=1, url="https://original.test/hook")
    org2_key = key_factory(org_id=2)

    r = client.patch(f"/webhooks/{org1_endpoint['id']}",
                     headers={"X-API-Key": org2_key},
                     json={"url": "https://attacker.test/steal"})
    assert r.status_code == 404

    r = client.delete(f"/webhooks/{org1_endpoint['id']}",
                      headers={"X-API-Key": org2_key})
    assert r.status_code == 404

    row = query("SELECT url FROM webhook_endpoints WHERE id = %s",
                (org1_endpoint["id"],))
    assert row and row[0]["url"] == "https://original.test/hook", (
        "org 2 repointed org 1's webhook at its own server — every future "
        "decision in org 1 would be delivered to the attacker"
    )


@needs_db
def test_an_event_only_reaches_endpoints_in_its_own_org(endpoint_factory,
                                                        decision_factory):
    """The delivery fan-out is org-scoped, which is where a leak would matter.

    A cross-org delivery does not just reveal that a decision exists — the
    payload is the FULL decision record including reasoning, so it would hand
    another tenant the lead, the score and the model's rationale.
    """
    org1_endpoint = endpoint_factory(org_id=1)
    org2_endpoint = endpoint_factory(org_id=2)
    did = decision_factory(org_id=1)

    delivery_ids = webhooks.emit("proposed", decision_id=did, org_id=1)

    endpoints_used = {
        query("SELECT endpoint_id FROM webhook_deliveries WHERE id = %s",
              (d,))[0]["endpoint_id"]
        for d in delivery_ids
    }
    assert org1_endpoint["id"] in endpoints_used
    assert org2_endpoint["id"] not in endpoints_used, (
        "org 2 was sent org 1's full decision record, reasoning included"
    )


@needs_db
def test_the_secret_is_returned_once_and_never_listed(client, key_factory):
    """A list call that returned signing secrets would turn one leaked read-only
    key into the ability to forge deliveries for every endpoint in the org."""
    key = key_factory()
    created = client.post("/webhooks", headers={"X-API-Key": key},
                          json={"url": "https://receiver.test/hook",
                                "events": ["proposed"]})
    assert created.status_code == 201
    endpoint_id = created.json()["id"]
    try:
        assert created.json()["secret"].startswith(webhooks.SECRET_PREFIX)

        listed = client.get("/webhooks", headers={"X-API-Key": key}).json()
        one = client.get(f"/webhooks/{endpoint_id}",
                         headers={"X-API-Key": key}).json()

        assert all("secret" not in e for e in listed), "secrets leaked from list"
        assert "secret" not in one, "secrets leaked from get"
    finally:
        execute("DELETE FROM webhook_endpoints WHERE id = %s", (endpoint_id,))


@needs_db
def test_subscriptions_filter_by_event(endpoint_factory, decision_factory):
    """An endpoint hears only what it asked for."""
    approved_only = endpoint_factory(org_id=1, events=["approved"])
    proposed_only = endpoint_factory(org_id=1, events=["proposed"])
    everything = endpoint_factory(org_id=1, events=[webhooks.EVENT_WILDCARD])
    did = decision_factory(org_id=1)

    matched = {e["id"] for e in webhooks.endpoints_for_event(org_id=1,
                                                             event="proposed")}

    assert proposed_only["id"] in matched
    assert everything["id"] in matched, "'*' did not match"
    assert approved_only["id"] not in matched, "an unsubscribed endpoint matched"


@needs_db
def test_an_inactive_endpoint_receives_nothing(endpoint_factory, decision_factory):
    """`active: false` is the pause button — it stops deliveries being generated
    at all, rather than generating them to fail five times each."""
    endpoint = endpoint_factory(org_id=1)
    execute("UPDATE webhook_endpoints SET active = FALSE WHERE id = %s",
            (endpoint["id"],))
    did = decision_factory(org_id=1)

    assert webhooks.emit("proposed", decision_id=did, org_id=1) == []


@needs_db
@pytest.mark.parametrize("bad_event", ["aproved", "PROPOSED", "decision.made", ""])
def test_an_unknown_event_name_is_a_422(client, key_factory, bad_event):
    """A typo'd subscription must fail loudly, not silently never fire.

    Subscribe to 'aproved', get a 201, then wait forever for a webhook while
    assuming the system is broken — that is the failure this prevents.
    """
    r = client.post("/webhooks", headers={"X-API-Key": key_factory()},
                    json={"url": "https://receiver.test/hook",
                          "events": [bad_event]})
    assert r.status_code == 422


@needs_db
def test_webhook_bodies_refuse_identity(client, key_factory):
    """Same rule as everywhere else: no org_id in a request body."""
    r = client.post("/webhooks", headers={"X-API-Key": key_factory()},
                    json={"url": "https://receiver.test/hook",
                          "events": ["proposed"], "org_id": 2})
    assert r.status_code == 422
    assert "org_id" in str(r.json())


# =========================================================================== #
# 4. A BROKEN ENDPOINT MUST NOT STALL A DECISION
# =========================================================================== #

@needs_db
def test_a_dead_endpoint_does_not_stall_the_decision(monkeypatch, endpoint_factory,
                                                     decision_factory):
    """The guarantee most likely to be broken by a well-meaning refactor.

    A customer's receiver being down is not a reason for qualification to fail.
    The decision is the product; the notification is a consequence of it. So
    this is tested from the outside: attach an endpoint whose delivery explodes,
    then assert the transition landed anyway.
    """
    endpoint_factory(org_id=1)
    did = decision_factory(org_id=1, status="pending_approval")

    def boom(*a, **k):
        raise RuntimeError("receiver is on fire")

    monkeypatch.setattr(tasks, "enqueue_delivery", boom)

    from agent import gate

    result = gate.approve(did, "a-human", 1)

    assert result["ok"] is True, (
        "a human's approval failed because a webhook could not be enqueued"
    )
    assert query("SELECT status FROM decisions WHERE id = %s",
                 (did,))[0]["status"] == "approved"


@needs_db
def test_a_failed_enqueue_closes_the_delivery_rather_than_leaving_it_pending(
    monkeypatch, endpoint_factory, decision_factory,
):
    """'pending' is not somewhere a delivery may come to rest.

    Same rule as 'processing' for decisions, and for the same reason: a row that
    nothing will ever move is a silent failure wearing a status.
    """
    endpoint_factory(org_id=1)
    did = decision_factory(org_id=1)

    monkeypatch.setattr(tasks, "enqueue_delivery",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("queue unreachable")))

    delivery_ids = webhooks.emit("proposed", decision_id=did, org_id=1)
    assert delivery_ids, "no delivery row was created at all"

    row = query("SELECT status, error FROM webhook_deliveries WHERE id = %s",
                (delivery_ids[0],))[0]
    assert row["status"] == webhooks.DELIVERY_FAILED
    assert "enqueue" in row["error"]


@needs_db
def test_an_unreadable_endpoint_registry_does_not_break_emission(monkeypatch,
                                                                 decision_factory):
    """Even the LOOKUP failing must not propagate into a decision."""
    did = decision_factory(org_id=1)

    monkeypatch.setattr(webhooks, "endpoints_for_event",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("db down")))

    assert webhooks.emit("proposed", decision_id=did, org_id=1) == []


@needs_db
def test_emission_never_re_enters_the_decision_module(monkeypatch, endpoint_factory,
                                                      decision_factory):
    """needs_review is called from the handler that runs when ENQUEUEING failed.

    So if emitting from that path raised — or worse, called back into
    needs_review — a broken queue would turn one parked row into a loop.
    """
    endpoint_factory(org_id=1)
    did = decision_factory(org_id=1, status="processing")

    monkeypatch.setattr(tasks, "enqueue_delivery",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("queue unreachable")))

    result = decisions.needs_review(
        did, org_id=1, identity=IDENTITY, error="enqueue failed upstream",
    )

    assert result["status"] == decisions.STATUS_NEEDS_REVIEW
    assert query("SELECT status FROM decisions WHERE id = %s",
                 (did,))[0]["status"] == "needs_review"


# =========================================================================== #
# 5. DELIVERY OUTCOMES ARE RECORDED
# =========================================================================== #

class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


@needs_db
def test_a_successful_delivery_is_recorded_with_its_status_code(
    monkeypatch, endpoint_factory, decision_factory,
):
    """'Did the webhook fire?' has to be a query, not a guess."""
    endpoint = endpoint_factory(org_id=1, url="https://receiver.test/hook")
    did = decision_factory(org_id=1)
    record = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                      decision_id=did, event="proposed")

    monkeypatch.setattr(webhooks, "_resolve", lambda h: ["93.184.216.34"])
    sent = {}

    def fake_post(url, content, headers, timeout, follow_redirects):
        sent.update(url=url, content=content, headers=headers)
        return _Response(200)

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    result = webhooks.deliver(record["id"], attempt=0, is_final_attempt=False)

    assert result["status"] == webhooks.DELIVERY_DELIVERED
    row = query("SELECT * FROM webhook_deliveries WHERE id = %s",
                (record["id"],))[0]
    assert row["status"] == "delivered"
    assert row["status_code"] == 200
    assert row["attempts"] == 1
    assert row["delivered_at"] is not None

    # The headers a receiver needs, and a signature that actually verifies.
    assert sent["headers"]["X-Brains-Event"] == "proposed"
    assert sent["headers"]["X-Brains-Delivery"] == record["delivery_uuid"]
    assert webhooks.verify(
        endpoint["secret"], sent["headers"]["X-Brains-Timestamp"],
        sent["content"], sent["headers"]["X-Brains-Signature"],
    ), "the signature we sent does not verify with the secret we issued"


@needs_db
def test_the_payload_carries_the_full_decision_including_reasoning(
    monkeypatch, endpoint_factory, decision_factory,
):
    """Reasoning is the point. A receiver that only learns the status has to
    come back and ask why; the whole argument of this system is that the trail
    travels with the decision."""
    endpoint = endpoint_factory(org_id=1)
    did = decision_factory(org_id=1)
    record = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                      decision_id=did, event="proposed")

    monkeypatch.setattr(webhooks, "_resolve", lambda h: ["93.184.216.34"])
    sent = {}

    import json

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, content, headers, timeout,
                        follow_redirects: (sent.update(body=content),
                                           _Response(200))[1])

    webhooks.deliver(record["id"], attempt=0, is_final_attempt=False)

    payload = json.loads(sent["body"])
    assert payload["event"] == "proposed"
    assert payload["decision"]["id"] == did
    assert "reasoning" in payload["decision"]
    assert payload["decision"]["org_id"] == 1


@needs_db
def test_an_ssrf_refusal_is_permanent_and_not_retried(monkeypatch,
                                                      endpoint_factory,
                                                      decision_factory):
    """A refusal must not burn five attempts to reach the same answer.

    And it must be recorded as a refusal, so an operator can see WHY nothing
    arrived rather than concluding the receiver is flaky.
    """
    endpoint = endpoint_factory(org_id=1, url="https://internal.test/hook")
    did = decision_factory(org_id=1)
    record = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                      decision_id=did, event="proposed")

    monkeypatch.setattr(webhooks, "_resolve", lambda h: ["169.254.169.254"])

    posted = []
    import httpx

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: posted.append(1) or _Response(200))

    result = webhooks.deliver(record["id"], attempt=0, is_final_attempt=False)

    assert result["reason"] == "ssrf_refused"
    assert not posted, "the guard let the request through to httpx"
    row = query("SELECT status, error FROM webhook_deliveries WHERE id = %s",
                (record["id"],))[0]
    assert row["status"] == webhooks.DELIVERY_FAILED
    assert "metadata" in row["error"]


@needs_db
def test_a_5xx_raises_for_retry_until_the_last_attempt(monkeypatch,
                                                        endpoint_factory,
                                                        decision_factory):
    """Retryable while retries remain; terminal on the last one.

    A 5xx on the final attempt would just burn another retry to reach the same
    place, so the row is closed instead — the same reasoning as
    /internal/process's retry-exhaustion branch.
    """
    endpoint = endpoint_factory(org_id=1)
    did = decision_factory(org_id=1)
    monkeypatch.setattr(webhooks, "_resolve", lambda h: ["93.184.216.34"])

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(500))

    mid = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                   decision_id=did, event="proposed")
    with pytest.raises(webhooks.Retryable):
        webhooks.deliver(mid["id"], attempt=1, is_final_attempt=False)
    assert query("SELECT status FROM webhook_deliveries WHERE id = %s",
                 (mid["id"],))[0]["status"] == webhooks.DELIVERY_PENDING

    last = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                    decision_id=did, event="proposed")
    result = webhooks.deliver(last["id"], attempt=4, is_final_attempt=True)
    assert result["reason"] == "exhausted"
    row = query("SELECT status, attempts FROM webhook_deliveries WHERE id = %s",
                (last["id"],))[0]
    assert row["status"] == webhooks.DELIVERY_FAILED, (
        "a delivery came to rest in 'pending' with no attempts left to move it"
    )
    assert row["attempts"] == 5


@needs_db
def test_a_delivered_delivery_is_not_sent_twice(monkeypatch, endpoint_factory,
                                                decision_factory):
    """Cloud Tasks is at-least-once, so a retry of an attempt that landed must
    not re-send it."""
    endpoint = endpoint_factory(org_id=1)
    did = decision_factory(org_id=1)
    record = webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                                      decision_id=did, event="proposed")
    webhooks.finish_delivery(record["id"],
                             status=webhooks.DELIVERY_DELIVERED,
                             attempts=1, status_code=200)

    posted = []
    import httpx

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: posted.append(1) or _Response(200))

    result = webhooks.deliver(record["id"], attempt=1, is_final_attempt=False)

    assert result.get("already") is True
    assert not posted, "an already-delivered event was sent a second time"


@needs_db
def test_deliveries_are_queryable_per_decision(client, key_factory,
                                               endpoint_factory,
                                               decision_factory):
    """The endpoint that settles 'we sent it' vs 'we never got it'."""
    endpoint = endpoint_factory(org_id=1)
    did = decision_factory(org_id=1)
    webhooks.record_delivery(org_id=1, endpoint_id=endpoint["id"],
                             decision_id=did, event="proposed")

    r = client.get(f"/decisions/{did}/deliveries",
                   headers={"X-API-Key": key_factory(org_id=1)})
    assert r.status_code == 200
    assert [d["event"] for d in r.json()] == ["proposed"]

    r2 = client.get(f"/decisions/{did}/deliveries",
                    headers={"X-API-Key": key_factory(org_id=2)})
    assert r2.status_code == 404, "org 2 read org 1's delivery history"


# =========================================================================== #
# 6. THE EVENT MAP
# =========================================================================== #

def test_every_terminal_status_maps_to_an_event():
    """A status with no event is a transition nobody downstream can hear."""
    for status in ("pending_approval", "auto_executed", "auto_discarded",
                   "approved", "rejected", "needs_review"):
        assert status in webhooks.STATUS_TO_EVENT, f"{status} emits nothing"


def test_pending_approval_is_published_as_proposed():
    """The internal status name is a database detail; the event is a contract."""
    assert webhooks.STATUS_TO_EVENT["pending_approval"] == "proposed"


def test_transient_statuses_emit_nothing():
    """'processing' is not a decision, so it is not an event."""
    assert webhooks.emit_for_status("processing", decision_id=1, org_id=1) == []


def test_every_mapped_event_is_a_known_event():
    """The map and the vocabulary cannot drift apart."""
    assert set(webhooks.STATUS_TO_EVENT.values()) <= set(webhooks.KNOWN_EVENTS)
