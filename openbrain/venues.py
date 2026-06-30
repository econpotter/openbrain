"""
venues.py — venue registry: load/seed the `venues` table, the venue-match
cascade (issn_l → source_id → normalized name), and a stdlib lexical-overlap
helper (a deterministic recall signal for callers that gate relevance).

Pure helpers (normalize_name, lexical_overlap, load_seeds) have no DB
dependency. seed_db/match take a brain.get_pool() pool and follow openbrain's
"all DB access through a pool.acquire() connection" pattern.
"""

import re
import tomllib
from pathlib import Path

# Tokens dropped from venue names before matching: generic publication words and
# articles that carry no disambiguating signal.
_STOP = {"the", "of", "and", "journal", "review", "for", "a", "an"}

VENUES_TOML = str(Path(__file__).with_name("venues.toml"))


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and stop-words, collapse whitespace."""
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _STOP)


def _terms(text: str) -> set[str]:
    """Distinct content tokens (length > 2) from free text."""
    return {
        w for w in re.sub(r"[^\w\s]", " ", (text or "").lower()).split() if len(w) > 2
    }


def lexical_overlap(query: str, text: str) -> int:
    """Count distinct query terms present in `text` — a cheap recall gate, not a ranker."""
    return len(_terms(query) & _terms(text))


def load_seeds(path: str = VENUES_TOML) -> list[dict]:
    """Parse venues.toml into normalized seed dicts."""
    data = tomllib.loads(Path(path).read_text())
    seeds = []
    for v in data.get("venue", []):
        seeds.append(
            {
                "abbrev": v["abbrev"],
                "full_name": v.get("full_name", v["abbrev"]),
                "field": v.get("field"),
                "kind": v["kind"],
                "issn_l": v.get("issn_l"),
                "source_id": v.get("source_id"),
                "match_names": [normalize_name(n) for n in v.get("match_names", [])],
            }
        )
    return seeds


async def seed_db(pool, path: str = VENUES_TOML) -> int:
    """Upsert seed venues into the `venues` table (origin='seed'); idempotent."""
    seeds = load_seeds(path)
    async with pool.acquire() as conn:
        for s in seeds:
            await conn.execute(
                """
                INSERT INTO venues
                    (abbrev, full_name, field, kind, issn_l, source_id, match_names, origin)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'seed')
                ON CONFLICT (abbrev) DO UPDATE SET
                    full_name   = EXCLUDED.full_name,
                    field       = EXCLUDED.field,
                    kind        = EXCLUDED.kind,
                    issn_l      = EXCLUDED.issn_l,
                    source_id   = EXCLUDED.source_id,
                    match_names = EXCLUDED.match_names
                """,
                s["abbrev"],
                s["full_name"],
                s["field"],
                s["kind"],
                s["issn_l"],
                s["source_id"],
                s["match_names"],
            )
    return len(seeds)


async def issns_for(pool, abbrevs) -> list[str]:
    """Resolve venue abbrevs → their journal ISSN-Ls (for the API-side filter).
    Abbrevs without an issn_l (preprints/working papers) are skipped."""
    issns: list[str] = []
    async with pool.acquire() as conn:
        for abbrev in abbrevs:
            issn = await conn.fetchval(
                "SELECT issn_l FROM venues WHERE abbrev = $1", abbrev
            )
            if issn:
                issns.append(issn)
    return issns


async def match(
    pool,
    *,
    issn_l: str | None = None,
    source_id: str | None = None,
    name: str | None = None,
) -> str | None:
    """Resolve a venue to its canonical abbrev via the cascade:
    issn_l → source_id → normalized name in match_names → None."""
    async with pool.acquire() as conn:
        if issn_l:
            r = await conn.fetchval(
                "SELECT abbrev FROM venues WHERE issn_l = $1", issn_l
            )
            if r:
                return r
        if source_id:
            r = await conn.fetchval(
                "SELECT abbrev FROM venues WHERE source_id = $1", source_id
            )
            if r:
                return r
        if name:
            r = await conn.fetchval(
                "SELECT abbrev FROM venues WHERE $1 = ANY(match_names)",
                normalize_name(name),
            )
            if r:
                return r
    return None
