"""
sources.py — pluggable external paper-metadata sources for fetch_papers.

Each source exposes two functions:

    lookup(identifier, id_kind) -> PaperMeta | None   # known-item (doi|arxiv|title)
    search(query, limit)        -> list[PaperMeta]     # topic/keyword discovery

`resolve()` fans out across an ordered list of sources and either merges the
results into one record (known-item lookup) or returns a deduplicated candidate
list (topic search).

This module is intentionally pure: it has no dependency on brain.py or the
database. Network calls use urllib (stdlib, no new dependency) and are blocking,
so brain.py runs resolve() inside an executor. Configuration (which sources, in
what order) is owned by brain.py and passed in via the `order` argument.
"""

import difflib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, TypedDict, TypeVar

_UA = {"User-Agent": "OpenBrain/1.0 (mailto:open-brain@localhost)"}
_TIMEOUT = 10

_DOI_RE = re.compile(r"\b(10\.\d{4,}(?:\.\d+)*/\S+)\b")
_ARXIV_RE = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


class PaperMeta(TypedDict, total=False):
    title: str | None
    authors: list[str]
    year: int | None
    source: str | None  # venue / journal
    abstract: str | None
    doi: str | None
    arxiv_id: str | None
    bibtex: str | None
    url: str | None
    source_name: str  # which backend produced this record
    issn_l: str | None  # journal venue key (OpenAlex issn_l / Crossref ISSN)
    venue_source_id: (
        str | None
    )  # OpenAlex source id (S…), issn-equivalent for issn-less venues
    concepts: list[str]  # OpenAlex concepts ∪ Crossref subjects ∪ source keywords


# ---------------------------------------------------------------------------
# Identifier detection
# ---------------------------------------------------------------------------


def detect_identifier_kind(identifier: str) -> str:
    """Classify a raw identifier as 'doi', 'arxiv', or 'title'."""
    ident = identifier.strip()
    if _DOI_RE.search(ident):
        return "doi"
    if ident.lower().startswith("arxiv:") or _ARXIV_RE.fullmatch(ident):
        return "arxiv"
    return "title"


def _normalize_doi(identifier: str) -> str | None:
    m = _DOI_RE.search(identifier)
    return m.group(1).rstrip(".,)") if m else None


def _normalize_arxiv(identifier: str) -> str | None:
    m = _ARXIV_RE.search(identifier)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _with_retry(
    fn: Callable[[], _T], *, attempts: int = 3, base_delay: float = 1.0
) -> _T:
    """Call `fn`, retrying with exponential backoff on transient failures (e.g.
    HTTP 429). Re-raises the last exception after `attempts` tries."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — transient network errors, retried
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


def _get_json(url: str, headers: dict | None = None) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_text(url: str, headers: dict | None = None) -> str | None:
    try:
        req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _clean_abstract(text: str | None) -> str | None:
    """Strip JATS/HTML tags and collapse whitespace from an abstract."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def _openalex_abstract(inverted: dict | None) -> str | None:
    if not inverted:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions)) or None


