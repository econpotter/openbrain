-- Semantic search over thoughts.

CREATE OR REPLACE FUNCTION match_thoughts(
    query_embedding VECTOR(768),
    match_threshold FLOAT DEFAULT 0.5,
    match_count     INT   DEFAULT 10,
    filter          JSONB DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    id UUID,
    content TEXT,
    metadata JSONB,
    similarity FLOAT,
    created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE AS $$
    SELECT t.id, t.content, t.metadata,
           1 - (t.embedding <=> query_embedding) AS similarity,
           t.created_at
    FROM   thoughts t
    WHERE  t.embedding IS NOT NULL
      AND  1 - (t.embedding <=> query_embedding) > match_threshold
      AND  (filter = '{}'::jsonb OR t.metadata @> filter)
    ORDER  BY t.embedding <=> query_embedding
    LIMIT  match_count;
$$;

-- Semantic search over document chunks, returning library metadata.
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding VECTOR(768),
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

-- Semantic search over people.
CREATE OR REPLACE FUNCTION match_people(
    query_embedding VECTOR(768),
    match_threshold FLOAT DEFAULT 0.3,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (
    id UUID,
    name TEXT,
    affiliation TEXT,
    relationship TEXT,
    notes TEXT,
    similarity FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT id, name, affiliation, relationship, notes,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   people
    WHERE  embedding IS NOT NULL
      AND  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;

-- Semantic search over projects.
CREATE OR REPLACE FUNCTION match_projects(
    query_embedding VECTOR(768),
    match_threshold FLOAT DEFAULT 0.3,
    match_count     INT   DEFAULT 10
)
RETURNS TABLE (
    id UUID,
    name TEXT,
    description TEXT,
    status TEXT,
    similarity FLOAT
)
LANGUAGE sql STABLE AS $$
    SELECT id, name, description, status,
           1 - (embedding <=> query_embedding) AS similarity
    FROM   projects
    WHERE  embedding IS NOT NULL
      AND  1 - (embedding <=> query_embedding) > match_threshold
    ORDER  BY similarity DESC
    LIMIT  match_count;
$$;

-- Structured connection discovery adapted from OB1:
-- schemas/enhanced-thoughts/schema.sql. openbrain also compares tags and
-- projects because its capture path stores both as first-class metadata.
CREATE OR REPLACE FUNCTION get_thought_connections(
    p_thought_id UUID,
    p_limit INT DEFAULT 20,
    p_exclude_restricted BOOLEAN DEFAULT true
)
RETURNS TABLE (
    id UUID,
    type TEXT,
    importance SMALLINT,
    preview TEXT,
    created_at TIMESTAMPTZ,
    shared_topics TEXT[],
    shared_tags TEXT[],
    shared_people TEXT[],
    shared_projects TEXT[],
    overlap_count INT
)
LANGUAGE sql
STABLE
AS $$
    WITH source AS (
        SELECT metadata
        FROM thoughts
        WHERE thoughts.id = p_thought_id
    ),
    source_values AS (
        SELECT key, value
        FROM source
        CROSS JOIN LATERAL (
            VALUES
                ('topics'),
                ('tags'),
                ('people'),
                ('projects')
        ) AS keys(key)
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(source.metadata->keys.key) = 'array'
                    THEN source.metadata->keys.key
                ELSE '[]'::jsonb
            END
        ) AS elements(value)
    ),
    candidate_values AS (
        SELECT t.id, keys.key, elements.value
        FROM thoughts t
        CROSS JOIN LATERAL (
            VALUES
                ('topics'),
                ('tags'),
                ('people'),
                ('projects')
        ) AS keys(key)
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(t.metadata->keys.key) = 'array'
                    THEN t.metadata->keys.key
                ELSE '[]'::jsonb
            END
        ) AS elements(value)
        WHERE t.id <> p_thought_id
          AND (NOT p_exclude_restricted OR t.sensitivity_tier <> 'restricted')
    ),
    shared_values AS (
        SELECT candidate_values.id, candidate_values.key, candidate_values.value
        FROM candidate_values
        JOIN source_values USING (key, value)
        GROUP BY candidate_values.id, candidate_values.key, candidate_values.value
    ),
    aggregated AS (
        SELECT
            shared_values.id,
            array_agg(shared_values.value ORDER BY shared_values.value)
                FILTER (WHERE shared_values.key = 'topics') AS shared_topics,
            array_agg(shared_values.value ORDER BY shared_values.value)
                FILTER (WHERE shared_values.key = 'tags') AS shared_tags,
            array_agg(shared_values.value ORDER BY shared_values.value)
                FILTER (WHERE shared_values.key = 'people') AS shared_people,
            array_agg(shared_values.value ORDER BY shared_values.value)
                FILTER (WHERE shared_values.key = 'projects') AS shared_projects,
            count(*)::int AS overlap_count
        FROM shared_values
        GROUP BY shared_values.id
    )
    SELECT
        t.id,
        t.type,
        t.importance,
        left(t.content, 200) AS preview,
        t.created_at,
        COALESCE(aggregated.shared_topics, '{}'::text[]),
        COALESCE(aggregated.shared_tags, '{}'::text[]),
        COALESCE(aggregated.shared_people, '{}'::text[]),
        COALESCE(aggregated.shared_projects, '{}'::text[]),
        aggregated.overlap_count
    FROM aggregated
    JOIN thoughts t USING (id)
    ORDER BY aggregated.overlap_count DESC, t.created_at DESC
    LIMIT p_limit;
$$;

-- Aggregate statistics adapted from OB1:
-- schemas/enhanced-thoughts/schema.sql. openbrain adds library totals, tags,
-- source types, and sensitivity tiers for its broader core schema.
CREATE OR REPLACE FUNCTION brain_stats_aggregate()
RETURNS JSONB
LANGUAGE sql
STABLE
AS $$
    WITH type_counts AS (
        SELECT type AS key, count(*) AS value
        FROM thoughts
        GROUP BY type
        ORDER BY value DESC, key
    ),
    tag_counts AS (
        SELECT tag AS key, count(*) AS value
        FROM thoughts
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(metadata->'tags') = 'array'
                    THEN metadata->'tags'
                ELSE '[]'::jsonb
            END
        ) AS tags(tag)
        GROUP BY tag
        ORDER BY value DESC, key
        LIMIT 10
    ),
    topic_counts AS (
        SELECT topic AS key, count(*) AS value
        FROM thoughts
        CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(metadata->'topics') = 'array'
                    THEN metadata->'topics'
                ELSE '[]'::jsonb
            END
        ) AS topics(topic)
        GROUP BY topic
        ORDER BY value DESC, key
        LIMIT 10
    ),
    source_counts AS (
        SELECT source_type AS key, count(*) AS value
        FROM thoughts
        GROUP BY source_type
        ORDER BY value DESC, key
    ),
    sensitivity_counts AS (
        SELECT sensitivity_tier AS key, count(*) AS value
        FROM thoughts
        GROUP BY sensitivity_tier
        ORDER BY value DESC, key
    )
    SELECT jsonb_build_object(
        'thoughts_total', (SELECT count(*) FROM thoughts),
        'thoughts_last_24h', (
            SELECT count(*)
            FROM thoughts
            WHERE created_at >= now() - interval '24 hours'
        ),
        'library_total', (SELECT count(*) FROM library),
        'types', COALESCE(
            (SELECT jsonb_object_agg(key, value) FROM type_counts),
            '{}'::jsonb
        ),
        'tags', COALESCE(
            (SELECT jsonb_object_agg(key, value) FROM tag_counts),
            '{}'::jsonb
        ),
        'topics', COALESCE(
            (SELECT jsonb_object_agg(key, value) FROM topic_counts),
            '{}'::jsonb
        ),
        'sources', COALESCE(
            (SELECT jsonb_object_agg(key, value) FROM source_counts),
            '{}'::jsonb
        ),
        'sensitivity_tiers', COALESCE(
            (SELECT jsonb_object_agg(key, value) FROM sensitivity_counts),
            '{}'::jsonb
        )
    );
$$;
