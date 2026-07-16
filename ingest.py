"""Ingest docs/knowledge/*.md into the permission-aware knowledge base.

Each markdown doc carries a YAML-ish frontmatter block naming the roles allowed
to see it:

    ---
    permitted_roles: [sales, ops, admin]
    ---

We chunk the body, embed each chunk with Voyage, and store it with its org_id
and permitted_roles.

SURVIVING THE RATE LIMIT
------------------------
Voyage's free tier is 3 requests/minute. Ingest makes one request per doc, so a
plain run trips a 429 partway through and dies — which used to leave the
knowledge base in a state nobody chose:

  - Each doc was replaced by DELETE-then-INSERT across separate autocommits, so
    a failure between them left the doc with SOME of its chunks, or none.
  - Docs are processed in sorted order, so a 429 on doc 3 left docs 1-2 freshly
    embedded and doc 3 stale — a half-updated KB with no error state and no way
    to tell by looking. Search kept working, on the wrong data.

Both are fixed here:

  - RATE LIMITER: requests are spaced to stay under the limit, so the common
    case is not to 429 at all. Prevention beats recovery.
  - BACKOFF: a 429 anyway is retried with exponential backoff + jitter, rather
    than failing the run.
  - ATOMIC PER DOC: the DELETE and its INSERTs run in ONE transaction. A doc is
    replaced completely or not at all — it can never be half-deleted.
  - EMBED BEFORE WRITE: a doc's chunks are embedded before its transaction
    opens, so a doc we cannot embed leaves its existing rows untouched rather
    than deleted-and-not-replaced.

A run that gives up partway therefore leaves every doc either fully updated or
exactly as it was. Re-running finishes the job.

Verified before writing (not from memory):
  - voyageai 0.5.0: Client().embed(texts, model, input_type, output_dimension)
    -> obj with .embeddings (list[list[float]]).
  - voyage-3.5 default dim 1024 (256/512/1024/2048 supported); we pin 1024 to
    match the vector(1024) column, and assert the returned length.
  - voyageai.error.RateLimitError is raised on 429 (observed against the live
    free tier, not assumed).
"""

import json
import random
import re
import sys
import time
from pathlib import Path

import voyageai

import config
from db import transaction

KNOWLEDGE_DIR = Path(__file__).parent / "docs" / "knowledge"
MODEL = "voyage-3.5"
DIM = 1024
MAX_CHUNK_CHARS = 600

# Voyage free tier: 3 requests/minute. Pace at 1 per 21s (~2.85/min) to stay
# under it with headroom, rather than sprinting into a 429 and recovering.
MIN_SECONDS_BETWEEN_REQUESTS = 21.0

# Retry budget for a 429 that gets through anyway (a shared key, a burst from
# elsewhere). 5 attempts with 5s doubling ~= 75s of waiting, which comfortably
# outlasts a 60s rate-limit window.
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 5.0


def parse_doc(text: str) -> tuple[list[str], str]:
    """Split a doc into (permitted_roles, body). Frontmatter is required."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("missing frontmatter block")
    frontmatter, body = m.group(1), m.group(2)
    roles_m = re.search(r"permitted_roles:\s*\[([^\]]*)\]", frontmatter)
    if not roles_m:
        raise ValueError("frontmatter missing permitted_roles")
    roles = [r.strip() for r in roles_m.group(1).split(",") if r.strip()]
    if not roles:
        raise ValueError("permitted_roles is empty")
    return roles, body.strip()


def chunk_markdown(body: str) -> list[str]:
    """Paragraph-pack chunks up to ~MAX_CHUNK_CHARS, respecting blank lines."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > MAX_CHUNK_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


class RateLimiter:
    """Space requests at least `min_interval` apart. Not thread-safe; not needed.

    The point is to not hit the limit in the first place. Backoff is what you do
    when prevention failed; a limiter is how prevention happens.
    """

    def __init__(self, min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS):
        self.min_interval = min_interval
        self._last: float | None = None

    def wait(self) -> float:
        """Sleep as needed before the next request. Returns seconds slept."""
        if self._last is None:
            self._last = time.monotonic()
            return 0.0
        elapsed = time.monotonic() - self._last
        slept = 0.0
        if elapsed < self.min_interval:
            slept = self.min_interval - elapsed
            time.sleep(slept)
        self._last = time.monotonic()
        return slept


