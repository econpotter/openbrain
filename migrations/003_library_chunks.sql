-- Structured catalog of ingested documents
CREATE TABLE library (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL,
    authors     TEXT[],
    year        INT,
    journal     TEXT,
    doc_type    TEXT,              -- paper | note | conversation | web | other
    abstract    TEXT,
    filepath    TEXT,
    file_hash   TEXT UNIQUE,       -- SHA256, used for deduplication
    status      TEXT DEFAULT 'ingested'
                CHECK (status IN ('reading_list','ingested','reading','read','archived')),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Text chunks from library documents, each with its own embedding
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id  UUID REFERENCES library(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(1024),
    page        INT,
    chunk_index INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX ON chunks (library_id);
