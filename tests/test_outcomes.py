"""Outcome capture: ground truth, and the fact that it cannot be rewritten.

Roadmap item 7. The decision record says what the system DECIDED; this says what
actually HAPPENED. Everything Horizon 3 wants to measure — agreement rate,
threshold calibration — is computed against this table, so the property worth
testing hardest is not that a row can be written but that an earlier one cannot
be erased.
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
    made = []

    def make(*, org_id=1, role="sales", name=None):
        name = name or f"outcome test key org{org_id} {len(made)}"
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

    def make(org_id=1):
        did = decisions.create_processing(
            org_id=org_id, trigger_input={"email": "o@test.invalid"},
            identity={"role": "sales", "org_id": org_id,
                      "bound_at": "loop_construction"},
        )
        execute("UPDATE decisions SET status = 'auto_executed', "
                "proposed_action = 'route_to_sales' WHERE id = %s", (did,))
        made.append(did)
        return did

    yield make
    for did in made:
        execute("DELETE FROM outcomes WHERE decision_id = %s", (did,))
        execute("DELETE FROM decisions WHERE id = %s", (did,))


def _post(client, raw, did, **body):
    return client.post(f"/decisions/{did}/outcome", headers={"X-API-Key": raw},
                       json=body)


# --- The happy path --------------------------------------------------------- #

def test_an_outcome_is_recorded_and_read_back(client, key_factory,
                                              decision_factory):
    raw, _ = key_factory()
    did = decision_factory()

    r = _post(client, raw, did, outcome="converted", value_usd=42000,
              note="closed on the second call", recorded_by="dana@acme.test")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["outcome"] == "converted"
    assert body["value_usd"] == 42000
    assert body["decision_id"] == did

    detail = client.get(f"/decisions/{did}", headers={"X-API-Key": raw}).json()
    assert len(detail["outcomes"]) == 1
    assert detail["outcomes"][0]["outcome"] == "converted"
    assert detail["outcomes"][0]["recorded_by"] == "dana@acme.test"


def test_value_and_note_are_optional(client, key_factory, decision_factory):
    raw, _ = key_factory()
    did = decision_factory()

    r = _post(client, raw, did, outcome="contacted", recorded_by="sam")
    assert r.status_code == 201, r.text
    assert r.json()["value_usd"] is None
    assert r.json()["note"] is None


# --- Append-only: the property the analytics rest on ------------------------ #

def test_a_correction_is_a_new_row_and_the_latest_wins(client, key_factory,
                                                       decision_factory):
    """Somebody marked this converted, then lost. BOTH facts survive."""
    raw, _ = key_factory()
    did = decision_factory()

    assert _post(client, raw, did, outcome="converted", value_usd=50000,
                 recorded_by="optimist").status_code == 201
    assert _post(client, raw, did, outcome="lost",
                 note="churned in trial", recorded_by="realist").status_code == 201

    detail = client.get(f"/decisions/{did}", headers={"X-API-Key": raw}).json()
    outcomes = detail["outcomes"]

    assert len(outcomes) == 2, "the correction replaced the original instead of appending"
    assert outcomes[0]["outcome"] == "lost", "newest first: the latest row must win"
    assert outcomes[1]["outcome"] == "converted", (
        "the superseded row is the correction's evidence and must survive"
    )
    assert outcomes[1]["value_usd"] == 50000


def test_there_is_no_update_or_delete_route_for_an_outcome():
    """Append-only by construction, not by convention."""
    from api.main import app

    methods = {
        (getattr(r, "path", ""), m)
        for r in app.routes
        for m in getattr(r, "methods", set())
    }
    for verb in ("PUT", "PATCH", "DELETE"):
        assert not [p for p, m in methods if "outcome" in p and m == verb], (
            f"a {verb} route on outcomes exists — ground truth that can be "
            "edited in place is not ground truth"
        )


# --- Validation ------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["won", "CONVERTED", "", "deleted"])
def test_an_unknown_outcome_value_is_422(client, key_factory, decision_factory,
                                         bad):
    raw, _ = key_factory()
    did = decision_factory()

    r = _post(client, raw, did, outcome=bad, recorded_by="sam")
    assert r.status_code == 422, f"{bad!r} was accepted: {r.text}"
    assert not query("SELECT id FROM outcomes WHERE decision_id = %s", (did,))


def test_every_documented_outcome_value_is_accepted(client, key_factory,
                                                    decision_factory):
    from api.main import OUTCOME_VALUES

    raw, _ = key_factory()
    did = decision_factory()
    for value in OUTCOME_VALUES:
        r = _post(client, raw, did, outcome=value, recorded_by="sam")
        assert r.status_code == 201, f"{value} rejected: {r.text}"


def test_a_negative_value_is_refused(client, key_factory, decision_factory):
    raw, _ = key_factory()
    did = decision_factory()
    assert _post(client, raw, did, outcome="converted", value_usd=-1,
                 recorded_by="sam").status_code == 422


def test_recorded_by_is_required(client, key_factory, decision_factory):
    raw, _ = key_factory()
    did = decision_factory()
    r = client.post(f"/decisions/{did}/outcome", headers={"X-API-Key": raw},
                    json={"outcome": "contacted"})
    assert r.status_code == 422, "an outcome with no claimant was accepted"


def test_the_body_forbids_unknown_fields(client, key_factory, decision_factory):
    """StrictRequest: org_id in the body is refused, not ignored."""
    raw, _ = key_factory()
    did = decision_factory()
    r = _post(client, raw, did, outcome="contacted", recorded_by="sam", org_id=2)
    assert r.status_code == 422


# --- Org scoping, both directions ------------------------------------------- #

def test_another_orgs_decision_is_404_not_403(client, key_factory,
                                              decision_factory):
    """Indistinguishable from absent — 403 would confirm the id is real."""
    org2_raw, _ = key_factory(org_id=2, name="outcome org2 key")
    org1_decision = decision_factory(org_id=1)

    r = _post(client, org2_raw, org1_decision, outcome="converted",
              recorded_by="intruder")
    assert r.status_code == 404, r.text
    assert not query("SELECT id FROM outcomes WHERE decision_id = %s",
                     (org1_decision,)), "an outcome was written cross-org"

    # Same status as an id that genuinely does not exist.
    missing = _post(client, org2_raw, 999_999_999, outcome="converted",
                    recorded_by="intruder")
    assert missing.status_code == 404
    assert missing.json()["detail"] == r.json()["detail"]


def test_an_orgs_own_decision_still_works(client, key_factory, decision_factory):
    """The other direction: scoping must not lock an org out of its own rows."""
    org2_raw, _ = key_factory(org_id=2, name="outcome org2 owner key")
    org2_decision = decision_factory(org_id=2)

    r = _post(client, org2_raw, org2_decision, outcome="qualified",
              recorded_by="owner")
    assert r.status_code == 201, r.text


def test_outcomes_do_not_leak_across_orgs_in_the_detail_view(client, key_factory,
                                                             decision_factory):
    org1_raw, _ = key_factory(org_id=1, name="outcome leak org1")
    org2_raw, _ = key_factory(org_id=2, name="outcome leak org2")
    did = decision_factory(org_id=1)

    assert _post(client, org1_raw, did, outcome="converted",
                 recorded_by="owner").status_code == 201

    assert client.get(f"/decisions/{did}",
                      headers={"X-API-Key": org2_raw}).status_code == 404


def test_a_missing_decision_is_404(client, key_factory):
    raw, _ = key_factory()
    r = _post(client, raw, 999_999_999, outcome="contacted", recorded_by="sam")
    assert r.status_code == 404


# --- The budget ------------------------------------------------------------- #

def test_recording_an_outcome_is_not_rate_limited(client, key_factory,
                                                  decision_factory, monkeypatch):
    """The limit bounds model spend. An outcome spends nothing, and throttling
    the data every later measurement rests on is the wrong failure."""
    monkeypatch.setattr(auth, "TRIGGER_RATE_LIMIT_PER_HOUR", 1)
    raw, key_id = key_factory()
    did = decision_factory()

    for i in range(5):
        r = _post(client, raw, did, outcome="contacted", recorded_by=f"sam{i}")
        assert r.status_code == 201, r.text

    used = query("SELECT count FROM api_key_rate_limit WHERE api_key_id = %s",
                 (key_id,))
    assert not used or used[0]["count"] == 0, (
        "recording an outcome charged the trigger budget"
    )
