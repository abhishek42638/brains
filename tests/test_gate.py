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


# --------------------------------------------------------------------------- #
# Per-org policy — propose() stays pure; the org is resolved outside it        #
# --------------------------------------------------------------------------- #
#
# The thirteen tests above pass a config of None and are deliberately untouched:
# the default policy has to keep behaving exactly as it did, or "configurable"
# would mean "changed for everyone".


def test_default_config_is_still_the_policy_when_none_is_passed():
    """The whole compatibility claim, stated once as its own assertion."""
    assert gate.propose(_decision(80)) == gate.propose(
        _decision(80), config=None
    )
    assert gate.propose(_decision(80), config=None)["status"] == "auto_executed"


def test_custom_thresholds_move_both_boundaries():
    strict = {"AUTO_EXECUTE_MIN_SCORE": 90, "AUTO_DISCARD_MAX_SCORE": 10}

    # 85 auto-executes under the default policy and does not under this one.
    assert gate.propose(_decision(85))["status"] == "auto_executed"
    assert gate.propose(_decision(85), config=strict)["status"] == "pending_approval"
    assert gate.propose(_decision(90), config=strict)["status"] == "auto_executed"

    # And the floor moved with it: 15 discards by default, not under this one.
    assert gate.propose(
        _decision(15, proposed_action="discard")
    )["status"] == "auto_discarded"
    assert gate.propose(
        _decision(15, proposed_action="discard"), config=strict
    )["status"] == "pending_approval"
    assert gate.propose(
        _decision(10, proposed_action="discard"), config=strict
    )["status"] == "auto_discarded"


def test_a_partial_config_leaves_the_other_threshold_alone():
    """Read-through per field: overriding one must not reset the other."""
    only_min = {"AUTO_EXECUTE_MIN_SCORE": 95}

    assert gate.propose(_decision(90), config=only_min)["status"] == "pending_approval"
    # The discard floor is untouched at the default 20.
    assert gate.propose(
        _decision(20, proposed_action="discard"), config=only_min
    )["status"] == "auto_discarded"


def test_custom_blockers_are_a_subset_of_the_registry():
    """An org that does not care about missing companies can say so."""
    tickets_only = {"BLOCKERS": gate.resolve_blockers(["blocker:open_tickets"])}

    # Default policy blocks this outright; this org's does not.
    assert gate.propose(
        _decision(100, has_company=False)
    )["status"] == "pending_approval"
    result = gate.propose(_decision(100, has_company=False), config=tickets_only)
    assert result["status"] == "auto_executed", result
    assert result["blockers"] == []

    # The one it kept still fires.
    still_blocked = gate.propose(_decision(100, open_tickets=1),
                                 config=tickets_only)
    assert still_blocked["rule"] == "blocker:open_tickets"


def test_an_org_can_block_on_nothing():
    """An empty blocker set is a real configuration, not a missing one."""
    result = gate.propose(
        _decision(100, has_company=False, lost_deals=3, open_tickets=9),
        config={"BLOCKERS": ()},
    )
    assert result["status"] == "auto_executed"
    assert result["blockers"] == []


def test_blocker_order_is_the_orgs_reported_precedence():
    """Order is configuration: the first fired blocker is the headline rule."""
    ev = _decision(100, has_company=False, open_tickets=1)

    default_first = gate.propose(ev)["rule"]
    assert default_first == "blocker:no_company"

    reordered = {"BLOCKERS": gate.resolve_blockers(
        ["blocker:open_tickets", "blocker:no_company"]
    )}
    result = gate.propose(ev, config=reordered)
    assert result["rule"] == "blocker:open_tickets"
    # Both still reported — only the precedence changed.
    assert set(result["blockers"]) == {"blocker:open_tickets",
                                       "blocker:no_company"}


def test_an_unknown_blocker_name_raises_rather_than_being_skipped():
    """A typo must not silently disable a block an admin thinks is on."""
    with pytest.raises(gate.UnknownBlocker) as excinfo:
        gate.resolve_blockers(["blocker:no_company", "blocker:typo"])
    assert "blocker:typo" in str(excinfo.value)
    # The error names what IS available, so the fix is obvious from the message.
    assert "blocker:no_company" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# gate_config_for — the only DB read in the policy path                       #
# --------------------------------------------------------------------------- #


def _db_ready() -> bool:
    try:
        from db import query as _q

        _q("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")


