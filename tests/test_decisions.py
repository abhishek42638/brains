"""The single write path, and the rule that nothing is left in 'processing'.

'processing' is invisible to the human queue, so a row stuck there is gone: not
pending, not decided, and nobody waiting for it — the caller already has its
202. Master doc §2 is about automations that break silently; a decision that
evaporates because the loop died IS that failure, inside BRAINS itself.

So the tests here are mostly about the unhappy paths, and they assert on the
STORED ROW rather than on return values: what a reviewer can see is the only
thing that counts.
"""

import asyncio

import pytest
from psycopg.types.json import Json

import decisions
from agent import loop
from db import execute, query


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _db_ready(), reason="needs Postgres (docker compose up)"
)

IDENTITY = {"role": "sales", "org_id": 1, "bound_at": "loop_construction"}


@pytest.fixture
def decision_id():
    """A real row in 'processing', cleaned up afterwards."""
    did = decisions.create_processing(
        org_id=1, trigger_input={"email": "stub@test.invalid"}, identity=IDENTITY,
    )
    try:
        yield did
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (did,))


def _row(decision_id: int) -> dict:
    return query("SELECT * FROM decisions WHERE id = %s", (decision_id,))[0]


def _ok_run(**over):
    """A run that concluded properly, with a usable proposal."""
    run = {
        "email": "stub@test.invalid",
        "model": "stub-model",
        "identity": IDENTITY,
        "iterations": [{
            "index": 1, "intent": "", "tool": "score_lead", "args": {"lead_id": 1},
            "result": {"found": True, "score": 90, "band": "hot",
                       "rules_version": "1.0.0"},
            "is_error": False, "model_latency_ms": 1.0, "tool_latency_ms": 1.0,
        }],
        "final": {"index": 2, "intent": "", "stop_reason": "end_turn", "proposal": {}},
        "proposal": {"proposed_action": "nurture", "confidence": "low",
                     "rationale": "stub"},
        "stop_reason": "end_turn",
        "tool_calls": 1,
        "halted": None,
    }
    run.update(over)
    return run


def _patch_loop(monkeypatch, result=None, exc=None):
    async def fake(email, *, role, org_id):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(loop, "run_qualification", fake)


# --- The requested test: a dead loop must not strand the row ---------------- #

@needs_db
def test_a_raising_loop_lands_in_needs_review_not_processing(monkeypatch, decision_id):
    """THE test. Mock the loop to raise; the row must be reviewable, not stuck."""
    _patch_loop(monkeypatch, exc=RuntimeError("anthropic exploded"))

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    row = _row(decision_id)
    assert row["status"] == "needs_review", "a dead run must not stay in processing"
    assert row["status"] != "processing"
    assert "RuntimeError: anthropic exploded" in row["reasoning"]["error"]
    assert row["reasoning"]["identity"] == IDENTITY


@needs_db
def test_an_ordinary_exception_never_escapes_process(monkeypatch, decision_id):
    """The background task must not die and take the row with it."""
    _patch_loop(monkeypatch, exc=ValueError("bad json somewhere deep"))

    # No pytest.raises: the point is that this returns normally.
    result = decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")
    assert result["status"] == "needs_review"
    assert _row(decision_id)["status"] == "needs_review"


@needs_db
@pytest.mark.parametrize("exc", [
    KeyboardInterrupt("SIGINT mid-run"),
    SystemExit("SIGTERM mid-run"),
])
def test_an_interrupt_files_the_row_and_still_stops_the_process(
    monkeypatch, decision_id, exc,
):
    """A restart is one of the ways rows get stranded — and it isn't an Exception.

    `except Exception` misses KeyboardInterrupt/SystemExit entirely, so an
    operator restarting the API mid-run used to leave the row in 'processing'
    forever. We record it, then let the interrupt continue: filing the row must
    not turn a shutdown into a hang.
    """
    _patch_loop(monkeypatch, exc=exc)

    with pytest.raises(type(exc)):
        decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    assert _row(decision_id)["status"] == "needs_review", (
        "an interrupted run must still be reviewable"
    )


