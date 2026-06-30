# Library API Phase 2: `find_papers` semantic retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the `search_library` MCP tool to `find_papers` and add anchor-by-ids and anchor-by-reference modes (centroid semantic search), reusing the Phase 1 `resolve_reference`. Migrate the agentic-researcher client in lockstep (hard cut, no alias).

**Architecture:** Extract a shared `_search_chunks_by_vector(embedding, …)` core from `search_library_vec`; add `search_library_by_anchors(anchor_ids, …)` that builds a centroid from the anchors' stored embeddings and searches with it (excluding the anchors). The `find_papers` tool picks one mode — ids > reference > text — resolving references through `brain.resolve_reference`, and returns `{anchors, ids, results}`.

**Tech Stack:** Python 3, FastMCP, asyncpg/pgvector (mocked in tests), `unittest` via pytest. Spans two repos: `openbrain` and `agentic-researcher`.

## Global Constraints

- **All DB access in `brain.py`.** Tools call `brain.*`; no SQL in `tools/`.
- **Run openbrain tests with `uv run python -m pytest`**; researcher tests with `uv run pytest` (its justfile `test` recipe).
- **Hard cut, no alias.** After this phase, no `search_library` *tool* reference may remain in either repo. The brain-level fn `search_library_vec` keeps its name (internal).
- **Mode precedence in `find_papers`:** `ids` > reference (`doi/title/journal/authors/year`) > `text`. Exactly one runs. None → instructional error string.
- **Result JSON keys:** `anchors` (list of `{id,title,year}`; `[]` in text mode), `ids` (distinct library ids in rank order, ≤ limit), `results` (per-chunk records). Anchors are excluded from `results`.
- **Centroid path skips rerank** (no query text); text path keeps `_reranked`.
- Commit after each task.

---

## File Structure

- `openbrain/brain.py` — extract `_search_chunks_by_vector`; add `search_library_by_anchors`. `search_library_vec` becomes a thin wrapper (text → embed → core → rerank).
- `openbrain/tools/library.py` — replace `search_library` tool with `find_papers`.
- `openbrain/tests/{test_brain,test_driver,test_acquire}.py` — new brain tests; rename tool refs.
- `openbrain/{server.py,resources.py}`, `integrations/**` — prose ref updates.
- `agentic-researcher/src/researcher/{openbrain,cli}.py`, `tests/test_cli.py` — client migration.

---

### Task 1: Brain — shared vector-search core + anchor (centroid) search

**Files:**
- Modify: `openbrain/brain.py` (`search_library_vec` ~line 2035; add helpers near it)
- Test: `openbrain/tests/test_brain.py`

**Interfaces:**
- Consumes: `embed`, `_candidate_count`, `_reranked`, `_mean_vector`, `get_pool` (all exist).
- Produces:
  ```python
  async def _search_chunks_by_vector(
      embedding, threshold: float, count: int,
      year_min: int | None, doc_type: str | None,
      exclude_ids: list[str] | None = None,
  ) -> list[dict]
  async def search_library_by_anchors(
      anchor_ids: list[str], threshold: float = 0.4, count: int = 10,
      year_min: int | None = None, doc_type: str | None = None,
  ) -> list[dict]   # raises ValueError if no anchor_ids or none has an embedding
  ```
  `search_library_vec(query, threshold=0.4, count=10, year_min=None, doc_type=None)` keeps its signature and behavior.

- [ ] **Step 1: Write the failing tests**

Add to `openbrain/tests/test_brain.py` (a self-contained connection stub; place ABOVE any `if __name__ == "__main__"` guard near the insertion point):

