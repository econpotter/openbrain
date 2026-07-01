#!/usr/bin/env bash
# Run the clean schema against an ephemeral PostgreSQL + pgvector container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONTAINER_NAME="openbrain-schema-trial-${RANDOM}-${RANDOM}"
POSTGRES_PASSWORD="openbrain-trial-password"

cleanup() {
    docker rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed or is not on PATH."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Cannot access the Docker daemon."
    echo "Open a new login session after joining the docker group, then rerun:"
    echo "  bash openbrain/scripts/run-tests.sh --postgres"
    exit 1
fi

echo "Starting ephemeral PostgreSQL + pgvector container..."
docker run --detach \
    --name "$CONTAINER_NAME" \
    --env POSTGRES_USER=openbrain \
    --env POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    --env POSTGRES_DB=openbrain \
    --volume "$PROJECT_DIR/schemas/core:/docker-entrypoint-initdb.d:ro" \
    pgvector/pgvector:pg16 >/dev/null

echo "Waiting for PostgreSQL initialization..."
for _ in $(seq 1 90); do
    # The entrypoint applies every schema file against a temporary server and
    # then restarts. Gate on the "init process complete" marker so the probe
    # never races ahead of a later file (e.g. 005_search's match_thoughts) the
    # way a single-function check does, then wait for the real server to accept
    # connections.
    if docker logs "$CONTAINER_NAME" 2>&1 \
        | grep --quiet "PostgreSQL init process complete" \
       && docker exec "$CONTAINER_NAME" pg_isready \
        --username openbrain --dbname openbrain >/dev/null 2>&1; then
        break
    fi
    if [ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]; then
        break
    fi
    sleep 1
done

if ! docker exec "$CONTAINER_NAME" psql \
    --username openbrain \
    --dbname openbrain \
    --tuples-only \
    --command "SELECT to_regprocedure('upsert_thought(text,vector,jsonb,text,text)') IS NOT NULL
                 AND to_regproc('match_thoughts') IS NOT NULL;" \
    2>/dev/null | grep --quiet t; then
    echo "ERROR: PostgreSQL did not finish schema initialization."
    echo
    echo "Container logs:"
    docker logs "$CONTAINER_NAME" 2>&1 || true
    exit 1
fi

echo "Checking schema, duplicate merges, and search behavior..."
docker exec --interactive "$CONTAINER_NAME" \
    psql --username openbrain --dbname openbrain --set ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
    v_embedding VECTOR(768) := array_fill(1.0::real, ARRAY[768])::vector;
    v_distant_embedding VECTOR(768) :=
        array_fill(-1.0::real, ARRAY[768])::vector;
    v_first JSONB;
    v_duplicate JSONB;
    v_related JSONB;
    v_stats JSONB;
    v_library_id UUID;
    v_person_id UUID;
    v_project_id UUID;
    v_thought thoughts%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'vector extension is missing';
    END IF;

    IF to_regprocedure('upsert_thought(text,vector,jsonb,text,text)') IS NULL THEN
        RAISE EXCEPTION 'upsert_thought function is missing';
    END IF;

    v_first := upsert_thought(
        'A Trial Thought',
        v_embedding,
        '{"people":["Ada"],"tags":["baseline"]}'::jsonb,
        'note'
    );
    v_duplicate := upsert_thought(
        '  a   trial thought  ',
        v_embedding,
        '{"people":["Grace"],"tags":["follow-up"]}'::jsonb,
        NULL
    );

    IF NOT (v_first->>'inserted')::boolean THEN
        RAISE EXCEPTION 'first capture was not inserted';
    END IF;

    IF (v_duplicate->>'inserted')::boolean THEN
        RAISE EXCEPTION 'normalized duplicate created a second thought';
    END IF;

    IF NOT (v_duplicate->>'metadata_changed')::boolean THEN
        RAISE EXCEPTION 'duplicate metadata merge was not reported';
    END IF;

    SELECT *
    INTO v_thought
    FROM thoughts
    WHERE id = (v_first->>'id')::uuid;

    IF (SELECT count(*) FROM thoughts) <> 1 THEN
        RAISE EXCEPTION 'expected one thought after normalized duplicate capture';
    END IF;

    IF v_thought.type <> 'note' THEN
        RAISE EXCEPTION 'omitted duplicate type replaced existing classification';
    END IF;

    IF NOT v_thought.metadata @> '{"people":["Ada","Grace"]}'::jsonb THEN
        RAISE EXCEPTION 'people metadata was not merged';
    END IF;

    IF NOT v_thought.metadata @> '{"tags":["baseline","follow-up"]}'::jsonb THEN
        RAISE EXCEPTION 'tag metadata was not merged';
    END IF;

    -- Enriched-flag lifecycle + extraction merge precedence (Plan F). Two paths,
    -- both re-running the canonical upsert exactly as brain.extract_thought_metadata
    -- does. Test rows are removed afterwards so they do not perturb later aggregates.

    -- Reject an unknown scalar mode up front (fail loud, no silent fallback).
    BEGIN
        PERFORM upsert_thought(
            'A mode guard probe', v_embedding, '{}'::jsonb, NULL, 'bogus'
        );
        RAISE EXCEPTION 'invalid scalar mode was accepted';
    EXCEPTION
        WHEN others THEN
            IF SQLERRM NOT LIKE '%unknown p_scalar_mode%' THEN
                RAISE;
            END IF;
    END;

    -- Path A — sparse auto-capture (caller set no importance, so it seeds the
    -- default 3). The deferred extraction re-upserts in 'overwrite' scalar mode,
    -- so the model's importance lands verbatim even when it is BELOW the default:
    -- GREATEST would have pinned it back up and lost the signal (AC #1). Arrays
    -- union and enriched flips true.
    PERFORM upsert_thought(
        'A sparse auto-captured thought',
        v_embedding,
        '{"tags":["caller"]}'::jsonb,
        'thought'
    );
    IF (SELECT enriched FROM thoughts
        WHERE content = 'A sparse auto-captured thought') THEN
        RAISE EXCEPTION 'sparse capture was marked enriched before extraction';
    END IF;
    IF (SELECT importance FROM thoughts
        WHERE content = 'A sparse auto-captured thought') <> 3 THEN
        RAISE EXCEPTION 'sparse capture did not seed the default importance';
    END IF;

    PERFORM upsert_thought(
        'A sparse auto-captured thought',
        v_embedding,
        '{"enriched":true,"importance":1,"tags":["extracted"],"people":["Ada"]}'::jsonb,
        NULL,
        'overwrite'
    );
    SELECT *
    INTO v_thought
    FROM thoughts
    WHERE content = 'A sparse auto-captured thought';

    IF NOT v_thought.enriched THEN
        RAISE EXCEPTION 'extraction re-upsert did not set enriched=true';
    END IF;

    IF v_thought.importance <> 1 THEN
        RAISE EXCEPTION
            'overwrite mode did not let the extracted importance land: %',
            v_thought.importance;
    END IF;

    IF (v_thought.metadata->>'importance')::int <> 1 THEN
        RAISE EXCEPTION 'metadata importance was not synced to the extracted value';
    END IF;

    IF NOT v_thought.metadata @> '{"tags":["caller","extracted"]}'::jsonb THEN
        RAISE EXCEPTION 'extraction did not union caller and extracted tags';
    END IF;

    IF NOT v_thought.metadata @> '{"people":["Ada"]}'::jsonb THEN
        RAISE EXCEPTION 'extraction did not add the discovered person';
    END IF;

    DELETE FROM thoughts WHERE content = 'A sparse auto-captured thought';

    -- Path B — caller-set importance wins (AC #2). A deliberate low importance (2)
    -- must survive even when the model wants a higher one. brain.extract_thought_metadata
    -- strips importance from the extracted payload when importance_source='caller',
    -- so the overwrite-mode merge below never receives it and the caller's 2 stands.
    PERFORM upsert_thought(
        'A deliberately ephemeral thought',
        v_embedding,
        '{"importance":2,"importance_source":"caller","tags":["caller"]}'::jsonb,
        'thought'
    );
    PERFORM upsert_thought(
        'A deliberately ephemeral thought',
        v_embedding,
        '{"enriched":true,"tags":["extracted"],"people":["Ada"]}'::jsonb,
        NULL,
        'overwrite'
    );
    SELECT *
    INTO v_thought
    FROM thoughts
    WHERE content = 'A deliberately ephemeral thought';

    IF v_thought.importance <> 2 THEN
        RAISE EXCEPTION
            'extraction overrode the caller''s deliberate importance: %',
            v_thought.importance;
    END IF;

    IF NOT v_thought.enriched THEN
        RAISE EXCEPTION 'caller-importance extraction did not set enriched=true';
    END IF;

    IF NOT v_thought.metadata @> '{"tags":["caller","extracted"]}'::jsonb THEN
        RAISE EXCEPTION 'caller-importance extraction did not union tags';
    END IF;

    DELETE FROM thoughts WHERE content = 'A deliberately ephemeral thought';

    PERFORM upsert_thought(
        'A distant thought about archival policy',
        v_distant_embedding,
        '{"tags":["archive"]}'::jsonb,
        'note'
    );

    IF (SELECT count(*) FROM match_thoughts(v_embedding, 0.9, 10)) <> 1 THEN
        RAISE EXCEPTION 'thought vector search did not isolate the nearest match';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM thoughts
        WHERE fts @@ plainto_tsquery('english', 'archival policy')
          AND content = 'A distant thought about archival policy'
    ) THEN
        RAISE EXCEPTION 'thought full-text search index did not return the expected match';
    END IF;

    INSERT INTO library (
        title, authors, year, source, doc_type, filepath, file_hash
    )
    VALUES (
        'Trial Library Document',
        ARRAY['Ada Researcher'],
        2026,
        'Trial Journal',
        'paper',
        '/tmp/trial-library-document.txt',
        'trial-file-hash'
    )
    RETURNING id INTO v_library_id;

    BEGIN
        INSERT INTO library (title, filepath, file_hash)
        VALUES ('Duplicate Trial Document', '/tmp/duplicate.txt', 'trial-file-hash');
        RAISE EXCEPTION 'duplicate library file hash was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            NULL;
    END;

    INSERT INTO chunks (library_id, content, embedding, chunk_index)
    VALUES (
        v_library_id,
        'Synthetic control methods provide a transparent comparison.',
        v_embedding,
        0
    );

    IF (SELECT count(*) FROM match_chunks(v_embedding, 0.9, 10, 2020)) <> 1 THEN
        RAISE EXCEPTION 'chunk vector search did not return the ingested document';
    END IF;

    IF (SELECT count(*) FROM match_chunks(v_embedding, 0.9, 10, 2030)) <> 0 THEN
        RAISE EXCEPTION 'chunk vector search ignored the year filter';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM chunks
        WHERE fts @@ plainto_tsquery('english', 'synthetic control')
          AND library_id = v_library_id
    ) THEN
        RAISE EXCEPTION 'chunk full-text search index did not return the expected match';
    END IF;

    INSERT INTO people (
        name, affiliation, relationship, notes, embedding, follow_up_date
    )
    VALUES (
        'Ada Researcher',
        'Trial University',
        'collaborator',
        'Discuss synthetic controls.',
        v_embedding,
        current_date
    )
    RETURNING id INTO v_person_id;

    INSERT INTO projects (name, description, status, embedding)
    VALUES (
        'Policy Evaluation Trial',
        'Evaluate policy announcements.',
        'active',
        v_embedding
    )
    RETURNING id INTO v_project_id;

    INSERT INTO links (
        source_table, source_id, target_table, target_id, relationship
    )
    VALUES (
        'people', v_person_id, 'projects', v_project_id, 'collaborates_on'
    );

    IF (SELECT count(*) FROM match_people(v_embedding, 0.9, 10)) <> 1 THEN
        RAISE EXCEPTION 'people vector search did not return the expected match';
    END IF;

    IF (SELECT count(*) FROM match_projects(v_embedding, 0.9, 10)) <> 1 THEN
        RAISE EXCEPTION 'project vector search did not return the expected match';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM links
        WHERE source_table = 'people'
          AND source_id = v_person_id
          AND target_table = 'projects'
          AND target_id = v_project_id
          AND relationship = 'collaborates_on'
    ) THEN
        RAISE EXCEPTION 'entity link was not stored';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM people
        WHERE id = v_person_id
          AND follow_up_date <= current_date + make_interval(days := 1)
    ) THEN
        RAISE EXCEPTION 'due follow-up was not queryable';
    END IF;

    v_related := upsert_thought(
        'A related thought about baseline comparisons',
        v_distant_embedding,
        '{"people":["Ada"],"tags":["baseline"],"projects":["Policy Evaluation Trial"]}'::jsonb,
        'note'
    );
    PERFORM upsert_thought(
        'A restricted related thought',
        v_distant_embedding,
        '{"people":["Ada"],"tags":["baseline"],"sensitivity_tier":"restricted"}'::jsonb,
        'note'
    );

    IF (SELECT count(*) FROM get_thought_connections(
        (v_first->>'id')::uuid, 10, true
    )) <> 1 THEN
        RAISE EXCEPTION 'thought connections did not exclude restricted matches';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM get_thought_connections((v_first->>'id')::uuid, 10, true)
        WHERE id = (v_related->>'id')::uuid
          AND shared_tags = ARRAY['baseline']
          AND shared_people = ARRAY['Ada']
          AND overlap_count = 2
    ) THEN
        RAISE EXCEPTION 'thought connections did not report expected overlaps';
    END IF;

    v_stats := brain_stats_aggregate();

    IF (v_stats->>'thoughts_total')::int <> 4
       OR (v_stats->>'library_total')::int <> 1 THEN
        RAISE EXCEPTION 'aggregate stats returned incorrect table totals: %', v_stats;
    END IF;

    IF (v_stats->'types'->>'note')::int <> 4
       OR (v_stats->'tags'->>'baseline')::int <> 3
       OR (v_stats->'sensitivity_tiers'->>'restricted')::int <> 1 THEN
        RAISE EXCEPTION 'aggregate stats returned incorrect grouped counts: %', v_stats;
    END IF;
END;
$$;
SQL

echo "PostgreSQL schema and search trial run passed."
echo "RESULT: PASS"