@needs_db
def test_partial_trace_survives_a_crash(monkeypatch, decision_id):
    """A run that died after real work must show how far it got.

    'It failed' and 'it failed after reading the CRM and scoring 90' are
    different reviews. loop.run_qualification attaches what it had.
    """
    boom = RuntimeError("died on turn 3")
    boom.partial_trace = [
        {"index": 1, "tool": "lookup_lead", "args": {"email": "x"},
         "result": {"found": True}, "is_error": False},
    ]
    boom.identity = IDENTITY
    _patch_loop(monkeypatch, exc=boom)

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    reasoning = _row(decision_id)["reasoning"]
    assert reasoning["iterations"], "the partial trace must be recorded"
    assert reasoning["iterations"][0]["tool"] == "lookup_lead"


# --- Bad JSON: the loop returns, but with nothing we can act on ------------- #

@needs_db
@pytest.mark.parametrize("proposal,why", [
    ({"parse_error": "JSONDecodeError: line 1", "raw": "not json"}, "unparseable"),
    ({"proposed_action": None}, "missing action"),
    ({"proposed_action": "delete_everything"}, "action outside the allowlist"),
])
def test_an_unusable_proposal_goes_to_review_not_a_default(
    monkeypatch, decision_id, proposal, why,
):
    """No proposal must ever be invented on the model's behalf.

    Both callers used to do `proposal.get("proposed_action") or "nurture"`,
    which files an action the model never chose as though it had — a silent
    break wearing a plausible face. A human sees it now.
    """
    _patch_loop(monkeypatch, result=_ok_run(proposal=proposal))

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    row = _row(decision_id)
    assert row["status"] == "needs_review", f"{why} must not be defaulted away"
    assert row["proposed_action"] != "nurture", "must not fabricate an action"
    assert row["proposed_action"] == decisions.NO_ACTION
    assert row["reasoning"]["error"]


@needs_db
def test_a_halted_loop_goes_to_review(monkeypatch, decision_id):
    """Hitting the iteration/tool cap is not a conclusion."""
    _patch_loop(monkeypatch, result=_ok_run(halted="max_iterations"))

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")
    assert _row(decision_id)["status"] == "needs_review"


@needs_db
def test_a_failed_run_keeps_its_trace_for_the_reviewer(monkeypatch, decision_id):
    """needs_review from a completed-but-unusable run keeps the full trace."""
    _patch_loop(monkeypatch, result=_ok_run(
        proposal={"parse_error": "bad json", "raw": "{oops"}))

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    reasoning = _row(decision_id)["reasoning"]
    assert reasoning["iterations"][0]["tool"] == "score_lead"
    assert reasoning["model"] == "stub-model"
    assert reasoning["identity"] == IDENTITY


# --- The happy path still works, through the same door ---------------------- #

@needs_db
def test_a_good_run_completes_through_the_gate(monkeypatch, decision_id):
    _patch_loop(monkeypatch, result=_ok_run())

    result = decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    row = _row(decision_id)
    assert row["status"] not in ("processing", "needs_review")
    assert row["proposed_action"] == "nurture"
    assert row["score"] == 90 and row["band"] == "hot"
    assert row["reasoning"]["gate"]["rule"]
    assert result["status"] == row["status"]
    # Provenance survives into the PERSISTED trace, not just the return value:
    # the row has to answer "which policy gated this?" long after the process
    # that decided it is gone, the same way rules_version answers "which rules
    # scored it?". Org 1 has no settings row, so this is the default policy.
    assert row["reasoning"]["gate"]["policy_source"] == "defaults"