```python
class _AnchorSearchConnection:
    """First fetch returns anchor embeddings; second returns candidate chunk rows."""

    def __init__(self, anchor_embeddings, candidate_rows):
        self.anchor_embeddings = anchor_embeddings
        self.candidate_rows = candidate_rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "SELECT c.embedding FROM chunks c" in query:
            return [{"embedding": e} for e in self.anchor_embeddings]
        return self.candidate_rows


class AnchorSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_centroid_search_excludes_anchors_and_returns_rows(self):
        cand = [
            {
                "chunk_id": "c1", "library_id": "lib-2", "title": "Neighbor",
                "authors": ["A"], "year": 2020, "source": "J", "doc_type": "paper",
                "status": "ingested", "chunk_text": "text", "section": None,
                "similarity": 0.9,
            }
        ]
        conn = _AnchorSearchConnection([[1.0, 0.0], [0.0, 1.0]], cand)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            rows = await brain.search_library_by_anchors(["lib-1"], count=5)
        self.assertEqual([r["library_id"] for r in rows], ["lib-2"])
        # The candidate query must carry an exclude-ids clause for the anchors.
        candidate_query = conn.calls[1][0]
        self.assertIn("!= ALL", candidate_query)

    async def test_raises_when_no_anchor_embeddings(self):
        conn = _AnchorSearchConnection([], [])
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            with self.assertRaises(ValueError):
                await brain.search_library_by_anchors(["lib-1"])

    async def test_raises_when_no_anchor_ids(self):
        with self.assertRaises(ValueError):
            await brain.search_library_by_anchors([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest openbrain/tests/test_brain.py::AnchorSearchTests -v` (run from repo root; if that path errors, use `tests/test_brain.py`).
Expected: FAIL with `AttributeError: ... has no attribute 'search_library_by_anchors'`.

- [ ] **Step 3: Refactor `search_library_vec` + add the two functions**

In `openbrain/brain.py`, replace the existing `search_library_vec` (the whole function, ~lines 2035–2078) with the three functions below. The SQL body is lifted verbatim from the current function into the shared core, with one added optional `exclude_ids` clause:

```python
async def _search_chunks_by_vector(
    embedding,
    threshold: float,
    count: int,
    year_min: int | None,
    doc_type: str | None,
    exclude_ids: list[str] | None = None,
) -> list[dict]:
    """Core nearest-chunk search by a precomputed embedding vector. No rerank
    (callers that have query text rerank the returned rows themselves)."""
    pool = await get_pool()
    conditions = ["c.embedding IS NOT NULL", "1 - (c.embedding <=> $1) > $2"]
    params: list = [embedding, threshold]
    p = 3
    if year_min is not None:
        conditions.append(f"l.year >= ${p}")
        params.append(year_min)
        p += 1
    if doc_type is not None:
        conditions.append(f"l.doc_type = ${p}")
        params.append(doc_type)
        p += 1
    if exclude_ids:
        conditions.append(f"l.id != ALL(${p}::uuid[])")
        params.append(list(exclude_ids))
        p += 1
    where = " AND ".join(conditions)
    params.append(_candidate_count(count))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id AS chunk_id, l.id AS library_id, l.title, l.authors,
                   l.year, l.source, l.doc_type, l.status,
                   c.content AS chunk_text, c.section,
                   1 - (c.embedding <=> $1) AS similarity
            FROM chunks c
            JOIN library l ON l.id = c.library_id
            WHERE {where}
            ORDER BY similarity DESC
            LIMIT ${p}
            """,
            *params,
        )
    return [dict(r) for r in rows]


async def search_library_vec(
    query: str,
    threshold: float = 0.4,
    count: int = 10,
    year_min: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    embedding = await embed(query, task="query")
    rows = await _search_chunks_by_vector(
        embedding, threshold, count, year_min, doc_type
    )
    return await _reranked(query, rows, lambda r: r["chunk_text"], count)


async def search_library_by_anchors(
    anchor_ids: list[str],
    threshold: float = 0.4,
    count: int = 10,
    year_min: int | None = None,
    doc_type: str | None = None,
) -> list[dict]:
    """Centroid of the anchors' stored chunk embeddings -> nearest library
    chunks, excluding the anchors themselves. No cross-encoder rerank: a
    centroid has no query text. Raises ValueError when no anchor ids are given
    or none has a usable embedding."""
    if not anchor_ids:
        raise ValueError("search_library_by_anchors requires at least one anchor id.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.embedding FROM chunks c "
            "WHERE c.library_id = ANY($1::uuid[]) AND c.embedding IS NOT NULL",
            list(anchor_ids),
        )
    vectors = [list(r["embedding"]) for r in rows if r["embedding"] is not None]
    if not vectors:
        raise ValueError("No stored embeddings for the given anchor ids.")
    centroid = _mean_vector(vectors)
    return await _search_chunks_by_vector(
        centroid, threshold, count, year_min, doc_type, exclude_ids=list(anchor_ids)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_brain.py -v`
