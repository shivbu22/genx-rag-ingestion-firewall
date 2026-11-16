-- GenX Leadership — Bounded Dual-Stream RAG schema
-- Runs once on first boot of an empty pgdata volume
-- (mounted at /docker-entrypoint-initdb.d/init.sql).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Vector store: one row per chunk, for BOTH streams.
--   framework    = dense conceptual takeaway extracted from a coaching call
--   voice_sample = the coach's verbatim dialogue, chunked at 400-700 chars
-- Embeddings are 1024-dim (bge-m3). If you switch to a 1536-dim model
-- (e.g. OpenAI text-embedding-3-small), change vector(1024) below AND the
-- index, then recreate the table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_vault (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT        NOT NULL,
    stream_type TEXT        NOT NULL CHECK (stream_type IN ('framework', 'voice_sample')),
    content     TEXT        NOT NULL,
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_vault_session
    ON knowledge_vault (session_id);

CREATE INDEX IF NOT EXISTS idx_knowledge_vault_stream
    ON knowledge_vault (stream_type);

-- HNSW index for cosine distance (the <=> operator used by gated retrieval).
CREATE INDEX IF NOT EXISTS idx_knowledge_vault_embedding_hnsw
    ON knowledge_vault
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- Flat archive for sessions the ingestion firewall rejects.
-- Deliberately NO embedding column: non-coaching calls must never enter
-- the vector store, but they stay auditable / re-classifiable here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rejected_sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   TEXT        NOT NULL,
    meeting_type TEXT        NOT NULL,
    confidence   NUMERIC(4, 3),
    summary      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rejected_sessions_session
    ON rejected_sessions (session_id);
