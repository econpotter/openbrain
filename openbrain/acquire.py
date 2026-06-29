"""
acquire.py — the deterministic paper-acquisition pipeline.

    acquire(query, *, venues_filter=None, since=None, limit=50, min_overlap=2,
            dry_run=False) -> report

Stages: SEARCH (paginated, API-side filters) → DEDUP (DOI then title+year) →
ENRICH (crossref fills missing abstract/issn/concepts) → CANON (registry match
cascade → venue_abbrev) → PREFILTER (lexical recall-gate, no LLM) → STORE (bulk
insert + abstract embedding via brain.store_acquired_paper).

There is NO LLM anywhere here — relevance is a deterministic lexical overlap
gate only. Volume is bounded by venues_filter, since, min_overlap, limit, and
dedup-vs-library.
"""

import asyncio
import re

from openbrain import brain, sources
from openbrain import venues as venues_mod

# How many raw candidates to pull per requested survivor, so the prefilter has
# headroom to drop junk and still reach `limit` keepers.
_OVERFETCH = 5


def _norm_title(title: str | None) -> str:
    return re.sub(r"\W+", " ", (title or "").lower()).strip()


def dedup_records(records: list[dict]) -> list[dict]:
    """Drop intra-batch duplicates: same DOI, or (DOI-less) same normalized
    title + year. OpenAlex and Crossref often return the same paper with
    differing or absent DOIs."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in records:
        if r.get("doi"):
            key = "doi:" + r["doi"].lower()
        else:
            key = f"ty:{_norm_title(r.get('title'))}:{r.get('year')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def prefilter(
    rec: dict,
    query: str,
    since: int | None,
    min_overlap: int,
    venues_filter: set[str] | None,
) -> str | None:
    """Deterministic recall gate. Returns a drop-reason string, or None to keep.

    Drop reasons: 'no_abstract', 'old', 'venue', 'low_overlap'.
    """
    if not rec.get("abstract"):
        return "no_abstract"
    if since and (rec.get("year") or 0) < since:
        return "old"
    if venues_filter and rec.get("venue_abbrev") not in venues_filter:
        return "venue"
    hay = " ".join(
        filter(
            None,
            [
                rec.get("title"),
                rec.get("abstract"),
                " ".join(rec.get("concepts") or []),
            ],
        )
    )
    if venues_mod.lexical_overlap(query, hay) < min_overlap:
        return "low_overlap"
    return None


async def acquire(
    query: str,
    *,
    venues_filter: set[str] | None = None,
    since: int | None = None,
    limit: int = 50,
    min_overlap: int = 2,
    dry_run: bool = False,
) -> dict:
    """Run the deterministic acquisition pipeline and return a report dict."""
    pool = await brain.get_pool()
    loop = asyncio.get_event_loop()

    # --- SEARCH (API-side filters pushed down) ---
    issns = None
    if venues_filter:
        issns = await venues_mod.issns_for(pool, venues_filter)

    def _search() -> tuple[list, bool]:
        return sources.openalex_search_paged(
            query,
            issns=issns,
            since=since,
            limit=limit * _OVERFETCH,
        )

    raw_records, truncated = await loop.run_in_executor(None, _search)
    # Work on plain dicts (the records are TypedDicts) so the pipeline can write
    # derived keys (venue_abbrev, issn) freely.
    records: list[dict] = [dict(r) for r in raw_records]
    found = len(records)

    # --- DEDUP (DOI, then title+year) ---
    deduped_list = dedup_records(records)
    deduped = found - len(deduped_list)

    # --- ENRICH (crossref fills missing abstract / issn / concepts) ---
    for rec in deduped_list:
        if rec.get("abstract") or not rec.get("doi"):
            continue
        doi = rec["doi"]

        def _enrich(d: str = doi):
            return sources.crossref_lookup(d, "doi")

        extra_raw = await loop.run_in_executor(None, _enrich)
        if not extra_raw:
            continue
        extra = dict(extra_raw)
        for key in ("abstract", "issn_l", "bibtex"):
            if not rec.get(key) and extra.get(key):
                rec[key] = extra[key]
        if extra.get("concepts"):
            rec["concepts"] = list(
                dict.fromkeys((rec.get("concepts") or []) + extra["concepts"])
            )

    # --- CANON (registry match cascade → venue_abbrev) ---
    unknown_venues: list[dict] = []
    for rec in deduped_list:
        abbrev = await venues_mod.match(
            pool,
            issn_l=rec.get("issn_l"),
            source_id=rec.get("venue_source_id"),
            name=rec.get("source"),
        )
        rec["venue_abbrev"] = abbrev
        rec["issn"] = rec.get("issn_l")
        if abbrev is None and (
            rec.get("issn_l") or rec.get("venue_source_id") or rec.get("source")
        ):
            unknown_venues.append(
                {
                    "issn_l": rec.get("issn_l"),
                    "source_id": rec.get("venue_source_id"),
                    "name": rec.get("source"),
                }
            )

    # --- PREFILTER (deterministic recall gate) ---
    reasons = {"no_abstract": 0, "old": 0, "venue": 0, "low_overlap": 0}
    survivors: list[dict] = []
    for rec in deduped_list:
        reason = prefilter(rec, query, since, min_overlap, venues_filter)
        if reason:
            reasons[reason] += 1
        else:
            survivors.append(rec)
    survivors = survivors[:limit]

    # --- STORE (skip writes on dry_run) ---
    stored = 0
    stored_ids: list[str] = []
    sample: list[dict] = []
    for rec in survivors:
        if len(sample) < 10:
            sample.append(
                {
                    "title": rec.get("title"),
                    "year": rec.get("year"),
                    "venue_abbrev": rec.get("venue_abbrev"),
                }
            )
        if not dry_run:
            result = await brain.store_acquired_paper(rec)
            if not result.get("deduped"):
                stored += 1
                stored_ids.append(result["id"])

    return {
        "found": found,
        "deduped": deduped,
        "prefiltered_out": reasons,
        "stored": stored,
        "stored_ids": stored_ids,
        "truncated": truncated,
        "unknown_venues": unknown_venues,
        "sample": sample,
    }