Expected: PASS — new `AnchorSearchTests` (3) green AND the existing `test_search_library_applies_year_and_document_type_filters` still green (the refactor preserves `search_library_vec` behavior).

- [ ] **Step 5: Commit**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(brain): extract vector-search core; add search_library_by_anchors (centroid)"
```

---

### Task 2: Tool — `search_library` → `find_papers` with anchor modes

**Files:**
- Modify: `openbrain/tools/library.py` (`search_library` tool, ~line 305)
- Modify: `openbrain/tests/test_driver.py` (~lines 133–158), `openbrain/tests/test_acquire.py` (~lines 288–297)
- Modify: `openbrain/server.py`, `openbrain/resources.py`, `integrations/skills/openbrain-search/SKILL.md`, `integrations/README.md` (prose refs)

**Interfaces:**
- Consumes: `brain.resolve_reference` (Phase 1), `brain.search_library_vec`, `brain.search_library_by_anchors` (Task 1).
- Produces: MCP tool `find_papers(text=None, ids=None, doi=None, title=None, journal=None, authors=None, year=None, limit=10, year_min=None, doc_type=None, threshold=0.4) -> str` returning JSON `{anchors, ids, results}`.

- [ ] **Step 1: Find every `search_library` tool reference in openbrain**

Run: `grep -rn "search_library\b" openbrain/ integrations/ tests/`
Expected matches to migrate (record them): the tool def + docstring cross-ref in `tools/library.py`; `tests/test_driver.py`; `tests/test_acquire.py`; `server.py`; `resources.py`; `integrations/skills/openbrain-search/SKILL.md`; `integrations/README.md`. Note: `search_library_vec` (brain fn) is a DIFFERENT name — do NOT rename it.

- [ ] **Step 2: Replace the tool**

In `openbrain/tools/library.py`, replace the entire `search_library` `@mcp.tool` block (~lines 305–356) with:

```python
    @mcp.tool
    async def find_papers(
        text: str | None = None,
        ids: str | None = None,
        doi: str | None = None,
        title: str | None = None,
        journal: str | None = None,
        authors: str | None = None,
        year: int | None = None,
        limit: int = 10,
        year_min: int | None = None,
        doc_type: str | None = None,
        threshold: float = 0.4,
    ) -> str:
        """
        Topical/semantic search over the library — "papers related to X".

        Anchor it one of three ways (precedence: ids > reference > text):
          - text: a natural-language topic ("water economics").
          - ids: comma-separated library paper ids — returns papers similar to
            those (their centroid), excluding the anchors themselves.
          - reference: doi/title/journal/authors/year — resolves the known
            paper(s) (via get_papers' resolver) and anchors on them.

        Use get_papers when you know *which* paper you want (deterministic).

        Returns JSON: `anchors` (resolved anchor papers, [] for text mode),
        `ids` (distinct library ids in rank order), and `results` (matching
        records with title, authors, year, similarity, and an excerpt).

        Args:
            text:      Natural-language topic query.
            ids:       Comma-separated library paper ids to anchor on.
            doi/title/journal/authors/year: Bibliographic reference to anchor on.
            limit:     Max distinct papers. Default 10.
            year_min:  Only documents from this year or later.
            doc_type:  Filter by type (paper | report | documentation | …).
            threshold: Minimum similarity (0–1). Default 0.4.
        """
        anchors: list[dict] = []
        anchor_ids: list[str] | None = None

        if ids:
            anchor_ids = [s.strip() for s in ids.split(",") if s.strip()]
        elif any(v is not None for v in (doi, title, journal, authors, year)):
            author_list = (
                [a.strip() for a in authors.split(",") if a.strip()]
                if authors
                else None
            )
            anchors = await brain.resolve_reference(
                doi=doi, title=title, journal=journal, authors=author_list, year=year
            )
            if not anchors:
                return "No library record matched the reference; nothing to anchor on."
            anchor_ids = [str(a["id"]) for a in anchors]

        if anchor_ids:
            try:
                rows = await brain.search_library_by_anchors(
                    anchor_ids, threshold, limit, year_min, doc_type
                )
            except ValueError as exc:
                return f"Cannot find related papers: {exc}"
        elif text:
            rows = await brain.search_library_vec(
                text, threshold, limit, year_min, doc_type
            )
        else:
            return (
                "Provide text, ids, or a bibliographic reference "
                "(doi/title/journal/authors/year)."
            )

        result_ids: list[str] = []
        results = []
        for r in rows:
            lid = str(r["library_id"])
            if lid not in result_ids:
                result_ids.append(lid)
            results.append(
                {
                    "id": lid,
                    "title": r["title"],
                    "authors": r["authors"] or [],
                    "year": r["year"],
                    "source": r["source"],
                    "similarity": r["similarity"],
                    "section": r.get("section"),
                    "excerpt": r["chunk_text"][:300].replace("\n", " "),
                }
            )
            if len(result_ids) >= limit:
                break

        payload = {
            "anchors": [
                {"id": str(a["id"]), "title": a["title"], "year": a["year"]}
                for a in anchors
            ],
            "ids": result_ids,
            "results": results,
        }
        return json.dumps(payload, indent=2, default=str)
