"""A failed enqueue must not orphan the row.

The row is created BEFORE the task exists — an id has to exist to return in the
202. So if `enqueue_process` raises, there is a 'processing' row that nothing
will ever move: no task means no retry, no /internal/process, no needs_review.
The sweeper eventually rescues it, but only after STUCK_AFTER_MINUTES, and until
then a known-dead decision sits where no human can see it.

This is not hypothetical. On the first live deploy, `brains-run` was missing
iam.serviceAccounts.actAs on the tasks service account, so EVERY enqueue raised
PermissionDenied and every trigger left exactly such an orphan:

    google.api_core.exceptions.PermissionDenied: 403 The principal ... lacks IAM
    permission "iam.serviceAccounts.actAs" for the resource brains-tasks@...

The retry path is worse: begin_retry has already moved the row out of
needs_review, so a failed enqueue takes a row a human could see and hides it.
"""

import pytest
from fastapi.testclient import TestClient

import auth
import decisions
import tasks
from db import execute, query


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def key():
    raw, key_id = auth.create_key(org_id=1, role="sales", name="enqueue-fail test")
    yield raw
    execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (key_id,))
    execute("DELETE FROM api_keys WHERE id = %s", (key_id,))


@pytest.fixture
def broken_enqueue(monkeypatch):
    """Every enqueue raises, exactly as the missing actAs grant did."""
    def boom(*a, **k):
        raise PermissionError(
            'lacks IAM permission "iam.serviceAccounts.actAs"')

    monkeypatch.setattr(tasks, "enqueue_process", boom)


def _cleanup(decision_id):
    execute("DELETE FROM decisions WHERE id = %s", (decision_id,))


def test_a_failed_enqueue_parks_the_row_instead_of_orphaning_it(
    client, key, broken_enqueue,
):
    """THE regression: no row may be left in 'processing' with no task."""
    before = {r["id"] for r in query("SELECT id FROM decisions")}
    r = client.post("/decisions/trigger", headers={"X-API-Key": key},
                    json={"email": "mark@nimbushealth.com"})

    new = [x["id"] for x in query("SELECT id FROM decisions")
           if x["id"] not in before]
    assert len(new) == 1, "the row is created before the enqueue, by design"
    did = new[0]
    try:
        row = query("SELECT status, reasoning FROM decisions WHERE id = %s",
                    (did,))[0]
        assert row["status"] == "needs_review", (
            f"row left in {row['status']!r} with no task to move it — it would "
            "be invisible until the sweeper ran"
        )
        assert "actAs" in row["reasoning"]["error"], "the cause must be recorded"
        assert row["reasoning"]["identity"]["role"] == "sales"
    finally:
        _cleanup(did)


def test_the_caller_is_told_the_enqueue_failed(client, key, broken_enqueue):
    """A 202 would promise work that was never queued. 503 is the truth."""
    before = {r["id"] for r in query("SELECT id FROM decisions")}
    r = client.post("/decisions/trigger", headers={"X-API-Key": key},
                    json={"email": "mark@nimbushealth.com"})
    new = [x["id"] for x in query("SELECT id FROM decisions")
           if x["id"] not in before]
    try:
        assert r.status_code == 503, (
            "the caller must not get a 202 for work that was never enqueued"
        )
        assert "parked for review" in r.json()["detail"]
    finally:
        for did in new:
            _cleanup(did)


def test_a_parked_row_is_visible_to_a_human(client, key, broken_enqueue):
    """It has to reach the queue, not just leave 'processing'."""
    before = {r["id"] for r in query("SELECT id FROM decisions")}
    client.post("/decisions/trigger", headers={"X-API-Key": key},
                json={"email": "mark@nimbushealth.com"})
    new = [x["id"] for x in query("SELECT id FROM decisions")
           if x["id"] not in before]
    did = new[0]
    try:
        ids = [d["id"] for d in client.get("/decisions/pending",
                                           headers={"X-API-Key": key}).json()]
        assert did in ids, "a dead decision that no human can see is the failure"
    finally:
        _cleanup(did)


def test_a_failed_retry_enqueue_puts_the_row_back(client, key, monkeypatch):
    """Worse case: begin_retry already moved it OUT of needs_review.

    A failed enqueue here would take a row a human could see and hide it — the
    retry would leave things strictly worse than not retrying at all.
    """
    did = decisions.create_processing(
        org_id=1, trigger_input={"email": "mark@nimbushealth.com"},
        identity={"role": "sales", "org_id": 1, "bound_at": "loop_construction"},
    )
    decisions.needs_review(
        did, org_id=1,
        identity={"role": "sales", "org_id": 1, "bound_at": "loop_construction"},
        error="original failure",
    )
    try:
        def boom(*a, **k):
            raise PermissionError("enqueue is down")

        monkeypatch.setattr(tasks, "enqueue_process", boom)

        r = client.post(f"/decisions/{did}/retry", headers={"X-API-Key": key})

        assert r.status_code == 503
        row = query("SELECT status, reasoning FROM decisions WHERE id = %s",
                    (did,))[0]
        assert row["status"] == "needs_review", (
            f"a failed retry hid a reviewable row in {row['status']!r}"
        )
        assert "retry could not enqueue" in row["reasoning"]["error"]
        # The original failure is still on file under attempts.
        assert row["reasoning"].get("attempts"), "the first attempt was lost"
    finally:
        _cleanup(did)


def test_the_happy_path_still_returns_202(client, key, monkeypatch):
    """Guard against the fix swallowing the normal case."""
    monkeypatch.setattr(tasks, "enqueue_process", lambda *a, **k: "queued")

    before = {r["id"] for r in query("SELECT id FROM decisions")}
    r = client.post("/decisions/trigger", headers={"X-API-Key": key},
                    json={"email": "mark@nimbushealth.com"})
    new = [x["id"] for x in query("SELECT id FROM decisions")
           if x["id"] not in before]
    try:
        assert r.status_code == 202
        assert r.json()["status"] == "processing"
    finally:
        for did in new:
            _cleanup(did)
