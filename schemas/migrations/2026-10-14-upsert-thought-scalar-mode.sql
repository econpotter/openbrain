-- Bring a database created before 2026-07-01 up to the current upsert_thought.
--
-- Commit 8bb066c added p_scalar_mode to upsert_thought in schemas/core/002_thoughts.sql.
-- A database initialized earlier keeps the 4-argument version, and every
-- extract_thought_metadata call then fails with
--   function upsert_thought(unknown, unknown, jsonb, unknown, unknown) does not exist
-- so no thought is ever enriched.
--
-- The old version is dropped first: with both present, a 4-argument call would
-- match two functions. The body below is copied from schemas/core/002_thoughts.sql.
--
-- Run once:  docker compose exec -T postgres psql -U openbrain -d openbrain -v ON_ERROR_STOP=1 < <this file>
BEGIN;

DROP FUNCTION IF EXISTS upsert_thought(text, vector, jsonb, text);

CREATE OR REPLACE FUNCTION upsert_thought(
    p_content   TEXT,
    p_embedding VECTOR(768),
    p_metadata  JSONB DEFAULT '{}'::jsonb,
    p_type      TEXT DEFAULT NULL,
    p_scalar_mode TEXT DEFAULT 'max'
)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_fingerprint TEXT;
    v_id          UUID;
    v_inserted    BOOLEAN := false;
    v_metadata_changed BOOLEAN := false;
    v_metadata    JSONB;
    v_existing_metadata JSONB;
    v_type        TEXT;
    v_source_type TEXT;
    v_importance  SMALLINT;
    v_quality_score NUMERIC(5,2);
    v_sensitivity_tier TEXT;
    v_enriched    BOOLEAN;
    v_has_type    BOOLEAN;
    v_has_source_type BOOLEAN;
    v_has_importance BOOLEAN;
    v_has_quality_score BOOLEAN;
    v_has_sensitivity_tier BOOLEAN;
BEGIN
    IF p_scalar_mode NOT IN ('max', 'overwrite') THEN
        RAISE EXCEPTION 'upsert_thought: unknown p_scalar_mode %', p_scalar_mode;
    END IF;

    v_fingerprint := encode(sha256(convert_to(
        lower(trim(regexp_replace(p_content, '\s+', ' ', 'g'))),
        'UTF8'
    )), 'hex');

    v_metadata := COALESCE(p_metadata, '{}'::jsonb);
    v_has_type := NULLIF(p_type, '') IS NOT NULL
        OR NULLIF(v_metadata->>'type', '') IS NOT NULL;
    v_has_source_type := NULLIF(v_metadata->>'source_type', '') IS NOT NULL
        OR NULLIF(v_metadata->>'source', '') IS NOT NULL;
    v_has_importance := COALESCE(v_metadata->>'importance', '') ~ '^[0-9]+$';
    v_has_quality_score :=
        COALESCE(v_metadata->>'quality_score', '') ~ '^[0-9]+(\.[0-9]+)?$';
    v_has_sensitivity_tier :=
        v_metadata->>'sensitivity_tier' IN ('standard', 'personal', 'restricted');

    v_type := COALESCE(NULLIF(p_type, ''), NULLIF(v_metadata->>'type', ''),
                       'thought');
    v_source_type := COALESCE(NULLIF(v_metadata->>'source_type', ''),
                              NULLIF(v_metadata->>'source', ''), 'unknown');
    v_importance := CASE
        WHEN v_has_importance
            THEN LEAST(6, GREATEST(0, (v_metadata->>'importance')::numeric))::smallint
        ELSE 3
    END;
    v_quality_score := CASE
        WHEN v_has_quality_score
            THEN LEAST(100, GREATEST(0, (v_metadata->>'quality_score')::numeric))
        ELSE 50
    END;
    v_sensitivity_tier := CASE
        WHEN v_has_sensitivity_tier THEN v_metadata->>'sensitivity_tier'
        ELSE 'standard'
    END;
    v_enriched := CASE
        WHEN jsonb_typeof(v_metadata->'enriched') = 'boolean'
            THEN (v_metadata->>'enriched')::boolean
        ELSE false
    END;

    v_metadata := v_metadata || jsonb_build_object(
        'type', v_type,
        'source_type', v_source_type,
        'importance', v_importance,
        'quality_score', v_quality_score,
        'sensitivity_tier', v_sensitivity_tier,
        'enriched', v_enriched
    );

    INSERT INTO thoughts (
        content,
        content_fingerprint,
        embedding,
        metadata,
        type,
        source_type,
        importance,
        quality_score,
        sensitivity_tier,
        enriched
    )
    VALUES (
        p_content,
        v_fingerprint,
        p_embedding,
        v_metadata,
        v_type,
        v_source_type,
        v_importance,
        v_quality_score,
        v_sensitivity_tier,
        v_enriched
    )
    ON CONFLICT (content_fingerprint)
        WHERE content_fingerprint IS NOT NULL
        DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT metadata
        INTO v_existing_metadata
        FROM thoughts
        WHERE content_fingerprint = v_fingerprint;

        UPDATE thoughts
        SET metadata = merge_thought_metadata(metadata, v_metadata),
            type = CASE WHEN v_has_type THEN v_type ELSE type END,
            source_type = CASE
                WHEN v_has_source_type THEN v_source_type
                ELSE source_type
            END,
            importance = CASE
                WHEN v_has_importance AND p_scalar_mode = 'overwrite'
                    THEN v_importance
                WHEN v_has_importance THEN GREATEST(importance, v_importance)
                ELSE importance
            END,
            quality_score = CASE
                WHEN v_has_quality_score AND p_scalar_mode = 'overwrite'
                    THEN v_quality_score
                WHEN v_has_quality_score
                    THEN GREATEST(quality_score, v_quality_score)
                ELSE quality_score
            END,
            sensitivity_tier = CASE
                WHEN sensitivity_tier = 'restricted'
                     OR v_sensitivity_tier = 'restricted' THEN 'restricted'
                WHEN sensitivity_tier = 'personal'
                     OR v_sensitivity_tier = 'personal' THEN 'personal'
                ELSE 'standard'
            END,
            enriched = enriched OR v_enriched
        WHERE content_fingerprint = v_fingerprint
        RETURNING id INTO v_id;

        UPDATE thoughts
        SET metadata = metadata || jsonb_build_object(
            'type', type,
            'source_type', source_type,
            'importance', importance,
            'quality_score', quality_score,
            'sensitivity_tier', sensitivity_tier,
            'enriched', enriched
        )
        WHERE id = v_id;

        SELECT metadata IS DISTINCT FROM v_existing_metadata
        INTO v_metadata_changed
        FROM thoughts
        WHERE id = v_id;
    ELSE
        v_inserted := true;
        v_metadata_changed := true;
    END IF;

    RETURN jsonb_build_object(
        'id', v_id,
        'fingerprint', v_fingerprint,
        'inserted', v_inserted,
        'metadata_changed', v_metadata_changed
    );
END;
$$;

COMMIT;