```

Also update the `search_library` cross-reference in the `get_papers` docstring if present (it points readers to the semantic tool): change "find_papers for ..." text to remain accurate (the get_papers docstring from Phase 1 already says "Use find_papers for ..." — verify it reads correctly; no change needed if it already says `find_papers`).

- [ ] **Step 3: Update the in-repo references (hard cut)**

In `openbrain/tests/test_driver.py`: change `tools["search_library"]` → `tools["find_papers"]` (lines ~136, 149, 158) and the call args to the new keyword form. The existing calls look like `tools["search_library"]("q", limit=10)`; the new tool's first positional param is `text`, so `tools["find_papers"]("q", limit=10)` still binds `text="q"` — only the tool key changes. Update the file's module docstring/comment at line ~8 (`search_library -> {ids}`) to `find_papers -> {ids}`. The patched brain target `openbrain.brain.search_library_vec` is unchanged (text mode still calls it) — leave it.

In `openbrain/tests/test_acquire.py` (`test_cli_search_library_emits_distinct_ids_top_k`, ~line 288): it patches `openbrain.brain.search_library_vec` (unchanged) but if it invokes the tool by name, change `search_library` → `find_papers`. If it only patches the brain fn and exercises a CLI path, leave the brain patch as-is and update only any tool-name string.

In `openbrain/server.py` and `openbrain/resources.py`: replace prose `search_library` → `find_papers`.

In `integrations/skills/openbrain-search/SKILL.md` and `integrations/README.md`: replace `search_library` → `find_papers`.

- [ ] **Step 4: Verify no `search_library` tool reference remains; run suite**

Run: `grep -rn "search_library\b" openbrain/ integrations/ tests/`
Expected: ZERO matches for the bare tool name `search_library`. (Matches for `search_library_vec` are fine — that is the retained brain fn.)

Run: `uv run python -m pytest -q`
Expected: PASS (full suite).

Run: `uv run python -c "import openbrain.tools.library"`
Expected: imports clean.

- [ ] **Step 5: Commit**

```bash
git add openbrain/tools/library.py openbrain/server.py openbrain/resources.py integrations/ tests/test_driver.py tests/test_acquire.py
git commit -m "feat(library): rename search_library tool to find_papers; add ids/reference anchor modes"
```

---

### Task 3: Migrate the agentic-researcher client (lockstep)

**Files (in `/home/potterzot/orchestra/projects/agentic-researcher`):**
- Modify: `src/researcher/openbrain.py` (method ~line 108; docstring ~line 9–11)
- Modify: `src/researcher/cli.py` (~line 94)
- Modify: `tests/test_cli.py` (stub ~line 59)

**Interfaces:**
- Consumes: openbrain MCP tool `find_papers` (Task 2). Param rename `query` → `text`.
- Produces: `OpenBrain.find_papers(self, text: str, limit: int) -> list[str]`.

- [ ] **Step 1: Find every `search_library` reference in the researcher repo**

Run (from researcher repo root): `grep -rn "search_library" src/ tests/`
Expected: `src/researcher/openbrain.py` (docstring + method + `_call`), `src/researcher/cli.py:94`, `tests/test_cli.py:59`.

- [ ] **Step 2: Rename the client method**

In `src/researcher/openbrain.py`, replace the method (~lines 108–111):

```python
    def find_papers(self, text: str, limit: int) -> list[str]:
        result = self._call("find_papers", {"text": text, "limit": limit})
        assert isinstance(result, dict)
        return [str(x) for x in result["ids"]]