@needs_db
def test_an_unreadable_policy_holds_the_row_and_says_so_in_the_trace(
    monkeypatch, decision_id
):
    """Fail closed, end to end: a bad migration must not relax the live gate."""
    from agent import gate

    # A CLEAN run: a company on file and no lost deals or open tickets, so no
    # blocker fires and the threshold is the only thing between score 90 and
    # auto_executed. Without this the row would be held by a blocker and the
    # test would pass without exercising the fail-closed path at all.
    clean = _ok_run()
    clean["iterations"] = [
        {"index": 1, "intent": "", "tool": "lookup_lead", "args": {},
         "result": {"found": True, "lead": {"id": 1},
                    "company": {"id": 1, "name": "Acme"}},
         "is_error": False, "model_latency_ms": 1.0, "tool_latency_ms": 1.0},
        {"index": 2, "intent": "", "tool": "check_crm", "args": {"lead_id": 1},
         "result": {"deals": [], "open_tickets": 0},
         "is_error": False, "model_latency_ms": 1.0, "tool_latency_ms": 1.0},
        *clean["iterations"],
    ]

    # Sanity: with settings readable, this exact run DOES auto-execute at 90.
    _patch_loop(monkeypatch, result=clean)
    probe = decisions.gather_evidence(clean)
    assert gate.propose({
        "score": probe["score"],
        "proposed_action": clean["proposal"]["proposed_action"],
        "evidence": probe["evidence"],
    })["status"] == "auto_executed", "the test's own premise is wrong"

    def boom(*a, **k):
        raise RuntimeError("relation \"org_settings\" does not exist")

    monkeypatch.setattr(gate, "query", boom)

    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    row = _row(decision_id)
    assert row["status"] == "pending_approval", (
        "an unreadable policy let a decision auto-execute"
    )
    trace = row["reasoning"]["gate"]
    assert trace["policy_source"] == "fallback_unreadable"
    assert trace["policy_error"] == "RuntimeError"
    assert trace["rule"] == "pending:policy_unreadable"


# --- A broken run must actually REACH a human ------------------------------- #

@needs_db
def test_needs_review_appears_in_the_human_queue(monkeypatch, decision_id):
    """The whole point: not pending == nobody looks == silent failure."""
    _patch_loop(monkeypatch, exc=RuntimeError("boom"))
    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    queue = query(
        "SELECT id, status FROM decisions WHERE org_id = %s AND status = ANY(%s)",
        (1, list(decisions.HUMAN_QUEUE_STATUSES)),
    )
    assert decision_id in [r["id"] for r in queue], (
        "a broken run that no human can see is the failure this system exists "
        "to prevent"
    )


@needs_db
def test_processing_is_never_in_the_human_queue():
    """A row mid-flight must NOT nag a human — which is why it can't rest there."""
    assert "processing" not in decisions.HUMAN_QUEUE_STATUSES


@needs_db
def test_api_pending_endpoint_serves_needs_review(monkeypatch, decision_id):
    """Same rule at the HTTP edge, not just in SQL.

    Goes through a real credential now: org comes from the key, so the endpoint
    cannot be called with an org at all.
    """
    from api import main
    from auth import Principal

    _patch_loop(monkeypatch, exc=RuntimeError("boom"))
    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    org1 = Principal(api_key_id=0, org_id=1, role="sales", name="t")
    rows = main.pending(principal=org1)
    assert decision_id in [d.id for d in rows]
    assert {d.id: d.status for d in rows}[decision_id] == "needs_review"


@needs_db
def test_pending_is_org_scoped_for_needs_review_too(monkeypatch, decision_id):
    """The rescue must not leak rows across tenants."""
    from api import main
    from auth import Principal

    _patch_loop(monkeypatch, exc=RuntimeError("boom"))
    decisions.process(decision_id, "stub@test.invalid", org_id=1, role="sales")

    org2 = Principal(api_key_id=0, org_id=2, role="sales", name="t")
    assert decision_id not in [d.id for d in main.pending(principal=org2)]


# --- One write path, structurally ------------------------------------------- #