def embed_with_backoff(
    client: voyageai.Client, texts: list[str], *, input_type: str = "document",
    limiter: RateLimiter | None = None, max_attempts: int = MAX_ATTEMPTS,
    sleep=None,
) -> list[list[float]]:
    """Embed, retrying a 429 with exponential backoff + jitter.

    Only RateLimitError is retried: it is the one error where the same request
    later is expected to work. A bad key or a malformed request would fail
    identically five times, so retrying those just delays the report.

    Jitter matters even here — without it, several ingest processes that trip
    the same limit would retry in lockstep and collide again on every attempt.

    `sleep` is injectable so tests exercise the backoff without waiting. It
    defaults to None and resolves to time.sleep at CALL time, not `sleep=time.sleep`
    in the signature: a default argument is evaluated once at import and frozen
    into the function object, so patching ingest.time.sleep afterwards would do
    nothing and the "gave up after 5 attempts" path would really sleep 75s.
    (Same trap as a module constant in a default — see auth.charge_trigger.)
    """
    sleep = time.sleep if sleep is None else sleep
    delay = BACKOFF_BASE_SECONDS
    for attempt in range(1, max_attempts + 1):
        if limiter is not None:
            limiter.wait()
        try:
            return client.embed(
                texts, model=MODEL, input_type=input_type, output_dimension=DIM,
            ).embeddings
        except voyageai.error.RateLimitError:
            if attempt == max_attempts:
                raise
            wait = delay + random.uniform(0, delay * 0.25)  # jitter
            print(f"    rate limited (attempt {attempt}/{max_attempts}); "
                  f"retrying in {wait:.1f}s", file=sys.stderr)
            sleep(wait)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def ingest_file(client: voyageai.Client, path: Path, org_id: int = 1,
                limiter: RateLimiter | None = None) -> dict:
    roles, body = parse_doc(path.read_text())
    chunks = chunk_markdown(body)
    doc_name = path.name

    # Embed FIRST, outside the transaction. If this raises (rate limit
    # exhausted, bad key), this doc's existing chunks are still in place — we
    # have not deleted anything we cannot replace.
    embeddings = embed_with_backoff(client, chunks, limiter=limiter)
    if embeddings and len(embeddings[0]) != DIM:
        raise ValueError(
            f"embedding dim {len(embeddings[0])} != expected {DIM} — "
            "vector(N) column and model output disagree"
        )

    # Replace atomically: DELETE + INSERTs commit together or not at all, so a
    # failure mid-write can never leave the doc with a subset of its chunks.
    with transaction() as cur:
        cur.execute(
            "DELETE FROM knowledge_chunks WHERE org_id = %s AND doc_name = %s",
            (org_id, doc_name),
        )
        for chunk, emb in zip(chunks, embeddings):
            cur.execute(
                "INSERT INTO knowledge_chunks "
                "(org_id, doc_name, chunk_text, embedding, permitted_roles) "
                "VALUES (%s, %s, %s, %s::vector, %s)",
                (org_id, doc_name, chunk, json.dumps(emb), roles),
            )
    return {"doc_name": doc_name, "chunks": len(chunks), "permitted_roles": roles}


def main() -> int:
    try:
        config.require("VOYAGE_API_KEY")
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    client = voyageai.Client()  # reads VOYAGE_API_KEY from the environment

    paths = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not paths:
        print(f"no markdown docs in {KNOWLEDGE_DIR}", file=sys.stderr)
        return 1

    print(f"ingesting {len(paths)} docs from {KNOWLEDGE_DIR} "
          f"(model={MODEL}, dim={DIM}, pacing={MIN_SECONDS_BETWEEN_REQUESTS}s/req)")
    limiter = RateLimiter()
    done = 0
    for path in paths:
        try:
            info = ingest_file(client, path, limiter=limiter)
        except voyageai.error.RateLimitError:
            # Out of retries. Everything already ingested is committed and
            # correct; this doc still has its previous chunks. Say exactly that
            # — a partial run must not look like a complete one.
            print(f"\nerror: gave up on {path.name} after {MAX_ATTEMPTS} rate-limited "
                  f"attempts.\n  {done}/{len(paths)} docs updated; the rest keep "
                  f"their existing chunks.\n  Nothing is half-written. Re-run to "
                  f"finish.", file=sys.stderr)
            return 2
        done += 1
        print(f"  {info['doc_name']:<24} {info['chunks']:>2} chunks  "
              f"roles={info['permitted_roles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
