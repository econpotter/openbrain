-- Structured catalog of ingested documents.

CREATE TABLE library (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title       TEXT NOT NULL,
    authors     TEXT[],
    year        INT,
    source      TEXT,
    doc_type    TEXT,              -- paper | note | conversation | web | other
    abstract    TEXT,
    filepath    TEXT,
    file_hash   TEXT UNIQUE,       -- SHA256, used for deduplication
    status      TEXT DEFAULT 'ingested'
                CHECK (status IN ('ingested', 'reading', 'read', 'archived')),
    metadata    JSONB DEFAULT '{}',
    doi         TEXT,
    bibtex      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Text chunks from library documents, each with its own embedding.
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id  UUID REFERENCES library(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(1536),
    page        INT,
    chunk_index INT,
    fts         TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', content)) STORED,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_library_id_idx ON chunks (library_id);
CREATE INDEX chunks_fts_idx ON chunks USING gin (fts);
