-- Universal capture table: every memory, note, conversation summary, and ingest event
CREATE TABLE thoughts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT NOT NULL,
    embedding   VECTOR(1024),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON thoughts USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Auto-update updated_at on row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER thoughts_updated_at
    BEFORE UPDATE ON thoughts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