```

In the module docstring (~lines 9–11), replace `search_library` with `find_papers` in both the prose and the tool list.

- [ ] **Step 3: Update the CLI call site**

In `src/researcher/cli.py:94`, change:

```python
            ids = ob.find_papers(args.query, cfg.core_bootstrap_count)
```

- [ ] **Step 4: Update the test stub**

In `tests/test_cli.py` (~line 59), rename the stand-in method `search_library` → `find_papers`, keeping the same signature shape it had (`def find_papers(self, text, limit):`) and body. If the test asserts on the method name or call args elsewhere in the file, update those to match.

- [ ] **Step 5: Verify and run the researcher suite**

Run (researcher repo root): `grep -rn "search_library" src/ tests/`
Expected: ZERO matches.

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit (in the researcher repo)**

```bash
git add src/researcher/openbrain.py src/researcher/cli.py tests/test_cli.py
git commit -m "refactor: migrate openbrain search_library -> find_papers (param query->text)"
```

---

## Self-Review

**Spec coverage (`2026-06-30-find-papers-phase2.md`):**
- Three anchor modes, precedence ids > reference > text → Task 2 tool logic. ✓
- Reference delegates to `resolve_reference` → Task 2. ✓
- Centroid search excludes anchors, no rerank → Task 1 (`exclude_ids`, core without rerank). ✓
- `anchors` in result for transparency → Task 2 payload. ✓
- Empty-embedding anchors → ValueError → readable tool message → Task 1 raise + Task 2 try/except. ✓
- `search_library_vec` name retained; only tool renamed → Task 1/2. ✓
- Hard-cut cross-repo migration → Task 2 (openbrain refs) + Task 3 (researcher). ✓

**Placeholder scan:** No TBD/“handle errors”/“similar to”. All code steps carry full code; verification steps carry exact grep/test commands and expected output. The only conditional instructions (Task 2 Step 3, Task 3 Step 4) are “update if the file references X” with the concrete edit named — acceptable because the exact line content is environment-dependent, and Step 1 grep enumerates the real references first.

**Type consistency:** `search_library_by_anchors(anchor_ids: list[str], …)` defined in Task 1, called in Task 2 with `anchor_ids: list[str]`. `_search_chunks_by_vector` returns `list[dict]` with keys consumed in Task 2's results loop (`library_id,title,authors,year,source,similarity,section,chunk_text`). `find_papers` JSON keys match the spec's result shape. Client `find_papers(text, limit)` matches the tool's `text`/`limit` params.

## Notes for later phases
- The retained brain fn `search_library_vec` and the `search_library_by_anchors` name are internal; a future cleanup could unify them under one `find_*` brain name, but not required.
- Carry-over Phase 1 Minors still open (no-arg `get_papers` guard, journal-abbrev clause) — unrelated to this phase.
