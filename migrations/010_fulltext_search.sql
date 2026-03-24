-- Add generated tsvector columns for full-text search on thoughts and chunks

ALTER TABLE thoughts ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS thoughts_fts_idx ON thoughts USING gin(fts);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
CREATE INDEX IF NOT EXISTS chunks_fts_idx ON chunks USING gin(fts);
