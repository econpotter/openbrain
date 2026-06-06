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
    file_hash   TEXT UNIQUE,       -- SHA256, used for deduplication.
                                   -- NULL for metadata-only records (fetch_paper);
                                   -- Postgres allows multiple NULLs under UNIQUE.
    status      TEXT DEFAULT 'ingested'
                CHECK (status IN ('ingested', 'reading', 'read', 'archived')),
    metadata    JSONB DEFAULT '{}',
    doi         TEXT,
    bibtex      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Deduplicate metadata-only records (no file_hash) by DOI. File-based ingests
-- still dedup on file_hash above; fetch_paper records dedup here.
CREATE UNIQUE INDEX library_doi_uniq ON library (doi) WHERE doi IS NOT NULL;

-- Text chunks from library documents, each with its own embedding.
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id  UUID REFERENCES library(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   VECTOR(768),
    page        INT,
    chunk_index INT,
    kind        TEXT NOT NULL DEFAULT 'body'
                CHECK (kind IN ('body', 'abstract')),   -- 'abstract' = fetched abstract
    fts         TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', content)) STORED,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_library_id_idx ON chunks (library_id);
CREATE INDEX chunks_fts_idx ON chunks USING gin (fts);
