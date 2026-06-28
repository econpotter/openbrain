-- Venue registry: maps a fetched paper's venue → canonical venue_abbrev.
-- Seeded from openbrain/venues.toml at setup; auto-grows with origin='discovered'.
CREATE TABLE venues (
    abbrev      TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    field       TEXT,
    kind        TEXT NOT NULL
                CHECK (kind IN ('journal','preprint','working_paper','report','book','series')),
    issn_l      TEXT UNIQUE,
    source_id   TEXT UNIQUE,
    match_names TEXT[],
    origin      TEXT DEFAULT 'seed' CHECK (origin IN ('seed','discovered'))
);
CREATE INDEX venues_kind_idx ON venues (kind);
