-- Saved acquisition queries for incremental harvest.
-- The harvested_through watermark is the MAX source-index created_date ingested so
-- far (NOT wall-clock run time); the next run queries from_created_date > this.
CREATE TABLE saved_queries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT UNIQUE NOT NULL,
    query             TEXT NOT NULL,
    venues            TEXT[],
    since             INT,
    min_overlap       INT DEFAULT 2,
    cadence           TEXT DEFAULT 'weekly'
                      CHECK (cadence IN ('nightly','weekly','monthly')),
    harvested_through TIMESTAMPTZ,
    last_run          TIMESTAMPTZ,
    last_yield        JSONB DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT now()
);
