"""Unit tests for the approval gate in agent/gate.py.

Pure functions only — no DB, no API. `propose` is tested by passing plain
evidence dicts; the atomic-transition test mocks the db layer so no Postgres is
needed. This is the "code disposes" half of the system, so its policy and its
race-safety are what these tests pin down.
"""

import pytest

from agent import gate


def _evidence(has_company=True, lost_deals=0, open_tickets=0):
    return {
        "has_company": has_company,
        "lost_deals": lost_deals,
        "open_tickets": open_tickets,
    }


def _decision(score, proposed_action="route_to_sales", **ev):
    return {
        "score": score,
        "proposed_action": proposed_action,
        "evidence": _evidence(**ev),
    }


# --------------------------------------------------------------------------- #
# propose — the disposition policy                                            #
# --------------------------------------------------------------------------- #


def test_score_80_no_blockers_auto_executes():
    result = gate.propose({"score": 80, "evidence": _evidence()})
    assert result["status"] == "auto_executed"
    assert result["rule"] == "auto_execute:threshold_met"


def test_score_79_pending():
    result = gate.propose({"score": 79, "evidence": _evidence()})
    assert result["status"] == "pending_approval"
    assert result["rule"] == "pending:below_threshold"


def test_score_100_no_company_blocker_wins():
    result = gate.propose(
        {"score": 100, "evidence": _evidence(has_company=False)}
    )
    assert result["status"] == "pending_approval"
    assert result["rule"] == "blocker:no_company"
    assert "blocker:no_company" in result["blockers"]


def test_score_100_lost_deal_blocker_wins():
    result = gate.propose(
        {"score": 100, "evidence": _evidence(lost_deals=1)}
    )
    assert result["status"] == "pending_approval"
    assert result["rule"] == "blocker:lost_deal"
    assert "blocker:lost_deal" in result["blockers"]


def test_score_100_open_tickets_blocker_wins():
    result = gate.propose(
        {"score": 100, "evidence": _evidence(open_tickets=2)}
    )
    assert result["status"] == "pending_approval"
    assert result["rule"] == "blocker:open_tickets"
    assert "blocker:open_tickets" in result["blockers"]


def test_exact_threshold_boundary():
    # 80 auto-executes; one below does not. Pins the >= boundary.
    assert gate.propose(_decision(80))["status"] == "auto_executed"
    assert gate.propose(_decision(79))["status"] == "pending_approval"


# --------------------------------------------------------------------------- #
# propose — the auto-discard floor (autonomy at the bottom end)               #
# --------------------------------------------------------------------------- #


def test_score_0_discard_auto_discards():
    result = gate.propose(_decision(0, proposed_action="discard"))
    assert result["status"] == "auto_discarded"
    assert result["rule"] == "auto_discard:below_floor"


def test_score_0_route_proposal_gates_on_disagreement():
    # Floor says discard, model says route_to_sales -> the disagreement gates.
    result = gate.propose(_decision(0, proposed_action="route_to_sales"))
    assert result["status"] == "pending_approval"
    assert result["rule"] == "pending:floor_action_mismatch"


def test_score_25_pending_above_floor():
    # 25 is above the 20 floor -> not auto-discarded even with a discard proposal.
    result = gate.propose(_decision(25, proposed_action="discard"))
    assert result["status"] == "pending_approval"
    assert result["rule"] == "pending:below_threshold"


def test_score_0_discard_with_blocker_stays_pending():
    # Blockers win even over the auto-discard floor.
    result = gate.propose(
        _decision(0, proposed_action="discard", open_tickets=1)
    )
    assert result["status"] == "pending_approval"
    assert result["rule"] == "blocker:open_tickets"


def test_discard_floor_boundary():
    # 20 auto-discards (with a discard proposal); 21 does not.
    assert gate.propose(_decision(20, proposed_action="discard"))["status"] == "auto_discarded"
    assert gate.propose(_decision(21, proposed_action="discard"))["status"] == "pending_approval"


# --------------------------------------------------------------------------- #
# approve / reject — atomic transition, no read-then-write                    #
# --------------------------------------------------------------------------- #


def test_transition_returns_error_when_not_pending(monkeypatch):
    """UPDATE ... WHERE status='pending_approval' matches nothing -> [] rows.

    The gate must report a clear error, not raise, and not claim success.
    """
    calls = {}

    def fake_execute(sql, params=()):
        calls["sql"] = sql
        calls["params"] = params
        return []  # RETURNING id matched no row -> already decided / missing

    monkeypatch.setattr(gate, "execute", fake_execute)

    result = gate.approve(42, "alice")
    assert result["ok"] is False
    assert "not pending_approval" in result["error"]
    # And it really did guard on the pending status atomically in one UPDATE.
    assert "status='pending_approval'" in calls["sql"]
    assert "RETURNING id" in calls["sql"]
    assert calls["params"] == ("approved", "alice", 42)


def test_transition_succeeds_when_pending(monkeypatch):
    monkeypatch.setattr(gate, "execute", lambda sql, params=(): [{"id": 7}])
    result = gate.reject(7, "bob")
    assert result == {"ok": True, "id": 7, "status": "rejected"}
