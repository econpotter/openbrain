"""
papers.py — thin adapter over the `citer` library for external paper metadata.

`citer` owns everything about talking to the outside world: network access,
source fan-out across OpenAlex / Crossref / Semantic Scholar / arXiv (/ NBER /
RePEc), field-by-field merging, and the polite/premium pool authentication
(Semantic Scholar `x-api-key` + 1 req/sec throttle, Crossref `mailto` polite
pool + optional Plus token, OpenAlex `mailto` + optional premium `api_key`). All
of that is read from the same environment variables openbrain already documents
in `.env.example`.

This module does two small jobs and nothing else:

  * translate citer's ``Paper`` model into openbrain's internal metadata dict
    (the shape ``brain.py``'s store path expects), and
  * expose lookup / topic-search / citation-graph / concept helpers that take the
    arguments ``brain.py`` already has (a raw identifier, a source order, a fuzzy
    threshold, corroborating year/authors).

It has no dependency on ``brain.py`` or the database. Network calls are blocking,
so ``brain.py`` runs these helpers inside an executor.
"""

from __future__ import annotations

import citer
from citer import (
    MATCH_THRESHOLD,
    AuthorRef,
    Paper,
    Reference,
    SearchFilters,
    detect_identifier_kind,
    normalize_doi,
    normalize_title,
    render,
    score_candidate,
)
from citer.config import load_config

__all__ = [
    "MATCH_THRESHOLD",
    "DEFAULT_ORDER",
    "detect_identifier_kind",
    "normalize_doi",
    "normalize_title",
    "score_row",
    "paper_to_meta",
    "resolve_known_item",
    "search_topic",
    "lookup",
    "citation_neighbors",
    "concepts",
]

# openbrain's config.yaml names Semantic Scholar with an underscore; citer's
# source registry uses the compact key. Everything else passes through.
_SOURCE_ALIASES = {"semantic_scholar": "semanticscholar"}

# Default fan-out order, in openbrain's naming (config.yaml may override).
DEFAULT_ORDER = ["openalex", "crossref", "semantic_scholar", "arxiv"]

# Polite-pool identifier used when neither CROSSREF_MAILTO nor CITER_MAILTO is
# set — preserves the mailto openbrain has always sent to OpenAlex/Crossref.
_DEFAULT_MAILTO = "open-brain@localhost"

# Identifier kinds citer can resolve to a paper directly (no fuzzy title search).
_STRONG_KINDS = {"doi", "arxiv", "openalex", "s2", "corpus", "pmid", "nber", "repec"}


def _config(order: list[str] | None = None):
    """Build a citer Config: env-derived auth (keys + polite/premium pools) plus
    the caller's source order. `load_config` populates the API keys from the
    environment, so all four pool behaviors come for free."""
    cfg = load_config()
    if order:
        cfg.enabled_sources = [_SOURCE_ALIASES.get(s, s) for s in order]
    if not cfg.mailto:
        cfg.mailto = _DEFAULT_MAILTO
    return cfg


# ---------------------------------------------------------------------------
# Paper (citer) -> meta dict (openbrain) translation
# ---------------------------------------------------------------------------


def paper_to_meta(paper: Paper | None) -> dict | None:
    """Translate a citer ``Paper`` into openbrain's internal metadata dict.

    Keys mirror the old ``sources.PaperMeta``: title, authors (plain names),
    year, source (venue name), abstract, doi, arxiv_id, bibtex, issn_l,
    venue_source_id (OpenAlex source id), concepts (citer topics), plus
    openalex_id for citation-graph reference building.
    """
    if paper is None:
        return None
    venue = paper.venue
    bibtex = render(paper, "bibtex") or None
    return {
        "title": paper.title,
        "authors": [a.name for a in paper.authors],
        "year": paper.year,
        "source": venue.name if venue else None,
        "abstract": paper.abstract,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "bibtex": bibtex,
        "issn_l": venue.issn if venue else None,
        "venue_source_id": venue.id if venue else None,
        "concepts": list(paper.topics),
        "openalex_id": paper.openalex_id,
    }


# ---------------------------------------------------------------------------
# Fuzzy scoring of local DB rows (reuses citer's scorer on its models)
# ---------------------------------------------------------------------------


def score_row(ref_title: str | None, row: dict, *, year=None, authors=None) -> float:
    """Score a library-row dict against a reference title (+ optional
    corroborating year/author) using citer's ``score_candidate``. Wraps the dict
    in citer models so brain.py's local reference matching shares the exact
    scorer citer uses for external resolution."""
    ref = Reference(
        title=ref_title,
        year=year,
        author=(authors[0] if authors else None),
    )
    cand = Paper(
        title=row.get("title"),
        year=row.get("year"),
        authors=[AuthorRef(name=a) for a in (row.get("authors") or [])],
    )
    return score_candidate(ref, cand)


# ---------------------------------------------------------------------------
# Reference construction + descriptor resolution
# ---------------------------------------------------------------------------


