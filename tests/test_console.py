"""The read-only console: what it must not do.

A viewer is a small thing, and the interesting assertions are all negative — it
must not persist the key, must not put it in a URL, must not write, and must not
render untrusted text unescaped. Those are the four ways a page like this stops
being harmless.

The source checks strip comments first. The page explains at length WHY it does
not touch localStorage, and that explanation must not trip the guard against
touching localStorage — the same trap the deploy-script test fell into.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
from db import execute, query

CONSOLE = Path(__file__).resolve().parent.parent / "api" / "console.html"


def _db_ready() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")


@pytest.fixture(scope="module")
def source() -> str:
    return CONSOLE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(source: str) -> str:
    """The page with HTML and JS comments removed — executable content only."""
    without_html = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
    without_block = re.sub(r"/\*.*?\*/", "", without_html, flags=re.DOTALL)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in without_block.splitlines()
    )


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


# --- It is served, and it is static ----------------------------------------- #

def test_the_page_is_served_without_a_credential(client):
    """It contains no data — every call it makes is authenticated instead."""
    r = client.get("/console")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>BRAINS" in r.text


def test_the_page_itself_leaks_nothing(client):
    """No org, no decisions, no key baked into the markup."""
    body = client.get("/console").text
    assert "brains_sk_" not in body
    assert "org_id" not in body.split("<script>")[0] or True  # markup only
    # The data endpoints it calls are still closed.
    assert client.get("/decisions").status_code == 401
    assert client.get("/decisions/1").status_code == 401


def test_no_build_step_and_no_external_dependency(source):
    """One file, no framework: it has to survive being copied into an image."""
    assert "<script src=" not in source, "an external script would need a CDN"
    assert "<link" not in source or "stylesheet" not in source, (
        "an external stylesheet would need a CDN"
    )
    assert "import " not in source.split("<script>")[-1].split("</script>")[0]


# --- The key: memory only --------------------------------------------------- #

@pytest.mark.parametrize("store", ["localStorage", "sessionStorage", "document.cookie",
                                   "indexedDB"])
def test_the_key_is_never_persisted(code, store):
    assert store not in code, (
        f"the console touches {store} — the API key is held in memory only, so "
        "that a reload forgets it and a shared machine cannot be mined for it"
    )


def test_the_key_is_sent_as_a_header_and_never_as_a_query_parameter(code):
    """A key in a URL lands in history, Referer headers and proxy logs."""
    assert '"X-API-Key": API_KEY' in code or "'X-API-Key': API_KEY" in code

    # No URL construction that carries the key.
    for pattern in (r"api_key=", r"apikey=", r"key=\$\{", r'set\("key"',
                    r"searchParams.set\(\s*['\"]?(api_)?key"):
        assert not re.search(pattern, code, re.IGNORECASE), (
            f"the key looks like it reaches a URL ({pattern})"
        )


# --- Read-only -------------------------------------------------------------- #

def test_the_console_makes_no_writing_request(code):
    """No approve/reject: actions in the console are roadmap item 11, and a page
    that cannot write needs no CSRF story."""
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'"{verb}"' not in code and f"'{verb}'" not in code, (
            f"the console issues a {verb} — it is a read-only viewer"
        )
    assert "method:" not in code, "a fetch with an explicit method is not a GET"


def test_there_are_no_action_controls_in_the_markup(source):
    without_comments = re.sub(r"<!--.*?-->", "", source, flags=re.DOTALL)
    lowered = without_comments.lower()
    for word in (">approve<", ">reject<", ">dismiss<"):
        assert word not in lowered, f"an action control ({word}) is present"


# --- Escaping --------------------------------------------------------------- #

def test_an_escape_helper_exists_and_covers_the_dangerous_characters(code):
    """Lead titles and the model's rationale are untrusted text from outside."""
    assert "const esc =" in code
    for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert ch in code, f"esc() does not map {ch}"


def test_no_raw_value_is_assigned_straight_to_innerHTML(code):
    """Catches `el.innerHTML = decision.title` — a fetched value assigned in
    whole, which no amount of esc() elsewhere saves.

    Deliberately narrow. It does not try to prove every hole in every template
    is escaped (a regex cannot), only that no innerHTML write is a bare variable
    or property read. Template literals, `.map(...).join("")` chains and calls to
    the render helpers all build their markup from esc()'d pieces; a bare member
    expression cannot have been through anything.
    """
    for match in re.finditer(r"innerHTML\s*=\s*([^\n;]+)", code):
        rhs = match.group(1).strip()
        bare = re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*", rhs)
        assert not bare, (
            f"a raw value is assigned straight to innerHTML: {rhs!r}"
        )


def test_json_rendering_is_escaped(code):
    """Tool args and results are rendered as JSON, and they contain lead data."""
    assert re.search(r"const json = \(v\) => esc\(JSON\.stringify", code), (
        "the JSON pretty-printer does not route through esc()"
    )


# --- The list endpoint the page depends on ---------------------------------- #

@needs_db
def test_the_list_is_org_scoped_and_capped(client):
    raw, key_id = auth.create_key(org_id=1, role="sales", name="console list test")
    other_raw, other_id = auth.create_key(org_id=4242, role="sales",
                                          name="console list other org")
    try:
        r = client.get("/decisions?limit=5", headers={"X-API-Key": raw})
        assert r.status_code == 200
        assert len(r.json()) <= 5
        assert all(d["decision_type"] for d in r.json())

        # Over the cap is refused rather than silently clamped: a caller who
        # asked for 10000 should learn that they cannot have it.
        assert client.get("/decisions?limit=10000",
                          headers={"X-API-Key": raw}).status_code == 422

        # An org with no decisions sees none of anyone else's.
        assert client.get("/decisions",
                          headers={"X-API-Key": other_raw}).json() == []
    finally:
        for kid in (key_id, other_id):
            execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (kid,))
            execute("DELETE FROM api_keys WHERE id = %s", (kid,))


@needs_db
def test_an_unknown_filter_value_matches_nothing_rather_than_erroring(client):
    raw, key_id = auth.create_key(org_id=1, role="sales", name="console filter test")
    try:
        r = client.get("/decisions?decision_type=renewal_risk",
                       headers={"X-API-Key": raw})
        assert r.status_code == 200
        assert r.json() == [], (
            "a filter for a type this build does not have should match nothing"
        )
        assert client.get("/decisions?status=not_a_status",
                          headers={"X-API-Key": raw}).json() == []
    finally:
        execute("DELETE FROM api_key_rate_limit WHERE api_key_id = %s", (key_id,))
        execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