def test_only_decisions_and_gate_write_to_the_table():
    """Regression guard: no caller may grow its own INSERT/UPDATE again.

    The CLI and the API each used to own SQL for this table, with different
    lifecycles. Keep the writers countable.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {"decisions.py", "agent/gate.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".venv", "tests/")) or rel in allowed:
            continue
        src = path.read_text()
        if re.search(r"(INSERT INTO|UPDATE)\s+decisions", src):
            offenders.append(rel)
    assert not offenders, f"decision writes leaked outside {allowed}: {offenders}"


def test_both_callers_use_the_shared_process():
    import inspect

    from agent import cli
    from api import main

    assert "decisions.process(" in inspect.getsource(cli.cmd_qualify)
    assert "decisions.process(" in inspect.getsource(main.internal_process)


# --- The failure try/except cannot catch: the process never comes back ------ #

@needs_db
def test_sweep_rescues_a_row_abandoned_by_a_killed_worker():
    """SIGKILL/OOM/power-loss run no handler — only an outside check finds these.

    Simulates the aftermath: a 'processing' row whose worker is never coming
    back. process()'s try/except is structurally incapable of covering this,
    which is why the sweeper exists.
    """
    did = decisions.create_processing(
        org_id=1, trigger_input={"email": "abandoned@test.invalid"},
        identity=IDENTITY,
    )
    try:
        # Backdate it past the threshold, as if the worker died 30m ago.
        execute("UPDATE decisions SET created_at = now() - interval '30 minutes' "
                "WHERE id = %s", (did,))

        swept = decisions.sweep_stuck(older_than_minutes=15)

        assert did in [r["id"] for r in swept]
        row = _row(did)
        assert row["status"] == "needs_review"
        assert row["reasoning"]["swept"] is True
        assert "abandoned" in row["reasoning"]["error"]
        # The binding stamped at birth survives the sweep — reasoning is merged,
        # not replaced, so the audit trail is not lost by the rescue.
        assert row["reasoning"]["identity"] == IDENTITY
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (did,))


@needs_db
def test_sweep_leaves_rows_that_are_still_being_worked_on(decision_id):
    """A young 'processing' row is in flight, not abandoned — do not touch it."""
    swept = decisions.sweep_stuck(older_than_minutes=15)

    assert decision_id not in [r["id"] for r in swept]
    assert _row(decision_id)["status"] == "processing"


@needs_db
def test_sweep_is_idempotent_and_org_scopeable():
    """Safe to run on a schedule; scopeable to one tenant."""
    did = decisions.create_processing(
        org_id=1, trigger_input={"email": "abandoned2@test.invalid"},
        identity=IDENTITY,
    )
    try:
        execute("UPDATE decisions SET created_at = now() - interval '30 minutes' "
                "WHERE id = %s", (did,))

        # Wrong tenant: not ours to rescue.
        assert did not in [r["id"] for r in decisions.sweep_stuck(org_id=2)]
        assert _row(did)["status"] == "processing"

        assert did in [r["id"] for r in decisions.sweep_stuck(org_id=1)]
        # Second pass finds nothing: already rescued, not swept twice.
        assert did not in [r["id"] for r in decisions.sweep_stuck(org_id=1)]
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (did,))


# --- needs_review is no longer a dead end ----------------------------------- #
#
# These rows 409 on approve/reject (there is no proposal to approve), so before
# retry/dismiss the queue could only grow. A queue that cannot be emptied stops
# being read, and an unread queue is the silent failure this system exists to
# prevent — reached by paperwork rather than by a crash.

@pytest.fixture
def failed_decision(monkeypatch):
    """A row genuinely in needs_review, via the real failure path."""
    did = decisions.create_processing(
        org_id=1, trigger_input={"email": "retry-me@test.invalid", "source": "test"},
        identity=IDENTITY,
    )
    _patch_loop(monkeypatch, exc=RuntimeError("transient blip"))
    decisions.process(did, "retry-me@test.invalid", org_id=1, role="sales")
    assert _row(did)["status"] == "needs_review"
    try:
        yield did
    finally:
        execute("DELETE FROM decisions WHERE id = %s", (did,))


@needs_db
def test_retry_and_dismiss_are_the_only_exits_from_needs_review(failed_decision):
    """approve/reject still refuse — retry/dismiss are why that's now acceptable."""
    from agent import gate

    assert gate.approve(failed_decision, "someone", 1)["ok"] is False
    assert gate.reject(failed_decision, "someone", 1)["ok"] is False
    assert _row(failed_decision)["status"] == "needs_review"


@needs_db
def test_retry_reruns_on_the_ORIGINAL_trigger_input(failed_decision, monkeypatch):
    """A retry re-executes the same decision, not a new one a client supplied."""
    result = decisions.begin_retry(failed_decision, org_id=1, role="sales")

    assert result["ok"] is True
    assert result["email"] == "retry-me@test.invalid", "must use the original input"
    assert _row(failed_decision)["status"] == "processing"