def _ref_from_identifier(identifier: str, *, year=None, authors=None) -> Reference:
    """Build a citer Reference from a raw identifier. DOI/arXiv/etc. become a
    strong-id reference; a title becomes a descriptor carrying corroborating
    year/author so citer's own resolver can score it."""
    kind = detect_identifier_kind(identifier)
    author = authors[0] if authors else None
    if kind == "doi":
        return Reference(doi=normalize_doi(identifier))
    if kind == "arxiv":
        return Reference(arxiv_id=identifier)
    if kind == "openalex":
        return Reference(openalex_id=identifier)
    if kind == "pmid":
        return Reference(pmid=identifier)
    if kind == "nber":
        return Reference(nber_id=identifier)
    if kind == "s2":
        return Reference(s2_id=identifier)
    if kind == "repec":
        return Reference(repec_handle=identifier)
    return Reference(title=identifier, year=year, author=author)


def _resolve_descriptor(ref: Reference, cfg, threshold: float) -> Reference | None:
    """Descriptor (title) -> id-bearing Reference using a caller-supplied fuzzy
    threshold. Mirrors citer's own resolve_bundle but lets openbrain override the
    match threshold (citer's public resolve uses a fixed one). Returns None when
    no candidate clears the threshold."""
    filters = SearchFilters(
        query=ref.title, title=ref.title, author=ref.author, year=ref.year, limit=10
    )
    cands = citer.search(ref.title, filters=filters, config=cfg)
    scored = sorted(
        ((score_candidate(ref, c), c) for c in cands),
        key=lambda t: t[0],
        reverse=True,
    )
    if scored and scored[0][0] >= threshold and scored[0][1].title:
        best = scored[0][1]
        data = ref.model_dump()
        for field in (
            "doi",
            "arxiv_id",
            "openalex_id",
            "s2_id",
            "pmid",
            "nber_id",
            "repec_handle",
        ):
            if not data.get(field) and getattr(best, field):
                data[field] = getattr(best, field)
        return Reference(**data)
    return None


# ---------------------------------------------------------------------------
# Public helpers used by brain.py / cli.py
# ---------------------------------------------------------------------------


def resolve_known_item(
    identifier: str,
    *,
    order: list[str] | None = None,
    min_score: float | None = None,
    year: int | None = None,
    authors=None,
) -> dict | None:
    """Resolve one known-item paper to a merged metadata dict, or None.

    DOI/arXiv/etc. are exact. A title is fuzzy: citer's resolver scores search
    hits and only resolves when the best clears the match threshold (overridable
    via ``min_score``); ``year``/``authors`` corroborate. citer fans out across
    the enabled sources and merges the record field-by-field.
    """
    cfg = _config(order)
    kind = detect_identifier_kind(identifier)
    if kind in _STRONG_KINDS:
        paper = citer.fetch(_ref_from_identifier(identifier), config=cfg)
        return paper_to_meta(paper) if paper and paper.title else None

    ref = _ref_from_identifier(identifier, year=year, authors=authors)
    if min_score is not None:
        ref = _resolve_descriptor(ref, cfg, min_score)
        if ref is None:
            return None
    paper = citer.fetch(ref, config=cfg)
    return paper_to_meta(paper) if paper and paper.title else None


def search_topic(
    topic: str, *, order: list[str] | None = None, limit: int = 10
) -> list[dict]:
    """Topic/keyword discovery: fan out across the enabled sources and return a
    deduplicated candidate list (citer dedups by canonical id/title)."""
    cfg = _config(order)
    papers = citer.search(topic, filters=SearchFilters(query=topic, limit=limit), config=cfg)
    metas = [paper_to_meta(p) for p in papers]
    return [m for m in metas if m is not None]


def lookup(doi: str, *, order: list[str] | None = None) -> dict | None:
    """Merged metadata for an exact DOI (used to enrich a partial record)."""
    cfg = _config(order)
    paper = citer.fetch(Reference(doi=normalize_doi(doi) or doi), config=cfg)
    return paper_to_meta(paper) if paper and paper.title else None


def citation_neighbors(
    *,
    doi: str | None = None,
    openalex_id: str | None = None,
    title: str | None = None,
    year: int | None = None,
    authors=None,
    order: list[str] | None = None,
) -> list[dict]:
    """Citation neighbors of a paper: its outbound references (``cites``) plus its
    inbound citing works (``cited_by``), deduplicated. The paper is identified by
    DOI, OpenAlex work id, or a descriptor (title + optional year/authors, which
    citer resolves to an id via scored search before walking the graph). Returns
    [] when the paper cannot be resolved to an id-bearing reference."""
    cfg = _config(order)
    ref = Reference(
        doi=normalize_doi(doi) if doi else None,
        openalex_id=openalex_id,
        title=title,
        year=year,
        author=(authors[0] if authors else None),
    )
    neighbors: list[dict] = []
    seen: set[str] = set()
    for fetch_edges in (citer.cites, citer.cited_by):
        for paper in fetch_edges(ref, config=cfg):
            key = paper.canonical_id() or (paper.title or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            meta = paper_to_meta(paper)
            if meta is not None:
                neighbors.append(meta)
    return neighbors


def concepts(term: str, *, limit: int = 25) -> list[dict]:
    """Look up OpenAlex topic vocabulary matching ``term`` (with work counts) so
    an agent expands a query with real index vocabulary instead of guessed
    synonyms. Returns OpenAlex-sourced entries only (the concept/topic index)."""
    cfg = _config(["openalex"])
    out = []
    for topic in citer.topics(term, config=cfg):
        if topic.source != "openalex":
            continue
        out.append(
            {
                "id": topic.id,
                "name": topic.name,
                "field": topic.field,
                "works_count": topic.works_count,
            }
        )
        if len(out) >= limit:
            break
    return out
