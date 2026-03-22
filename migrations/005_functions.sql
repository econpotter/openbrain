-- Semantic search over thoughts
CREATE OR REPLACE FUNCTION match_thoughts(
    query_embedding VECTOR(1024),
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

-- Semantic search over document chunks, returning library metadata
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding VECTOR(1024),
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
