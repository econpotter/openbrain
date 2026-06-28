-- Causal-graph nodes: normalized cause/effect variables, merged across papers.
-- Resolution (raw cause/effect text -> a variable node) is a deferred maintenance
-- pass (see findings design §6); this table holds the canonical nodes it mints.
CREATE TABLE variables (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,             -- canonical display name
    norm_name   TEXT NOT NULL,             -- normalized key for exact-ish matching
    embedding   VECTOR(768),               -- for fuzzy resolution (same pattern as people/projects)
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX variables_embedding_idx ON variables USING hnsw (embedding vector_cosine_ops);
CREATE INDEX variables_norm_name_idx ON variables (norm_name);
