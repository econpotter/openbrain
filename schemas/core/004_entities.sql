-- Contacts, collaborators, and authors.

CREATE TABLE people (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT NOT NULL,
    affiliation    TEXT,
    relationship   TEXT,             -- collaborator | author | contact | advisor
    notes          TEXT,
    metadata       JSONB DEFAULT '{}',
    embedding      VECTOR(768),
    follow_up_date DATE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TRIGGER people_updated_at
    BEFORE UPDATE ON people
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX people_embedding_idx
    ON people USING hnsw (embedding vector_cosine_ops);

-- Research projects and initiatives.
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    start_date  DATE,
    metadata    JSONB DEFAULT '{}',
    embedding   VECTOR(768),
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TRIGGER projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX projects_embedding_idx
    ON projects USING hnsw (embedding vector_cosine_ops);

-- Generic relationships between records in any core table.
CREATE TABLE links (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_table  TEXT NOT NULL,
    source_id     UUID NOT NULL,
    target_table  TEXT NOT NULL,
    target_id     UUID NOT NULL,
    relationship  TEXT,           -- e.g. authored_by, relevant_to, cited_in
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source_table, source_id, target_table, target_id, relationship)
);

CREATE INDEX links_source_idx ON links (source_table, source_id);
CREATE INDEX links_target_idx ON links (target_table, target_id);
