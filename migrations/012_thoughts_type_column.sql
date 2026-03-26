-- Add first-class type column to thoughts, backfilled from metadata
ALTER TABLE thoughts
    ADD COLUMN type TEXT NOT NULL DEFAULT 'thought';

UPDATE thoughts
    SET type = metadata->>'type'
    WHERE metadata->>'type' IS NOT NULL;

CREATE INDEX ON thoughts (type);
