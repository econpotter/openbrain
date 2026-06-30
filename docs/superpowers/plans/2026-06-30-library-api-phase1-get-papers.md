# Library API Phase 1: deterministic `get_papers` + reference resolver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `get_library_record(query: str)` ILIKE-substring lookup with a deterministic, reference-based `get_papers(<doi,title,journal,authors,year>)` backed by one shared brain-level resolver.

**Architecture:** Add `brain.resolve_reference(**fields) -> list[dict]` — DOI exact when present, else a normalized-field conjunction (normalized-title equality + author-surname + year/journal corroboration). The `get_papers` MCP tool is a thin formatter over it. This resolver is the single matching primitive that `find_papers` and the `update_`/`remove_` tools will reuse in later phases. Hard cut: `get_library_record` (brain fn + tool) is removed, not aliased.

**Tech Stack:** Python 3, FastMCP, asyncpg/Postgres (mocked in tests), `unittest` run via pytest.

## Global Constraints

- **All DB access goes through `brain.py`.** Tool files call `brain.*`; no SQL in `tools/`.
- **No hardcoded model providers/names.** (N/A here, but holds.)
- **Run tests with `uv run python -m pytest`** (NOT `uv run pytest` — wrong binary). Tests are `unittest.TestCase`/`IsolatedAsyncioTestCase`; `tests/conftest.py` stubs asyncpg/pgvector/bibtexparser and sets `brain.RERANK_CONFIG = {}`.
- **Reference fields supported this phase:** `doi, title, journal, authors, year`. `issue`/`month` are deferred — the `library` table has no column for them yet (a later schema phase adds them).
- **Hard cut, no aliases.** Remove `get_library_record`; the only caller is its own MCP tool (verify with grep). The agentic-researcher client does **not** use it.
- Commit after each task.

---

## File Structure

- `openbrain/sources.py` — extend `_normalize_title` (apostrophe handling). Shared by the external fetch gate and the new resolver.
- `openbrain/brain.py` — add `_surnames()` helper + `resolve_reference()`; remove `get_library_record()`. Leave `find_library_records()` (still used by `update_library`/`remove_record` until their phase).
- `openbrain/tools/library.py` — replace the `get_library_record` tool with `get_papers`.
- `tests/test_sources.py` — normalization test.
- `tests/test_brain.py` — `resolve_reference` tests (reuse `_FetchConnection`/`_Pool`).

---

### Task 1: Robust title normalization (apostrophes)

The current `_normalize_title` turns every non-alphanumeric char into a space, so `"Cattlemen's"` → `"cattlemen s"` ≠ `"cattlemens"`. Delete apostrophes before the space-substitution so apostrophe noise stops blocking matches. This also relaxes the external `fetch_paper` title gate (same helper) — intended, per the spec.

**Files:**
- Modify: `openbrain/sources.py` (`_normalize_title`, ~line 537)
- Test: `tests/test_sources.py`

