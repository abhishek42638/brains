"""Ingest: surviving the rate limit without corrupting the knowledge base.

Voyage's free tier is 3 req/min and ingest makes one request per doc, so a 429
mid-run is the normal case, not an edge case. Two things must hold when it
happens, and the second is the one that bites quietly:

  1. Retry with backoff, so a transient limit does not fail the run.
  2. A run that fails ANYWAY must leave every doc either fully updated or
     exactly as it was. The old code did DELETE-then-INSERT across separate
     autocommits, so a failure in between left a doc with some of its chunks —
     search kept working, on a subset of the data, with no error anywhere.

No live API calls here: the 429s are mocked and sleep is injected, so the
backoff is exercised in milliseconds.
"""

import pytest
import voyageai

import ingest


class FakeVoyage:
    """Fails the first `fail_times` calls with a 429, then succeeds."""

    def __init__(self, fail_times=0, dim=ingest.DIM, boom=None):
        self.fail_times = fail_times
        self.calls = 0
        self.dim = dim
        self.boom = boom

    def embed(self, texts, **kwargs):
        self.calls += 1
        if self.boom is not None and self.calls <= self.fail_times:
            raise self.boom
        if self.calls <= self.fail_times:
            raise voyageai.error.RateLimitError("429 rate limit")
        return type("R", (), {"embeddings": [[0.0] * self.dim for _ in texts]})()


@pytest.fixture
def slept():
    """Collect backoff durations instead of waiting them out."""
    waits = []
    return waits, waits.append


# --- Backoff ---------------------------------------------------------------- #

def test_a_transient_429_is_retried_and_succeeds(slept):
    waits, sleep = slept
    client = FakeVoyage(fail_times=2)

    out = ingest.embed_with_backoff(client, ["a", "b"], sleep=sleep)

    assert len(out) == 2
    assert client.calls == 3, "two failures then a success"
    assert len(waits) == 2, "slept once per retry"


def test_backoff_is_exponential_with_jitter(slept):
    waits, sleep = slept
    ingest.embed_with_backoff(FakeVoyage(fail_times=4), ["a"], sleep=sleep,
                              max_attempts=5)

    assert len(waits) == 4
    # Each wait is base*2^n plus up to 25% jitter — so strictly increasing, and
    # each within its own band rather than a fixed value.
    for i, w in enumerate(waits):
        base = ingest.BACKOFF_BASE_SECONDS * (2 ** i)
        assert base <= w <= base * 1.25, f"wait {i} = {w} outside [{base}, {base*1.25}]"
    assert waits == sorted(waits), "backoff must grow, not oscillate"


def test_jitter_actually_varies():
    """Without jitter, parallel ingests retry in lockstep and collide again."""
    seen = set()
    for _ in range(20):
        waits = []
        ingest.embed_with_backoff(FakeVoyage(fail_times=1), ["a"],
                                  sleep=waits.append)
        seen.add(waits[0])
    assert len(seen) > 1, "backoff waits are identical — jitter is not applied"


def test_retries_are_bounded_and_then_it_raises(slept):
    waits, sleep = slept
    client = FakeVoyage(fail_times=99)

    with pytest.raises(voyageai.error.RateLimitError):
        ingest.embed_with_backoff(client, ["a"], sleep=sleep, max_attempts=3)

    assert client.calls == 3, "must not retry forever"
    assert len(waits) == 2, "no sleep after the final attempt"


def test_only_rate_limits_are_retried(slept):
    """A bad key fails identically five times — retrying just delays the report."""
    waits, sleep = slept
    client = FakeVoyage(fail_times=1, boom=voyageai.error.AuthenticationError("bad key"))

    with pytest.raises(voyageai.error.AuthenticationError):
        ingest.embed_with_backoff(client, ["a"], sleep=sleep)

    assert client.calls == 1, "a non-429 must not be retried"
    assert waits == []


# --- Rate limiter ----------------------------------------------------------- #

def test_the_limiter_paces_requests(monkeypatch):
    """Prevention: stay under 3 RPM rather than sprinting into a 429."""
    now = [1000.0]
    slept = []
    monkeypatch.setattr(ingest.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ingest.time, "sleep", lambda s: (slept.append(s),
                                                        now.__setitem__(0, now[0] + s)))

    limiter = ingest.RateLimiter(min_interval=21.0)
    assert limiter.wait() == 0.0, "the first request must not wait"

    now[0] += 1.0          # only 1s has passed
    limiter.wait()
    assert slept and 19.9 < slept[-1] < 20.1, f"expected ~20s pause, got {slept}"

    now[0] += 100.0        # plenty of time has passed
    assert limiter.wait() == 0.0, "no pause when the interval has already elapsed"


