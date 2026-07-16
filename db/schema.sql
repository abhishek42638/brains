-- Phase 3 (RAG): pgvector for embedding storage + similarity search.
CREATE EXTENSION IF NOT EXISTS vector;

-- Phase 5 (auth): the credential is now the source of identity.
--
-- This table closes the chain. org_id and role used to be trusted from the
-- request; they now come from whoever presented the key, and travel:
--     X-API-Key -> sha256 -> api_keys.(org_id, role) -> build_mcp(role, org_id)
-- The model never sees them, and neither does the request body.
--
-- key_hash holds sha256(raw key) ONLY. The raw key is printed once at creation
-- and never stored, so a dump of this table cannot be replayed against the API.
-- The hash is the lookup key, so it is UNIQUE.
--
-- Revocation is a timestamp, not a DELETE: a revoked key must still resolve for
-- audit ("which key triggered decision 41?") after it stops working.
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL,
    role TEXT NOT NULL,                -- 'sales' | 'ops' | 'admin' (server.KNOWN_ROLES)
    key_hash TEXT NOT NULL UNIQUE,     -- sha256 hex of the raw key; never the key
    name TEXT NOT NULL,                -- human label, e.g. 'demo sales key'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ             -- NULL = active; set = rejected with 401
);
CREATE INDEX IF NOT EXISTS api_keys_hash_idx ON api_keys (key_hash);

-- Phase 5 (rate limit): a fixed hourly window per key.
--
-- POST /decisions/trigger spends Anthropic credits on every call, so a public
-- URL needs a ceiling per credential, not per IP (an IP is not who you are).
-- One row per key, updated by a single atomic UPSERT that also rolls the
-- window over — no read-then-write, so concurrent triggers cannot both pass a
-- check and then both increment.
CREATE TABLE IF NOT EXISTS api_key_rate_limit (
    api_key_id INT PRIMARY KEY REFERENCES api_keys(id) ON DELETE CASCADE,
    window_start TIMESTAMPTZ NOT NULL,
    count INT NOT NULL
);

CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    industry TEXT,
    employee_count INT,
    annual_revenue_usd BIGINT,
    country TEXT,
    UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    email TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    company_id INT REFERENCES companies(id),
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (org_id, email)
);

CREATE TABLE IF NOT EXISTS deals (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    company_id INT REFERENCES companies(id),
    name TEXT,
    stage TEXT,
    amount_usd BIGINT,
    closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    company_id INT REFERENCES companies(id),
    subject TEXT,
    status TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisions (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    lead_id INT REFERENCES leads(id),
    trigger_input JSONB NOT NULL,      -- what kicked this off
    proposed_action TEXT NOT NULL,     -- 'route_to_sales'|'nurture'|'discard'
    score INT,
    band TEXT,
    rules_version TEXT,
    reasoning JSONB NOT NULL,          -- full trace: see below
    status TEXT NOT NULL DEFAULT 'pending_approval',
                                       -- 'pending_approval'|'approved'|'rejected'|'executed'
                                       -- |'auto_executed'|'auto_discarded'
                                       -- |'processing'    transient: a worker is on it.
                                       --   NOT visible to the human queue, so nothing
                                       --   may come to rest here — decisions.process()
                                       --   always moves it on, failure included.
                                       -- |'needs_review'  the run produced no decision
                                       --   worth trusting (crashed, or returned an
                                       --   unusable/absent proposal). Served from
                                       --   /decisions/pending next to pending_approval
                                       --   so a broken run reaches a human instead of
                                       --   dying quietly. See decisions.py.
    decided_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS decisions_org_status_idx ON decisions (org_id, status);

-- Phase 3 (RAG): permission-aware knowledge base. Each chunk carries the roles
-- allowed to see it; the permission filter lives in the SQL WHERE clause, never
-- in Python. vector(1024) matches Voyage voyage-3.5's default output dimension.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id SERIAL PRIMARY KEY,
    org_id INT NOT NULL DEFAULT 1,
    doc_name TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    permitted_roles TEXT[] NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- HNSW (not IVFFlat): builds on an empty table with no k-means training step,
-- so it survives a fresh schema apply before any rows exist, and gives better
-- recall. Cosine distance -> vector_cosine_ops, queried with the <=> operator.
CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
-- GIN over permitted_roles accelerates the `permitted_roles @> ARRAY[role]`
-- permission filter that runs on every search.
CREATE INDEX IF NOT EXISTS knowledge_chunks_roles_idx
    ON knowledge_chunks USING gin (permitted_roles);
CREATE INDEX IF NOT EXISTS knowledge_chunks_org_doc_idx
    ON knowledge_chunks (org_id, doc_name);