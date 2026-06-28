-- Typed causal edges extracted from papers: cause -> effect, with magnitude.
-- A second derivation alongside chunk embeddings: one model reads each paper and
-- emits provenance-linked findings. cause_var_id/effect_var_id stay NULL until the
-- deferred variable-resolution pass (design §6) links raw text to canonical nodes.
CREATE TABLE findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id        UUID NOT NULL REFERENCES library(id) ON DELETE CASCADE,  -- provenance
    cause_text      TEXT NOT NULL,         -- as extracted (raw)
    effect_text     TEXT NOT NULL,         -- as extracted (raw)
    cause_var_id    UUID REFERENCES variables(id),   -- NULL until resolved (§6)
    effect_var_id   UUID REFERENCES variables(id),   -- NULL until resolved (§6)
    magnitude_value NUMERIC,               -- nullable: not every finding is a number
    magnitude_unit  TEXT,                  -- %, elasticity, $/unit, ...
    magnitude_stat  TEXT,                  -- coefficient | elasticity | correlation | p_value | ...
    conditions      TEXT,                  -- scope/population/qualifiers (free text)
    raw_quote       TEXT,                  -- source sentence, for verification
    confidence      NUMERIC,               -- model self-reported extraction confidence
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX findings_paper_idx  ON findings (paper_id);
CREATE INDEX findings_cause_idx  ON findings (cause_var_id);
CREATE INDEX findings_effect_idx ON findings (effect_var_id);