def test_the_limiter_stays_under_voyages_free_tier():
    """3 requests/minute. The pacing must actually respect that, with headroom."""
    assert ingest.MIN_SECONDS_BETWEEN_REQUESTS >= 20.0, (
        f"{ingest.MIN_SECONDS_BETWEEN_REQUESTS}s/req exceeds 3/min"
    )


# --- The quiet one: no half-written docs ------------------------------------ #

def _db_ready() -> bool:
    try:
        from db import query
        query("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_ready(), reason="needs Postgres")


@pytest.fixture
def no_real_sleep(monkeypatch):
    """Never actually wait out a backoff in a test.

    ingest_file uses the module's time.sleep for its retries, so without this a
    'gave up after 5 attempts' test really sleeps 5+10+20+40s.
    """
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)


@pytest.fixture
def doc(tmp_path):
    # Paragraphs long enough that each becomes its OWN chunk (chunk_markdown
    # packs up to MAX_CHUNK_CHARS): short ones would pack into a single chunk
    # and the multi-INSERT behaviour under test would never happen.
    para = "x" * (ingest.MAX_CHUNK_CHARS - 100)
    p = tmp_path / "test-doc.md"
    p.write_text(
        "---\npermitted_roles: [sales, admin]\n---\n\n"
        + "\n\n".join(f"Chunk {i}. {para}" for i in range(4))
    )
    yield p
    from db import execute
    execute("DELETE FROM knowledge_chunks WHERE doc_name = %s", (p.name,))


def _chunks(doc_name):
    from db import query
    return query(
        "SELECT chunk_text FROM knowledge_chunks WHERE doc_name = %s ORDER BY id",
        (doc_name,),
    )


@needs_db
def test_a_failed_embed_leaves_the_existing_chunks_untouched(doc, no_real_sleep):
    """THE regression: a doc must never end up half-deleted.

    Embedding happens before the transaction opens, so a doc we cannot embed
    keeps the chunks it already had. The old order (DELETE, then embed/INSERT)
    destroyed the good data before it knew it could replace it.
    """
    ingest.ingest_file(FakeVoyage(), doc, org_id=1)
    before = _chunks(doc.name)
    assert before, "precondition: the doc is ingested"

    # Now every embed attempt fails.
    with pytest.raises(voyageai.error.RateLimitError):
        ingest.ingest_file(FakeVoyage(fail_times=99), doc, org_id=1)

    after = _chunks(doc.name)
    assert after == before, (
        "a failed re-ingest destroyed the doc's existing chunks — the KB is now "
        "missing data with no error state to show for it"
    )


@needs_db
def test_a_failed_insert_rolls_back_the_delete(doc, monkeypatch):
    """DELETE + INSERTs are one transaction: all of the doc, or none of it."""
    ingest.ingest_file(FakeVoyage(), doc, org_id=1)
    before = _chunks(doc.name)

    # Break the write half-way through, after the DELETE has run.
    real_transaction = ingest.transaction
    calls = {"n": 0}

    class ExplodingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 3:  # DELETE, INSERT, then boom
                raise RuntimeError("connection died mid-write")
            return self._cur.execute(*a, **k)

    import contextlib

    @contextlib.contextmanager
    def flaky():
        with real_transaction() as cur:
            yield ExplodingCursor(cur)

    monkeypatch.setattr(ingest, "transaction", flaky)

    with pytest.raises(RuntimeError, match="connection died"):
        ingest.ingest_file(FakeVoyage(), doc, org_id=1)

    assert _chunks(doc.name) == before, (
        "the DELETE committed without its INSERTs — the doc is half-written"
    )


@needs_db
def test_ingest_is_idempotent(doc):
    ingest.ingest_file(FakeVoyage(), doc, org_id=1)
    first = _chunks(doc.name)
    ingest.ingest_file(FakeVoyage(), doc, org_id=1)

    assert _chunks(doc.name) == first, "re-ingesting must replace, not duplicate"


@needs_db
def test_a_wrong_dimension_is_caught_before_it_is_stored(doc, no_real_sleep):
    """A vector(1024) column and a 512-d model must not agree to disagree."""
    with pytest.raises(ValueError, match="embedding dim"):
        ingest.ingest_file(FakeVoyage(dim=512), doc, org_id=1)
    assert not _chunks(doc.name), "nothing may be written on a dimension mismatch"
