-- Fix embedding dimensions: voyage-large-2 produces 1536 dims, not 1024

-- Chunks
ALTER TABLE chunks ALTER COLUMN embedding TYPE VECTOR(1536);

-- Thoughts
ALTER TABLE thoughts ALTER COLUMN embedding TYPE VECTOR(1536);
DROP INDEX IF EXISTS thoughts_embedding_idx;
CREATE INDEX thoughts_embedding_idx ON thoughts USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- People
ALTER TABLE people ALTER COLUMN embedding TYPE VECTOR(1536);
DROP INDEX IF EXISTS people_embedding_idx;
CREATE INDEX people_embedding_idx ON people USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- Projects
ALTER TABLE projects ALTER COLUMN embedding TYPE VECTOR(1536);
DROP INDEX IF EXISTS projects_embedding_idx;
CREATE INDEX projects_embedding_idx ON projects USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- Recreate functions with correct dimensions
CREATE OR REPLACE FUNCTION match_thoughts(
    query_embedding VECTOR(1536),
    match_threshold FLOAT DEFAULT 0.5,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (id UUID, content TEXT, metadata JSONB, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, content, metadata,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   thoughts
    WHERE  embedding IS NOT NULL
      AND  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;

CREATE OR REPLACE FUNCTION match_people(
    query_embedding VECTOR(1536),
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
    query_embedding VECTOR(1536),
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

-- Recreate match_chunks with correct dimensions
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding VECTOR(1536),
    match_threshold FLOAT DEFAULT 0.5,
    match_count     INT   DEFAULT 10,
    year_min        INT   DEFAULT NULL
)
RETURNS TABLE (
    chunk_id    UUID,
    library_id  UUID,
    title       TEXT,
    authors     TEXT[],
    year        INT,
    chunk_text  TEXT,
    similarity  FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT
        c.id         AS chunk_id,
        l.id         AS library_id,
        l.title,
        l.authors,
        l.year,
        c.content    AS chunk_text,
        1 - (c.embedding <=> query_embedding) AS similarity
    FROM   chunks c
    JOIN   library l ON l.id = c.library_id
    WHERE  c.embedding IS NOT NULL
      AND  1 - (c.embedding <=> query_embedding) > match_threshold
      AND  (year_min IS NULL OR l.year >= year_min)
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;