@pytest.fixture
def settings_for():
    """Write an org_settings row for a throwaway org id; clean it up after."""
    from db import execute as _execute

    made = []

    def make(org_id, *, min_score=None, max_discard=None, blockers=None):
        _execute("DELETE FROM org_settings WHERE org_id = %s", (org_id,))
        _execute(
            "INSERT INTO org_settings (org_id, auto_execute_min_score, "
            "auto_discard_max_score, blockers) VALUES (%s, %s, %s, %s)",
            (org_id, min_score, max_discard, blockers),
        )
        made.append(org_id)
        return org_id

    yield make
    for org_id in made:
        _execute("DELETE FROM org_settings WHERE org_id = %s", (org_id,))


@needs_db
def test_an_org_with_no_settings_row_gets_the_shipped_defaults():
    """The common case, and the one that must not change: no row, no change."""
    config = gate.gate_config_for(987654)
    assert config["AUTO_EXECUTE_MIN_SCORE"] == gate.CONFIG["AUTO_EXECUTE_MIN_SCORE"]
    assert config["AUTO_DISCARD_MAX_SCORE"] == gate.CONFIG["AUTO_DISCARD_MAX_SCORE"]
    assert config["BLOCKERS"] == gate.BLOCKERS
    assert gate.propose(_decision(80), config=config)["status"] == "auto_executed"


@needs_db
def test_an_org_with_custom_thresholds(settings_for):
    org = settings_for(987655, min_score=95, max_discard=5)
    config = gate.gate_config_for(org)

    assert config["AUTO_EXECUTE_MIN_SCORE"] == 95
    assert config["AUTO_DISCARD_MAX_SCORE"] == 5
    # Not configured -> still the default set, not an empty one.
    assert config["BLOCKERS"] == gate.BLOCKERS

    # End to end through the actual policy function.
    assert gate.propose(_decision(90), config=config)["status"] == "pending_approval"
    assert gate.propose(_decision(95), config=config)["status"] == "auto_executed"
    assert gate.propose(
        _decision(5, proposed_action="discard"), config=config
    )["status"] == "auto_discarded"
    assert gate.propose(
        _decision(15, proposed_action="discard"), config=config
    )["status"] == "pending_approval"


@needs_db
def test_the_fallback_is_per_field_not_per_row(settings_for):
    """A row setting one threshold must not freeze the other at a copy."""
    org = settings_for(987656, min_score=60)
    config = gate.gate_config_for(org)

    assert config["AUTO_EXECUTE_MIN_SCORE"] == 60
    assert config["AUTO_DISCARD_MAX_SCORE"] == gate.CONFIG["AUTO_DISCARD_MAX_SCORE"]
    assert gate.propose(_decision(60), config=config)["status"] == "auto_executed"


@needs_db
def test_an_org_with_custom_blockers(settings_for):
    org = settings_for(
        987657, blockers=["blocker:open_tickets", "blocker:lost_deal"]
    )
    config = gate.gate_config_for(org)

    assert [name for name, _ in config["BLOCKERS"]] == [
        "blocker:open_tickets", "blocker:lost_deal",
    ]

    # The dropped blocker no longer gates this org.
    assert gate.propose(
        _decision(100, has_company=False), config=config
    )["status"] == "auto_executed"
    # The kept ones do, in the org's stated precedence.
    both = gate.propose(_decision(100, lost_deals=1, open_tickets=1),
                        config=config)
    assert both["rule"] == "blocker:open_tickets"


@needs_db
def test_an_empty_blocker_array_is_distinct_from_null(settings_for):
    """NULL means 'not configured'; [] means 'block on nothing'."""
    null_org = settings_for(987658, min_score=50)
    assert gate.gate_config_for(null_org)["BLOCKERS"] == gate.BLOCKERS

    empty_org = settings_for(987659, blockers=[])
    assert gate.gate_config_for(empty_org)["BLOCKERS"] == ()
    assert gate.propose(
        _decision(100, has_company=False), config=gate.gate_config_for(empty_org)
    )["status"] == "auto_executed"


@needs_db
def test_org_settings_are_not_shared_between_orgs(settings_for):
    """One tenant's policy must not leak into another's."""
    strict = settings_for(987660, min_score=99)
    assert gate.gate_config_for(strict)["AUTO_EXECUTE_MIN_SCORE"] == 99
    assert gate.gate_config_for(987661)["AUTO_EXECUTE_MIN_SCORE"] == (
        gate.CONFIG["AUTO_EXECUTE_MIN_SCORE"]
    )