def _openalex_record(work: dict) -> PaperMeta:
    authors = [
        a["author"]["display_name"]
        for a in work.get("authorships", [])
        if a.get("author", {}).get("display_name")
    ]
    venue = (work.get("primary_location") or {}).get("source") or {}
    doi = work.get("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "")
    source_id = venue.get("id")
    if source_id:
        source_id = source_id.replace("https://openalex.org/", "")
    concepts = [
        c["display_name"] for c in work.get("concepts", []) if c.get("display_name")
    ]
    return PaperMeta(
        title=work.get("display_name"),
        authors=authors,
        year=work.get("publication_year"),
        source=venue.get("display_name"),
        abstract=_clean_abstract(
            _openalex_abstract(work.get("abstract_inverted_index"))
        ),
        doi=doi,
        arxiv_id=None,
        bibtex=None,
        url=work.get("id"),
        source_name="openalex",
        issn_l=venue.get("issn_l"),
        venue_source_id=source_id,
        concepts=concepts,
    )


def openalex_lookup(identifier: str, id_kind: str) -> PaperMeta | None:
    if id_kind == "doi":
        doi = _normalize_doi(identifier)
        work = _get_json(
            f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
        )
        return _openalex_record(work) if work else None
    if id_kind == "title":
        results = openalex_search(identifier, limit=1)
        return results[0] if results else None
    return None  # arxiv handled by other sources


def openalex_search(query: str, limit: int = 10) -> list[PaperMeta]:
    q = urllib.parse.quote(query)
    url = (
        f"https://api.openalex.org/works?search={q}&per-page={limit}"
        "&mailto=open-brain@localhost"
    )
    # OpenAlex's search endpoint 504s under load; retry a few times before giving
    # up (the work/DOI endpoints are more reliable, so this only guards search).
    data = None
    for attempt in range(3):
        data = _get_json(url)
        if data is not None:
            break
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    if not data:
        return []
    return [_openalex_record(w) for w in data.get("results", [])]


def openalex_search_paged(
    query: str,
    *,
    issns: list[str] | None = None,
    since: int | None = None,
    limit: int = 50,
    per_page: int = 200,
) -> tuple[list[PaperMeta], bool]:
    """
    Cursor-paginated OpenAlex search with API-side filters for the acquire
    pipeline. Filters (`issns`, `since`) are pushed into the OpenAlex `filter=`
    parameter rather than applied client-side.

    `issns` restricts to those journal ISSN-Ls (the registry resolves
    venue_abbrev → issns before calling). Returns `(records, truncated)`; the
    `truncated` flag is True when matches remained past `limit` — never a silent
    cut. OpenAlex caps `per-page` at 200.
    """
    filters = []
    if issns:
        filters.append("primary_location.source.issn:" + "|".join(issns))
    if since:
        filters.append(f"from_publication_date:{since}-01-01")

    q = urllib.parse.quote(query)
    per_page = min(per_page, 200)
    records: list[PaperMeta] = []
    cursor: str | None = "*"
    truncated = False

    while cursor:
        url = (
            f"https://api.openalex.org/works?search={q}&per-page={per_page}"
            f"&cursor={urllib.parse.quote(cursor)}&mailto=open-brain@localhost"
        )
        if filters:
            url += "&filter=" + urllib.parse.quote(",".join(filters))
        data = _with_retry(lambda: _get_json(url))
        if not data:
            break
        results = data.get("results", [])
        records.extend(_openalex_record(w) for w in results)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if len(records) >= limit:
            truncated = bool(cursor) or len(records) > limit
            break
        if not results:
            break

    return records[:limit], truncated


def openalex_concepts(term: str, limit: int = 25) -> list[dict]:
    """Look up OpenAlex concepts matching `term`, with work counts, so an agent
    can expand a query with real index vocabulary instead of guessed synonyms."""
    q = urllib.parse.quote(term)
    data = _get_json(
        f"https://api.openalex.org/concepts?search={q}&per-page={limit}"
        "&mailto=open-brain@localhost"
    )
    if not data:
        return []
    out = []
    for c in data.get("results", []):
        cid = c.get("id") or ""
        out.append(
            {
                "id": cid.replace("https://openalex.org/", ""),
                "name": c.get("display_name"),
                "level": c.get("level"),
                "works_count": c.get("works_count"),
            }
        )
    return out


def _openalex_works_by_ids(ids: list[str]) -> list[dict]:
    """Batch-fetch OpenAlex work records for short ids (W…), 50 per request via
    the `openalex_id` filter. Missing ids are silently dropped by the API."""
    records: list[dict] = []
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        data = _get_json(
            "https://api.openalex.org/works?per-page=50"
            "&mailto=open-brain@localhost"
            "&filter=openalex_id:" + "|".join(batch)
        )
        if data:
            records.extend(data.get("results", []))
    return records


def openalex_citation_neighbors(openalex_id: str) -> list[PaperMeta]:
    """Citation neighbors of an OpenAlex work, deduplicated as PaperMeta.

    Outbound edges come from the work's ``referenced_works`` ids (batch-fetched
    into full records); inbound edges from the first page of its
    ``cited_by_api_url``. References ∩ citing-works are deduplicated by the shared
    DOI/arXiv/title key. Returns [] if the work cannot be fetched. Network calls
    are monkeypatched in tests.
    """
    wid = openalex_id.replace("https://openalex.org/", "")
    work = _get_json(
        f"https://api.openalex.org/works/{urllib.parse.quote(wid)}"
        "?mailto=open-brain@localhost"
    )
    if not work:
        return []

    works: list[dict] = []
    ref_ids = [
        r.replace("https://openalex.org/", "")
        for r in (work.get("referenced_works") or [])
    ]
    if ref_ids:
        works.extend(_openalex_works_by_ids(ref_ids))

    cited_url = work.get("cited_by_api_url")
    if cited_url:
        data = _get_json(cited_url)
        if data:
            works.extend(data.get("results", []))

    neighbors: list[PaperMeta] = []
    seen: set[str] = set()
    for w in works:
        rec = _openalex_record(w)
        key = _dedup_key(rec)
        if key in seen:
            continue
        seen.add(key)
        neighbors.append(rec)
    return neighbors


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------


def _crossref_record(item: dict) -> PaperMeta:
    titles = item.get("title", [])
    authors = []
    for a in item.get("author", []):
        if "family" in a:
            authors.append(f"{a.get('given', '')} {a['family']}".strip())
    year = None
    for date_key in ("published-print", "published-online", "issued"):
        parts = (item.get(date_key) or {}).get("date-parts", [[]])
        if parts and parts[0]:
            year = parts[0][0]
            break
    containers = item.get("container-title", [])
    return PaperMeta(
        title=titles[0] if titles else None,
        authors=authors,
        year=year,
        source=containers[0] if containers else None,
        abstract=_clean_abstract(item.get("abstract")),
        doi=item.get("DOI"),
        arxiv_id=None,
        bibtex=None,
        url=item.get("URL"),
        source_name="crossref",
        issn_l=(item.get("ISSN") or [None])[0],
        venue_source_id=None,
        concepts=item.get("subject", []),
    )


def _crossref_bibtex(doi: str) -> str | None:
    return _get_text(
        f"https://doi.org/{urllib.parse.quote(doi)}",
        headers={"Accept": "application/x-bibtex"},
    )


def crossref_lookup(identifier: str, id_kind: str) -> PaperMeta | None:
    if id_kind == "doi":
        doi = _normalize_doi(identifier)
        data = _get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
        if not data:
            return None
        rec = _crossref_record(data["message"])
        rec["bibtex"] = _crossref_bibtex(doi)
        return rec
    if id_kind == "title":
        results = crossref_search(identifier, limit=1)
        return results[0] if results else None
    return None


def crossref_search(query: str, limit: int = 10) -> list[PaperMeta]:
    q = urllib.parse.quote(query)
    data = _get_json(f"https://api.crossref.org/works?query={q}&rows={limit}")
    if not data:
        return []
    return [_crossref_record(item) for item in data["message"].get("items", [])]


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

_S2_FIELDS = "title,authors,year,abstract,venue,externalIds,url"


def _s2_headers() -> dict:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    return {"x-api-key": key} if key else {}


def _s2_record(paper: dict) -> PaperMeta:
    ext = paper.get("externalIds") or {}
    return PaperMeta(
        title=paper.get("title"),
        authors=[a["name"] for a in paper.get("authors", []) if a.get("name")],
        year=paper.get("year"),
        source=paper.get("venue") or None,
        abstract=_clean_abstract(paper.get("abstract")),
        doi=ext.get("DOI"),
        arxiv_id=ext.get("ArXiv"),
        bibtex=None,
        url=paper.get("url"),
        source_name="semantic_scholar",
    )


def semantic_scholar_lookup(identifier: str, id_kind: str) -> PaperMeta | None:
    base = "https://api.semanticscholar.org/graph/v1/paper"
    if id_kind == "doi":
        doi = _normalize_doi(identifier)
        pid = f"DOI:{doi}"
    elif id_kind == "arxiv":
        arxiv = _normalize_arxiv(identifier)
        pid = f"arXiv:{arxiv}"
    else:
        results = semantic_scholar_search(identifier, limit=1)
        return results[0] if results else None
    data = _get_json(
        f"{base}/{urllib.parse.quote(pid)}?fields={_S2_FIELDS}", headers=_s2_headers()
    )
    return _s2_record(data) if data else None


def semantic_scholar_search(query: str, limit: int = 10) -> list[PaperMeta]:
    q = urllib.parse.quote(query)
    data = _get_json(
        "https://api.semanticscholar.org/graph/v1/paper/search"
        f"?query={q}&limit={limit}&fields={_S2_FIELDS}",
        headers=_s2_headers(),
    )
    if not data:
        return []
    return [_s2_record(p) for p in data.get("data", [])]


# ---------------------------------------------------------------------------
# arXiv (Atom XML)
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"


def _arxiv_record(entry: ET.Element) -> PaperMeta:
    def text(tag: str) -> str | None:
        el = entry.find(f"{_ATOM}{tag}")
        return el.text.strip() if el is not None and el.text else None

    authors = [
        a.find(f"{_ATOM}name").text.strip()
        for a in entry.findall(f"{_ATOM}author")
        if a.find(f"{_ATOM}name") is not None
    ]
    published = text("published")
    year = int(published[:4]) if published else None
    raw_id = text("id") or ""
    arxiv_id = _normalize_arxiv(raw_id)
    doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
    return PaperMeta(
        title=re.sub(r"\s+", " ", text("title")) if text("title") else None,
        authors=authors,
        year=year,
        source="arXiv",
        abstract=_clean_abstract(text("summary")),
        doi=doi_el.text.strip() if doi_el is not None and doi_el.text else None,
        arxiv_id=arxiv_id,
        bibtex=None,
        url=raw_id or None,
        source_name="arxiv",
    )


def _arxiv_query(params: str) -> list[PaperMeta]:
    xml = _get_text(f"http://export.arxiv.org/api/query?{params}")
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [_arxiv_record(e) for e in root.findall(f"{_ATOM}entry")]


def arxiv_lookup(identifier: str, id_kind: str) -> PaperMeta | None:
    if id_kind == "arxiv":
        arxiv = _normalize_arxiv(identifier)
        results = _arxiv_query(f"id_list={urllib.parse.quote(arxiv)}")
        return results[0] if results else None
    if id_kind == "title":
        results = arxiv_search(identifier, limit=1)
        return results[0] if results else None
    return None  # arXiv has no DOI index


def arxiv_search(query: str, limit: int = 10) -> list[PaperMeta]:
    q = urllib.parse.quote(query)
    return _arxiv_query(f"search_query=all:{q}&max_results={limit}")


# ---------------------------------------------------------------------------
# Registry + fan-out
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, tuple[Callable, Callable]] = {
    "openalex": (openalex_lookup, openalex_search),
    "crossref": (crossref_lookup, crossref_search),
    "semantic_scholar": (semantic_scholar_lookup, semantic_scholar_search),
    "arxiv": (arxiv_lookup, arxiv_search),
}