@needs_db
def test_retry_preserves_the_failed_attempt(failed_decision):
    """The evidence of an intermittent failure is the evidence worth keeping.

    Three recorded attempts is a story; one success that erased them is not.
    """
    decisions.begin_retry(failed_decision, org_id=1, role="sales")

    reasoning = _row(failed_decision)["reasoning"]
    attempts = reasoning["attempts"]
    assert len(attempts) == 1
    assert "transient blip" in attempts[0]["error"], "the failure must survive"

    # A second retry appends rather than replacing.
    decisions.needs_review(failed_decision, org_id=1, identity=IDENTITY,
                           error="failed again")
    decisions.begin_retry(failed_decision, org_id=1, role="sales")
    attempts = _row(failed_decision)["reasoning"]["attempts"]
    assert len(attempts) == 2, "attempt 2 must not erase attempt 1"
    assert "transient blip" in attempts[0]["error"]
    assert "failed again" in attempts[1]["error"]


@needs_db
def test_retry_then_success_lands_a_real_decision(failed_decision, monkeypatch):
    """The point of a retry: the row leaves the queue with a real proposal."""
    decisions.begin_retry(failed_decision, org_id=1, role="sales")
    _patch_loop(monkeypatch, result=_ok_run())
    decisions.process(failed_decision, "retry-me@test.invalid", org_id=1,
                      role="sales")

    row = _row(failed_decision)
    assert row["status"] not in ("processing", "needs_review")
    assert row["proposed_action"] == "nurture"
    assert row["reasoning"]["attempts"], "the earlier failure is still on file"


@needs_db
def test_retry_is_atomic_and_only_from_needs_review(failed_decision):
    """Two concurrent retries must not both start one."""
    assert decisions.begin_retry(failed_decision, org_id=1, role="sales")["ok"] is True
    # Now 'processing' — a second retry must lose.
    second = decisions.begin_retry(failed_decision, org_id=1, role="sales")
    assert second["ok"] is False
    assert "not needs_review" in second["error"]


@needs_db
def test_retry_is_org_scoped(failed_decision):
    assert decisions.begin_retry(failed_decision, org_id=2, role="sales")["ok"] is False
    assert _row(failed_decision)["status"] == "needs_review"


@needs_db
def test_dismiss_closes_the_row_with_a_note(failed_decision):
    result = decisions.dismiss(failed_decision, org_id=1, decided_by="abhishek",
                               note="duplicate lead, not worth a rerun")

    assert result["ok"] is True
    row = _row(failed_decision)
    assert row["status"] == "dismissed"
    assert row["decided_by"] == "abhishek"
    assert row["decided_at"] is not None
    assert row["reasoning"]["dismissal"]["note"] == "duplicate lead, not worth a rerun"
    assert row["reasoning"]["dismissal"]["by"] == "abhishek"
    # The failure it dismissed is still readable.
    assert "transient blip" in row["reasoning"]["error"]


@needs_db
def test_dismiss_is_atomic_and_terminal(failed_decision):
    assert decisions.dismiss(failed_decision, org_id=1, decided_by="a",
                             note="n")["ok"] is True
    second = decisions.dismiss(failed_decision, org_id=1, decided_by="b", note="n2")
    assert second["ok"] is False, "a dismissed row cannot be dismissed twice"
    assert _row(failed_decision)["decided_by"] == "a", "first writer wins"


@needs_db
def test_dismiss_is_org_scoped(failed_decision):
    assert decisions.dismiss(failed_decision, org_id=2, decided_by="attacker",
                             note="n")["ok"] is False
    assert _row(failed_decision)["status"] == "needs_review"


@needs_db
def test_resolved_rows_leave_the_human_queue(failed_decision):
    """The whole point: the queue must be emptyable."""
    from db import query as real_query

    def in_queue():
        rows = real_query(
            "SELECT id FROM decisions WHERE org_id = 1 AND status = ANY(%s)",
            (list(decisions.HUMAN_QUEUE_STATUSES),),
        )
        return failed_decision in [r["id"] for r in rows]

    assert in_queue(), "a broken run starts in the queue"
    decisions.dismiss(failed_decision, org_id=1, decided_by="a", note="n")
    assert not in_queue(), "a dismissed row must leave the queue"