def test_an_unreadable_settings_table_fails_closed(monkeypatch):
    """A gate that cannot read its settings holds EVERYTHING for a human.

    Not the shipped defaults. The defaults are conservative only for an org that
    loosened its policy — for an org that tightened one they are a silent
    LOOSENING, which is the failure class this product exists to prevent. A full
    pending queue is visible and annoying; a quietly relaxed gate is invisible
    and wrong.
    """
    def boom(*a, **k):
        raise RuntimeError("relation \"org_settings\" does not exist")

    monkeypatch.setattr(gate, "query", boom)

    config = gate.gate_config_for(1)
    assert config["POLICY_SOURCE"] == gate.POLICY_UNREADABLE
    assert config["POLICY_ERROR"] == "RuntimeError"
    # Neither autonomy end exists under the fallback.
    assert config["AUTO_EXECUTE_MIN_SCORE"] is None
    assert config["AUTO_DISCARD_MAX_SCORE"] is None

    # A score that would sail past the default threshold is held anyway.
    result = gate.propose(_decision(100), config=config)
    assert result["status"] == "pending_approval"
    assert result["rule"] == "pending:policy_unreadable"
    assert result["policy_source"] == gate.POLICY_UNREADABLE
    assert result["policy_error"] == "RuntimeError"

    # So is one the default floor would have auto-discarded.
    discard = gate.propose(_decision(0, proposed_action="discard"), config=config)
    assert discard["status"] == "pending_approval"
    assert discard["rule"] == "pending:policy_unreadable"

    # Blockers are still evaluated and still reported — a fired blocker is
    # information the reviewer wants, and the outcome is pending either way.
    blocked = gate.propose(_decision(100, has_company=False), config=config)
    assert blocked["status"] == "pending_approval"
    assert blocked["rule"] == "blocker:no_company"
    assert blocked["policy_source"] == gate.POLICY_UNREADABLE


def test_a_stricter_org_is_never_silently_loosened_by_a_failed_read(monkeypatch,
                                                                    settings_for):
    """The exact case that decided the trade: min_score=95, read fails, 96 in.

    Under a fall-back-to-defaults gate this auto-executes at 80 — the org's own
    threshold silently relaxed by 15 points, with nothing on the row to say so.
    """
    org = settings_for(987662, min_score=95)

    # Sanity: while readable, 96 auto-executes under the org's OWN policy.
    healthy = gate.gate_config_for(org)
    assert healthy["POLICY_SOURCE"] == gate.POLICY_ORG_SETTINGS
    assert gate.propose(_decision(96), config=healthy)["status"] == "auto_executed"

    def boom(*a, **k):
        raise RuntimeError("relation \"org_settings\" does not exist")

    monkeypatch.setattr(gate, "query", boom)

    config = gate.gate_config_for(org)
    result = gate.propose(_decision(96), config=config)

    assert result["status"] == "pending_approval", (
        "a failed settings read silently loosened a stricter org's gate"
    )
    assert result["policy_source"] == "fallback_unreadable"
    assert result["rule"] == "pending:policy_unreadable"


# --------------------------------------------------------------------------- #
# Policy provenance — which policy gated this row?                            #
# --------------------------------------------------------------------------- #


def test_every_gate_result_names_its_policy_source():
    """Same job as rules_version: turn an argument about a ruling into a query."""
    for d in (_decision(100), _decision(0, proposed_action="discard"),
              _decision(50), _decision(0), _decision(100, has_company=False)):
        assert gate.propose(d)["policy_source"] == gate.POLICY_DEFAULTS

    # And no policy_error key when there was no failure to report.
    assert "policy_error" not in gate.propose(_decision(100))


@needs_db
def test_policy_source_distinguishes_configured_from_default(settings_for):
    configured = settings_for(987663, min_score=70)
    assert gate.gate_config_for(configured)["POLICY_SOURCE"] == (
        gate.POLICY_ORG_SETTINGS
    )
    assert gate.propose(
        _decision(75), config=gate.gate_config_for(configured)
    )["policy_source"] == "org_settings"

    assert gate.gate_config_for(987664)["POLICY_SOURCE"] == gate.POLICY_DEFAULTS


@needs_db
def test_a_row_that_overrides_nothing_reports_defaults(settings_for):
    """Provenance names the policy that APPLIED, and an all-NULL row applied none."""
    org = settings_for(987665)  # row exists; every column NULL
    config = gate.gate_config_for(org)
    assert config["POLICY_SOURCE"] == gate.POLICY_DEFAULTS
    assert config["AUTO_EXECUTE_MIN_SCORE"] == gate.CONFIG["AUTO_EXECUTE_MIN_SCORE"]
