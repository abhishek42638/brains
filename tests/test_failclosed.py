"""Emulation is an opt-in, never an absence.

Emulation skips OIDC verification on /internal/process. An earlier version
inferred it from "TASKS_QUEUE is unset", which meant auth on an internet-reachable
endpoint that spends Anthropic credits was disabled by MISSING config rather than
by anyone choosing it. `--set-env-vars` replaces the whole env block, so one
incomplete redeploy could have dropped the queue name and silently opened the
door — no error, no log line, a healthy-looking service.

That is the same bug class as the model naming its own role: a privilege granted
by absence instead of presence. Config that is missing must fail, not default to
trust.

Three layers, tested here:
  1. emulated() needs TASKS_EMULATED=1 — nothing else implies it.
  2. K_SERVICE (set by Cloud Run on every instance) overrides the flag entirely,
     so it cannot follow an image into production.
  3. assert_sane_config() refuses to BOOT rather than serve half-wired.
"""

import pytest
from fastapi.testclient import TestClient

import tasks


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start each test from a known-empty environment."""
    for name in ("K_SERVICE", "TASKS_EMULATED", *tasks.REQUIRED_IN_CLOUD):
        monkeypatch.delenv(name, raising=False)


# --- 1. Absence is not consent ---------------------------------------------- #

def test_emulation_requires_an_explicit_opt_in():
    """Missing config must NOT imply emulation — that was the hole."""
    assert tasks.emulated() is False, (
        "missing config enabled emulation: auth disabled by absence, not choice"
    )


def test_the_explicit_opt_in_works(monkeypatch):
    """...but asking for it plainly, off Cloud Run, is legitimate."""
    monkeypatch.setenv("TASKS_EMULATED", "1")
    assert tasks.emulated() is True


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "TRUE"])
def test_only_the_exact_flag_counts(monkeypatch, value):
    """No fuzzy truthiness: a typo must fail closed, not open."""
    monkeypatch.setenv("TASKS_EMULATED", value)
    assert tasks.emulated() is False, f"TASKS_EMULATED={value!r} enabled emulation"


# --- 2. A deployed service can never emulate -------------------------------- #

def test_a_deployed_service_can_never_emulate(monkeypatch):
    """K_SERVICE wins over TASKS_EMULATED, even set to 1.

    The flag cannot follow an image up: leaked in a .env, copied by a tired
    operator, inherited from a local shell — the answer is still False.
    """
    monkeypatch.setenv("TASKS_EMULATED", "1")
    monkeypatch.setenv("K_SERVICE", "brains-api")

    assert tasks.emulated() is False, (
        "a deployed service emulated because TASKS_EMULATED=1 reached it"
    )


def test_k_service_is_read_at_call_time(monkeypatch):
    """Not frozen at import — else the guard depends on import order."""
    assert tasks.on_cloud_run() is False
    monkeypatch.setenv("K_SERVICE", "brains-api")
    assert tasks.on_cloud_run() is True


# --- 3. Refuse to boot rather than serve half-wired ------------------------- #

def test_a_deployed_service_refuses_to_start_emulated(monkeypatch):
    """Not silently corrected at runtime — refused at boot, loudly.

    emulated() already returns False here, so the service would technically be
    safe. It still must not start: a deploy that asked for emulation is a mistake
    someone needs to hear about, not something to quietly paper over.
    """
    monkeypatch.setenv("K_SERVICE", "brains-api")
    monkeypatch.setenv("TASKS_EMULATED", "1")
    for name in tasks.REQUIRED_IN_CLOUD:
        monkeypatch.setenv(name, "set")

    with pytest.raises(tasks.ConfigError, match="TASKS_EMULATED"):
        tasks.assert_sane_config()


@pytest.mark.parametrize("missing", tasks.REQUIRED_IN_CLOUD)
def test_a_deployed_service_refuses_to_start_without_its_config(monkeypatch, missing):
    """Each required var is individually fatal in the cloud — no fallback."""
    monkeypatch.setenv("K_SERVICE", "brains-api")
    for name in tasks.REQUIRED_IN_CLOUD:
        monkeypatch.setenv(name, "set")
    monkeypatch.delenv(missing)

    with pytest.raises(tasks.ConfigError, match=missing):
        tasks.assert_sane_config()


def test_a_fully_configured_deployment_starts(monkeypatch):
    """The positive case — else the tests above pass vacuously."""
    monkeypatch.setenv("K_SERVICE", "brains-api")
    for name in tasks.REQUIRED_IN_CLOUD:
        monkeypatch.setenv(name, "set")

    tasks.assert_sane_config()  # must not raise


def test_local_startup_is_unaffected(monkeypatch):
    """Off Cloud Run there is nothing to assert; emulation is a real choice."""
    monkeypatch.setenv("TASKS_EMULATED", "1")
    tasks.assert_sane_config()  # must not raise


# --- The posture that results ----------------------------------------------- #

def test_internal_process_403s_when_not_emulated():
    """Non-emulated is now the DEFAULT, so this is what an unconfigured service does.

    Previously an unconfigured service was emulated => this call would have been
    accepted with no token at all.
    """
    from api.main import app

    r = TestClient(app).post("/internal/process", json={
        "decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"})
    assert r.status_code == 403, "an unauthenticated call to the internals got in"


def test_internal_sweep_403s_when_not_emulated():
    """Same for the cron door — a free way to make the DB work otherwise."""
    from api.main import app

    assert TestClient(app).post("/internal/sweep").status_code == 403


def test_an_api_key_is_not_a_key_to_the_internals():
    """A customer credential must not reach /internal/*, emulated or not."""
    from api.main import app

    r = TestClient(app).post(
        "/internal/process",
        json={"decision_id": 1, "email": "x@y.com", "org_id": 1, "role": "sales"},
        headers={"X-API-Key": "brains_sk_anything"})
    assert r.status_code == 403
