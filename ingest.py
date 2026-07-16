"""Ingest docs/knowledge/*.md into the permission-aware knowledge base.

Each markdown doc carries a YAML-ish frontmatter block naming the roles allowed
to see it:

    ---
    permitted_roles: [sales, ops, admin]
    ---

We chunk the body, embed each chunk with Voyage, and store it with its org_id
and permitted_roles. Idempotent: re-running a doc DELETEs its existing chunks
first, so a doc's rows are replaced rather than duplicated.

Verified before writing (not from memory):
  - voyageai 0.5.0: Client().embed(texts, model, input_type, output_dimension)
    -> obj with .embeddings (list[list[float]]).
  - voyage-3.5 default dim 1024 (256/512/1024/2048 supported); we pin 1024 to
    match the vector(1024) column, and assert the returned length.
"""

import json
import os
import re
import sys
from pathlib import Path

import voyageai

from db import execute

KNOWLEDGE_DIR = Path(__file__).parent / "docs" / "knowledge"
MODEL = "voyage-3.5"
DIM = 1024
MAX_CHUNK_CHARS = 600


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


def ingest_file(client: voyageai.Client, path: Path, org_id: int = 1) -> dict:
    roles, body = parse_doc(path.read_text())
    chunks = chunk_markdown(body)
    doc_name = path.name

    embeddings = client.embed(
        chunks, model=MODEL, input_type="document", output_dimension=DIM
    ).embeddings
    if embeddings and len(embeddings[0]) != DIM:
        raise ValueError(
            f"embedding dim {len(embeddings[0])} != expected {DIM} — "
            "vector(N) column and model output disagree"
        )

    # Idempotent replace: drop this doc's old chunks, then insert fresh ones.
    execute(
        "DELETE FROM knowledge_chunks WHERE org_id = %s AND doc_name = %s",
        (org_id, doc_name),
    )
    for chunk, emb in zip(chunks, embeddings):
        execute(
            "INSERT INTO knowledge_chunks "
            "(org_id, doc_name, chunk_text, embedding, permitted_roles) "
            "VALUES (%s, %s, %s, %s::vector, %s)",
            (org_id, doc_name, chunk, json.dumps(emb), roles),
        )
    return {"doc_name": doc_name, "chunks": len(chunks), "permitted_roles": roles}


def main() -> int:
    if not os.environ.get("VOYAGE_API_KEY"):
        print("error: VOYAGE_API_KEY is not set (put it in .env).", file=sys.stderr)
        return 2
    client = voyageai.Client()  # reads VOYAGE_API_KEY from the environment

    paths = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not paths:
        print(f"no markdown docs in {KNOWLEDGE_DIR}", file=sys.stderr)
        return 1

    print(f"ingesting {len(paths)} docs from {KNOWLEDGE_DIR} "
          f"(model={MODEL}, dim={DIM})")
    for path in paths:
        info = ingest_file(client, path)
        print(f"  {info['doc_name']:<24} {info['chunks']:>2} chunks  "
              f"roles={info['permitted_roles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