_DEFAULT_ORDER = ["openalex", "crossref", "semantic_scholar", "arxiv"]


def _normalize_title(title: str | None) -> str:
    """Lowercase, strip accents, drop apostrophes, punctuation->space, normalize
    '&', collapse whitespace — so spelling/punctuation noise compares equal."""
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['`‘’]", "", text)  # apostrophes deleted: cattlemen's -> cattlemens
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Shared fuzzy reference scorer (used by BOTH get_papers and fetch_papers)
# ---------------------------------------------------------------------------

MATCH_THRESHOLD = 0.8


def _title_similarity(a: str | None, b: str | None) -> float:
    """0..1 ratio of two normalized titles via difflib (stdlib)."""
    return difflib.SequenceMatcher(
        None, _normalize_title(a), _normalize_title(b)
    ).ratio()


def _ref_surnames(authors) -> set[str]:
    """Normalized author surnames from 'Last, First' or 'First Last' strings."""
    out: set[str] = set()
    for a in authors or []:
        token = a.split(",")[0] if "," in a else a.split()[-1]
        norm = _normalize_title(token)
        if norm:
            out.add(norm)
    return out


def _score_candidate(ref_title, cand: dict, *, year=None, authors=None) -> float:
    """Title similarity plus small bonuses when a supplied year / author surname
    corroborates. Bonuses only raise the score; they never block a strong title."""
    score = _title_similarity(ref_title, cand.get("title"))
    if year is not None and cand.get("year") == year:
        score += 0.05
    if authors and (_ref_surnames(authors) & _ref_surnames(cand.get("authors") or [])):
        score += 0.05
    return score


def _dedup_key(rec: PaperMeta) -> str:
    if rec.get("doi"):
        return "doi:" + rec["doi"].lower()
    if rec.get("arxiv_id"):
        return "arxiv:" + rec["arxiv_id"].lower()
    return "title:" + (rec.get("title") or "").strip().lower()


def _merge(records: list[PaperMeta]) -> PaperMeta:
    """Merge same-paper records field-by-field; earlier sources win per field."""
    merged: PaperMeta = {}
    contributors: list[str] = []
    for rec in records:
        contributors.append(rec.get("source_name", "?"))
        for key, value in rec.items():
            if key == "source_name":
                continue
            if not merged.get(key) and value:
                merged[key] = value
    merged["source_name"] = "+".join(dict.fromkeys(contributors))
    return merged


def resolve(
    identifier: str | None = None,
    topic: str | None = None,
    id_kind: str | None = None,
    order: list[str] | None = None,
    limit: int = 10,
    min_score: float | None = None,
    year: int | None = None,
    authors=None,
) -> PaperMeta | list[PaperMeta] | None:
    """
    Known-item lookup (identifier) -> one merged PaperMeta (or None).
    Topic search (topic) -> deduplicated list[PaperMeta] ranked by source order.

    Known-item resolution queries sources in order and SHORT-CIRCUITS on the
    first whose best candidate clears the fuzzy match threshold (DOI/arXiv use
    the exact identifier instead of scoring). The winner is then ENRICHED by its
    DOI from the remaining sources (fills a missing abstract/bibtex/issn).
    """
    names = [n for n in (order or _DEFAULT_ORDER) if n in _REGISTRY]

    if topic is not None:
        seen: dict[str, PaperMeta] = {}
        for name in names:
            _, search_fn = _REGISTRY[name]
            for rec in search_fn(topic, limit):
                key = _dedup_key(rec)
                if key not in seen:
                    seen[key] = rec
        return list(seen.values())[:limit]

    if identifier is not None:
        kind = id_kind or detect_identifier_kind(identifier)
        threshold = MATCH_THRESHOLD if min_score is None else min_score

        winner: PaperMeta | None = None
        remaining: list[str] = []
        for idx, name in enumerate(names):
            lookup_fn, search_fn = _REGISTRY[name]
            if kind == "title":
                # Title lookups are fuzzy at the source: score each hit and keep
                # the best only if it clears the threshold, so a generic or
                # partial query can't resolve to an unrelated paper.
                cands = search_fn(identifier, limit)
                scored = [
                    (c, _score_candidate(identifier, c, year=year, authors=authors))
                    for c in cands
                ]
                if scored:
                    best, best_score = max(scored, key=lambda t: t[1])
                    if best_score >= threshold and best.get("title"):
                        winner = best
            else:  # doi / arxiv — exact identifier, no scoring
                rec = lookup_fn(identifier, kind)
                if rec and rec.get("title"):
                    winner = rec
            if winner is not None:
                remaining = names[idx + 1 :]
                break

        if winner is None:
            return None

        # Enrich the winner from the remaining sources by its DOI (fills a missing
        # abstract/bibtex/issn). The winner is first, so its own fields win.
        enrich_recs: list[PaperMeta] = [winner]
        doi = winner.get("doi")
        if doi:
            for name in remaining:
                lookup_fn, _ = _REGISTRY[name]
                extra = lookup_fn(doi, "doi")
                if extra and extra.get("title"):
                    enrich_recs.append(extra)
        return _merge(enrich_recs)

    return None
