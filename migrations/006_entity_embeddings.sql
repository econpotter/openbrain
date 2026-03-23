-- Add semantic search support to people and projects

ALTER TABLE people    ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);
ALTER TABLE projects  ADD COLUMN IF NOT EXISTS embedding VECTOR(1024);

CREATE INDEX IF NOT EXISTS people_embedding_idx
    ON people USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

CREATE INDEX IF NOT EXISTS projects_embedding_idx
    ON projects USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

CREATE OR REPLACE FUNCTION match_people(
    query_embedding VECTOR(1024),
    match_threshold FLOAT DEFAULT 0.3,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (id UUID, name TEXT, affiliation TEXT, relationship TEXT, notes TEXT, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, name, affiliation, relationship, notes,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   people
    WHERE  embedding IS NOT NULL
      AND  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;

CREATE OR REPLACE FUNCTION match_projects(
    query_embedding VECTOR(1024),
    match_threshold FLOAT DEFAULT 0.3,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (id UUID, name TEXT, description TEXT, status TEXT, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, name, description, status,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   projects
    WHERE  embedding IS NOT NULL
      AND  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;