**Interfaces:**
- Produces: `sources._normalize_title(str|None) -> str` (unchanged signature; apostrophes now removed, not spaced).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sources.py`, inside the existing module (e.g. after `CleanAbstractTests`):

```python
class NormalizeTitleTests(unittest.TestCase):
    def test_apostrophes_are_deleted_not_spaced(self):
        # "Cattlemen's" must equal "Cattlemens" so apostrophe noise can't block a match.
        self.assertEqual(
            sources._normalize_title("Cattlemen's Association"),
            sources._normalize_title("Cattlemens Association"),
        )

    def test_still_collapses_punctuation_and_case(self):
        self.assertEqual(
            sources._normalize_title("  Optimal,  Bandwidth   Choice. "),
            "optimal bandwidth choice",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_sources.py::NormalizeTitleTests -v`
Expected: `test_apostrophes_are_deleted_not_spaced` FAILS (`'cattlemen s association' != 'cattlemens association'`).

- [ ] **Step 3: Implement the change**

In `openbrain/sources.py`, edit `_normalize_title` to delete apostrophes before substituting other punctuation:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sources.py -v`
Expected: PASS (new class + existing `TitleExactMatchTests` still green).

- [ ] **Step 5: Commit**

```bash
git add openbrain/sources.py tests/test_sources.py
git commit -m "fix(sources): delete apostrophes in title normalization so noise doesn't block matches"
```

---

### Task 2: `resolve_reference` deterministic resolver

**Files:**
- Modify: `openbrain/brain.py` (add `_surnames` + `resolve_reference` near the library helpers, ~line 2189)
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `sources._normalize_title` (Task 1), `brain._source_slug` (exists, line 1142), `brain.get_pool`.
- Produces:
  ```python
  def _surnames(authors: list[str] | str | None) -> set[str]
  async def resolve_reference(
      *, doi: str | None = None, title: str | None = None,
      journal: str | None = None, authors: list[str] | str | None = None,
      year: int | None = None,
  ) -> list[dict]   # 0, 1, or many matching library rows
  ```
  Each row dict has keys: `id, title, authors, year, source, doc_type, status, filepath, doi, abstract`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_brain.py` (reuses `_FetchConnection` and `_Pool` already defined in the file). Place after the existing library/paper test classes:

```python
def _lib_row(**kw):
    base = {
        "id": "x", "title": "T", "authors": [], "year": None, "source": None,
        "doc_type": "paper", "status": "ingested", "filepath": None,
        "doi": None, "abstract": None,
    }
    base.update(kw)
    return base


class ReferenceResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_doi_is_exact_and_authoritative(self):
        rows = [_lib_row(id="1", title="Whatever", doi="10.1/x")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(doi="https://doi.org/10.1/x")
        self.assertEqual([r["id"] for r in out], ["1"])
        self.assertIn("doi =", conn.calls[0][0])  # took the exact-DOI branch

    async def test_title_apostrophe_noise_matches(self):
        rows = [
            _lib_row(id="1", title="Cattlemen's Association", year=1990),
            _lib_row(id="2", title="Unrelated Paper", year=1990),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="cattlemens association")
        self.assertEqual([r["id"] for r in out], ["1"])

    async def test_author_surname_corroboration_disambiguates(self):
        rows = [
            _lib_row(id="1", title="Shared Title", authors=["Jane Doe"], year=2001),
            _lib_row(id="2", title="Shared Title", authors=["John Smith"], year=2001),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="shared title", authors=["Doe"])
        self.assertEqual([r["id"] for r in out], ["1"])

    async def test_returns_multiple_when_ambiguous(self):
        rows = [
            _lib_row(id="1", title="Shared Title", authors=["Jane Doe"]),
            _lib_row(id="2", title="Shared Title", authors=["John Smith"]),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="shared title")
        self.assertEqual({r["id"] for r in out}, {"1", "2"})

    async def test_nonmatching_title_returns_empty(self):
        rows = [_lib_row(id="1", title="Some Paper")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="a totally different title")
        self.assertEqual(out, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_brain.py::ReferenceResolveTests -v`
Expected: FAIL with `AttributeError: module 'openbrain.brain' has no attribute 'resolve_reference'`.

- [ ] **Step 3: Implement `_surnames` + `resolve_reference`**

In `openbrain/brain.py`, add near `find_library_records` (~line 2189). Note `sources` is already imported in brain.py:

```python
_LIBRARY_COLS = (
    "id, title, authors, year, source, doc_type, status, filepath, doi, abstract"
)


def _surnames(authors) -> set[str]:
    """Lowercased, accent-stripped surnames from author strings ('Last, First'
    or 'First Last'). Reuses _normalize_title as the per-token normalizer."""
    if isinstance(authors, str):
        authors = [authors]
    out: set[str] = set()
    for a in authors or []:
        a = a.strip()
        if not a:
            continue
        token = a.split(",")[0] if "," in a else a.split()[-1]
        norm = sources._normalize_title(token)
        if norm:
            out.add(norm)
    return out


async def resolve_reference(
    *,
    doi: str | None = None,
    title: str | None = None,
    journal: str | None = None,
    authors=None,
    year: int | None = None,
) -> list[dict]:
    """Deterministic reference -> library rows. DOI exact when present; otherwise
    a conjunction of normalized fields: exact-normalized title equality, plus
    author-surname and year/journal corroboration. Returns 0, 1, or many rows;
    the caller decides what to do with ambiguity (never auto-picks here)."""
    pool = await get_pool()

    # 1. DOI is exact and authoritative.
    if doi:
        norm = sources._normalize_doi(doi) or doi.strip()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_LIBRARY_COLS} FROM library WHERE doi = $1", norm
            )
        return [dict(r) for r in rows]

    # 2. No DOI: coarse SQL prefilter on exact structured fields, then
    #    normalized-equality gate in Python (mirrors sources._normalize_title).
    clauses: list[str] = []
    params: list = []
    if year is not None:
        params.append(year)
        clauses.append(f"year = ${len(params)}")
    if journal:
        params.append(_source_slug(journal) or journal)
        params.append(f"%{journal}%")
        clauses.append(
            f"(venue_abbrev = ${len(params) - 1} OR source ILIKE ${len(params)})"
        )
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {_LIBRARY_COLS} FROM library{where}", *params)
    cands = [dict(r) for r in rows]

    if title:
        wanted = sources._normalize_title(title)
        cands = [r for r in cands if sources._normalize_title(r.get("title")) == wanted]
    if authors:
        wanted_surnames = _surnames(authors)
        if wanted_surnames:
            cands = [
                r for r in cands if wanted_surnames & _surnames(r.get("authors") or [])
            ]
    return cands
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_brain.py::ReferenceResolveTests -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(brain): add deterministic resolve_reference (DOI exact + normalized-field conjunction)"
```

---

### Task 3: `get_papers` tool replaces `get_library_record`

**Files:**
- Modify: `openbrain/tools/library.py` (replace the `get_library_record` tool, ~line 358)
- Modify: `openbrain/brain.py` (remove `get_library_record`, ~line 2207)
- Test: manual grep + full suite

**Interfaces:**
- Consumes: `brain.resolve_reference` (Task 2).
- Produces: MCP tool
  ```python
  async def get_papers(
      doi: str | None = None, title: str | None = None,
      journal: str | None = None, authors: str | None = None,
      year: int | None = None,
  ) -> str
  ```

- [ ] **Step 1: Verify the only caller is the tool itself**

Run: `grep -rn "get_library_record" openbrain/`
Expected: matches only in `openbrain/brain.py` (the `def` + the tool calling it) and `openbrain/tools/library.py` (the tool). If anything else appears, stop and migrate it too.

Run: `grep -rn "get_library_record" ../../agentic-researcher/src 2>/dev/null`
Expected: no matches (the researcher client does not use it).

- [ ] **Step 2: Replace the tool**

In `openbrain/tools/library.py`, replace the entire `get_library_record` tool (the `@mcp.tool` block at ~line 358) with:

```python
    @mcp.tool
    async def get_papers(
        doi: str | None = None,
        title: str | None = None,
        journal: str | None = None,
        authors: str | None = None,
        year: int | None = None,
    ) -> str:
        """
        Deterministically retrieve library record(s) by bibliographic reference —
        not by free-text search. Supply any subset of doi/title/journal/authors/
        year; DOI matches exactly, the rest match on normalized fields together.

        Use this when you know *which* paper(s) you want (you have a citation).
        Use find_papers for "papers about X" (semantic), or search by topic.

        Args:
            doi: DOI (exact, authoritative when present).
            title: Paper title (matched after normalization — case/punctuation/apostrophe insensitive).
            journal: Journal/venue name or canonical abbrev.
            authors: One author or a comma-separated list (surname is enough).
            year: Publication year (exact).
        """
        author_list = (
            [a.strip() for a in authors.split(",") if a.strip()] if authors else None
        )
        rows = await brain.resolve_reference(
            doi=doi, title=title, journal=journal, authors=author_list, year=year
        )
        if not rows:
            return "No library record matched the reference."
        if len(rows) > 1:
            lines = [f"{len(rows)} matches — disambiguate (pass a DOI or add fields):"]
            for r in rows:
                year_s = f" ({r['year']})" if r.get("year") else ""
                doi_s = f" doi:{r['doi']}" if r.get("doi") else ""
                lines.append(f"- {r['title']}{year_s}{doi_s} [{r['id']}]")
            return "\n".join(lines)

        record = rows[0]
        lines = [f"## {record['title']}"]
        authors_out = record["authors"] or []
        if authors_out:
            lines.append("Authors: " + ", ".join(authors_out))
        if record["year"]:
            lines.append(f"Year: {record['year']}")
        if record["source"]:
            lines.append(f"Source: {record['source']}")
        if record["doi"]:
            lines.append(f"DOI: {record['doi']}")
        if record["doc_type"]:
            lines.append(f"Type: {record['doc_type']}")
        if record["status"]:
            lines.append(f"Status: {record['status']}")
        if record["filepath"]:
            lines.append(f"Path: {record['filepath']}")
        if record.get("abstract"):
            abstract = record["abstract"][:500].replace("\n", " ")
            lines.append(f"Abstract: {abstract}")
        return "\n".join(lines)
```

- [ ] **Step 3: Remove the dead brain function**

In `openbrain/brain.py`, delete the `async def get_library_record(query: str)` function (~line 2207, the 6-line body that calls `find_library_records`). Leave `find_library_records` and `update_library_record` in place — `update_library`/`remove_record` still use them (migrated in a later phase).

- [ ] **Step 4: Verify nothing references the removed function**

Run: `grep -rn "brain.get_library_record\|def get_library_record" openbrain/`
Expected: no matches.

Run: `uv run python -m pytest -v`
Expected: PASS (full suite; no test referenced the removed tool/fn).

Run: `uv run python -c "from openbrain.server import build_server" 2>/dev/null || uv run python -c "import openbrain.tools.library"`
Expected: imports clean (no syntax/name errors in the rewritten tool).

- [ ] **Step 5: Commit**

```bash
git add openbrain/tools/library.py openbrain/brain.py
git commit -m "feat(library): replace get_library_record with deterministic get_papers tool"
```

---

## Self-Review

**Spec coverage (Phase 1 slice of `2026-06-30-library-api-three-jobs.md`):**
- `get_` = deterministic by reference field → Task 2 + 3. ✓
- Robustness = better normalization, not fuzzy ranking → Task 1 (apostrophe), Task 2 (normalized equality + corroboration). ✓
- >1 match → return candidates, never guess → Task 3 multi-match branch. ✓
- Kills the `query: str` ILIKE defect for `get_` → Task 3 removes `get_library_record`. ✓
- One shared resolver for later `find_papers`/`update_`/`remove_` reuse → `resolve_reference` (Task 2). ✓
- `issue`/`month` deferred (no column) → Global Constraints. ✓
- Out of Phase 1 (later phases): `find_papers`, `fetch_papers`/`store_papers`/`acquire` fold, schema/status removal + `chunks.source`, driver split (`fetch_citations`/`score_relevance`) + researcher client migration. Noted, not implemented here.

**Placeholder scan:** No TBD/“add error handling”/“similar to”/bare “write tests”. All steps carry runnable code/commands. ✓

**Type consistency:** `resolve_reference` returns `list[dict]` with the `_LIBRARY_COLS` keys; `get_papers` reads exactly those keys (`id,title,authors,year,source,doc_type,status,filepath,doi,abstract`). `_surnames` accepts `list|str|None` and is called both ways. ✓

## Notes for later phases
- `update_library`/`remove_record` still call `find_library_records` (ILIKE). Migrate them to `resolve_reference` and delete `find_library_records` when their phase lands.
- The `status` field is still selected/printed; it disappears in the schema/derivation-state phase, which will also drop it from `_LIBRARY_COLS` and `get_papers` output.
