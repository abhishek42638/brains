"""A decision is OF A TYPE — the seam, not a second tenant of it.

Phase 7 adds three things and no new behaviour: a `decision_type` column on
decisions, a policy key of (org_id, decision_type), and a registry that scoring
resolves through. `lead_qualification` is the only entry, so every one of these
tests is really asking the same question — does the seam hold when there is
exactly one thing on either side of it?
"""

import pytest
from fastapi.testclient import TestClient

import auth
import decisions
import rules
import server
from db import execute, query


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


@pytest.fixture
def key_factory():
    made = []

    def make(*, org_id=1, role="sales", name=None):
        name = name or f"decision-type test key {len(made)}"
        raw, key_id = auth.create_key(org_id=org_id, role=role, name=name)
        made.append(key_id)
        return raw, key_id

    yield make
    for key_id in made:
        execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (key_id,))
        execute("DELETE FROM api_keys WHERE id = %s", (key_id,))


# --- The registry ----------------------------------------------------------- #

def test_lead_qualification_is_the_first_entry_and_currently_the_only_one():
    assert rules.known_decision_types() == ("lead_qualification",)
    assert rules.DEFAULT_DECISION_TYPE == "lead_qualification"


def test_the_entry_carries_its_own_rules_version():
    """Not a module constant: a second type's rules are not version 1.0.0."""
    entry = rules.entry_for("lead_qualification")
    assert entry["rules_version"] == rules.RULES_VERSION
    assert callable(entry["score"])


def test_an_unknown_type_raises_rather_than_defaulting():
    """Scoring by another type's rules would produce a meaningless number that
    looks exactly like a meaningful one."""
    with pytest.raises(rules.UnknownDecisionType) as excinfo:
        rules.entry_for("renewal_risk")
    assert "renewal_risk" in str(excinfo.value)
    assert "lead_qualification" in str(excinfo.value), (
        "the error should name what IS available"
    )


def test_the_registry_scorer_is_the_scoring_that_was_already_there():
    """The refactor moved the rules; it must not have changed them."""
    facts = {"title": "VP Sales", "employee_count": 600,
             "annual_revenue_usd": 200_000_000, "source": "webinar",
             "company_name": "Acme"}
    scored = rules.score_lead_qualification(facts)
    # 30 senior title + 25 size + 25 revenue + 20 source
    assert scored["score"] == 100
    assert scored["band"] == "hot"
    assert len(scored["reasons"]) == 4


def test_a_gateway_cannot_be_built_for_a_type_nothing_can_score():
    """Same rule as an unknown role: fail at construction, not mid-run."""
    with pytest.raises(rules.UnknownDecisionType):
        server.build_mcp(role="sales", org_id=1, decision_type="renewal_risk")

    # And the default still builds.
    assert server.build_mcp(role="sales", org_id=1) is not None


# --- The edge: a type nothing can score is never accepted ------------------- #

@needs_db
def test_an_unknown_decision_type_is_422_and_creates_nothing(client, key_factory,
                                                             monkeypatch):
    monkeypatch.setattr(decisions, "process", lambda *a, **k: {"id": 0, "status": "x"})
    raw, key_id = key_factory()

    before = query("SELECT count(*) AS n FROM decisions")[0]["n"]
    r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                    json={"email": "x@type.invalid", "decision_type": "renewal_risk"})

    assert r.status_code == 422, r.text
    assert "renewal_risk" in r.text
    assert query("SELECT count(*) AS n FROM decisions")[0]["n"] == before, (
        "a decision was filed for a type nothing can score"
    )
    # Refused at the edge, so the rate limit was never charged either.
    used = query("SELECT count FROM api_key_rate_limit WHERE api_key_id = %s",
                 (key_id,))
    assert not used or used[0]["count"] == 0


@needs_db
def test_the_default_type_is_applied_when_the_caller_says_nothing(client,
                                                                 key_factory,
                                                                 monkeypatch):
    monkeypatch.setattr(decisions, "process", lambda *a, **k: {"id": 0, "status": "x"})
    raw, _ = key_factory()

    r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                    json={"email": "default@type.invalid"})
    assert r.status_code == 202, r.text
    decision_id = r.json()["decision_id"]

    try:
        row = query("SELECT decision_type, trigger_input FROM decisions WHERE id = %s",
                    (decision_id,))[0]
        assert row["decision_type"] == "lead_qualification"
        # Stamped in the trigger_input too: the column is what the system reads,
        # this is the record of what was asked for.
        assert row["trigger_input"]["decision_type"] == "lead_qualification"
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (decision_id,))


@needs_db
def test_the_stamped_type_survives_into_the_persisted_row(client, key_factory,
                                                          monkeypatch):
    """Explicitly supplied, and readable off the row afterwards."""
    monkeypatch.setattr(decisions, "process", lambda *a, **k: {"id": 0, "status": "x"})
    raw, _ = key_factory()

    r = client.post("/decisions/trigger", headers={"X-API-Key": raw},
                    json={"email": "explicit@type.invalid",
                          "decision_type": "lead_qualification"})
    assert r.status_code == 202, r.text
    decision_id = r.json()["decision_id"]

    try:
        assert decisions.decision_type_of(decision_id, org_id=1) == "lead_qualification"
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (decision_id,))


@needs_db
def test_create_processing_stamps_the_type_at_birth():
    """Stamped with the identity and for the same reason — it selects the rules
    that score the row and the policy that gates it."""
    decision_id = decisions.create_processing(
        org_id=1, trigger_input={"email": "birth@type.invalid"},
        identity={"role": "sales", "org_id": 1, "bound_at": "loop_construction"},
    )
    try:
        row = query("SELECT decision_type, reasoning FROM decisions WHERE id = %s",
                    (decision_id,))[0]
        assert row["decision_type"] == "lead_qualification"
        assert row["reasoning"]["decision_type"] == "lead_qualification"
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (decision_id,))


@needs_db
def test_decision_type_of_falls_back_rather_than_raising_on_a_missing_row():
    """The gate must still get a policy key for a row that vanished mid-flight."""
    assert decisions.decision_type_of(999_999_999, org_id=1) == "lead_qualification"
