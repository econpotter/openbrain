# Library API Phase 3: fetch/store consolidation + shared fuzzy matcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse paper acquisition to `fetch_papers` (external → ob) + `store_papers` (store a chosen list); make reference matching one shared fuzzy scorer used by BOTH `get_papers` (internal) and `fetch_papers` (external); remove the `acquire` tool and its OpenAlex-only/relevance-gated pipeline.

**Architecture:** A single stdlib scorer in `sources.py` (`_score_candidate`: normalized-title `difflib` similarity + author/year/journal corroboration, accepted above a threshold). `get_papers`/`resolve_reference` score library rows; `fetch_papers` scores source results. Known-item fetch queries sources **in order, short-circuits** on the first above-threshold hit, then **enriches the winner** from the remaining sources. Topic fetch **fans out all sources** (dedup) and returns candidates, storing only when `store=True`. `store_papers` does the mechanical prep (crossref enrich + venue canon) then stores. The `acquire` pipeline (paginated OpenAlex search, lexical recall-gate, venue agenda) leaves openbrain entirely.

**Tech Stack:** Python 3, FastMCP, asyncpg/pgvector (mocked), `unittest` via pytest. `sources.py` is stdlib-only — the scorer uses `difflib`.

## Global Constraints

- **One matcher for both jobs.** `get_papers` (internal) and `fetch_papers` (external) tolerate slight misspellings on title/journal/authors via the SAME `sources._score_candidate` + threshold. "Deterministic" (get_papers) means rule-based and repeatable — a fixed scorer + fixed threshold qualifies; it is NOT exact-equality, and NOT embeddings (that is `find_papers`).
- **`fetch_` = bring external papers INTO openbrain.** Defaults by input:
  - `doi`/`identifiers`/title (known-item) → fetch **+ store**.
  - `topic` → candidates, **no store**; `store=True` → bulk store.
- **Known-item query = short-circuit + enrich.** Query sources in configured order; the first whose best candidate clears the threshold (DOI/arXiv: exact id) wins; enrich that winner from the remaining sources by its DOI. Do not query every source for a single paper.
- **Topic query = fan out all sources**, dedup.
- **No relevance policy / no agenda in openbrain.** The `min_overlap` recall-gate, paginated venue harvest, and venue agenda are removed (driver territory). Mechanical prep (dedup, crossref enrich, venue canon) is retained in the store path.
- **All DB access in `brain.py`.** SQL stays in brain.
- **Hard cut, no alias.** No `acquire`/`fetch_paper`(singular)/`store_paper` tool reference may remain in openbrain. Researcher client migrates in a separate lockstep issue.
- **Run tests with `uv run python -m pytest`.** Commit after each task.

---

## File Structure

- `openbrain/sources.py` — add `_title_similarity`, `_ref_surnames`, `_score_candidate`, `MATCH_THRESHOLD`; rewrite `resolve` known-item path (short-circuit + enrich) — topic path already fans out.
- `openbrain/brain.py` — `resolve_reference` scores instead of exact-equality; `store_one` → `store_papers` (enrich + canon + store); `fetch_paper` known-item plumbs threshold/corroboration, topic routes to `resolve(topic)` + `store` flag.
- `openbrain/acquire.py` — delete `acquire()` + `prefilter()` (pipeline gone); keep `dedup_records` (used by store path) or relocate it into brain.
- `openbrain/venues.py` — unchanged (canon engine).
- `openbrain/tools/{library,driver,acquire}.py` — `fetch_papers`, `store_papers`, remove `acquire` tool.
- `openbrain/{server.py,resources.py}`, `integrations/**`, `tests/{test_sources,test_brain,test_driver,test_acquire}.py`.

---

### Task 1: Shared fuzzy scorer in `sources.py`

**Files:**
- Modify: `openbrain/sources.py` (add `import difflib`; helpers near `_normalize_title`)
- Test: `openbrain/tests/test_sources.py`

**Interfaces:**
- Produces:
  ```python
  MATCH_THRESHOLD: float = 0.8
  def _title_similarity(a: str | None, b: str | None) -> float
  def _ref_surnames(authors) -> set[str]
  def _score_candidate(ref_title: str | None, cand: dict, *, year=None, authors=None) -> float
  ```

- [ ] **Step 1: Write the failing tests**

Add to `openbrain/tests/test_sources.py` (above any `__main__` guard):

```python
class ScoreCandidateTests(unittest.TestCase):
    def test_near_title_scores_high(self):
        cand = {"title": "Optimal Bandwidth Choice for Regression"}
        s = sources._score_candidate("Optimal Bandwidth Choice for Regressions", cand)
        self.assertGreaterEqual(s, sources.MATCH_THRESHOLD)

    def test_unrelated_title_scores_low(self):
        cand = {"title": "QuickStats: Suicide Rates for Teens"}
        s = sources._score_candidate("Land Speculation and Development", cand)
        self.assertLess(s, sources.MATCH_THRESHOLD)

    def test_year_and_author_corroboration_add(self):
        cand = {"title": "Water Policy", "year": 2015, "authors": ["Jane Doe"]}
        base = sources._score_candidate("Water Policy", cand)
        boosted = sources._score_candidate("Water Policy", cand, year=2015, authors=["Doe"])
        self.assertGreater(boosted, base)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run python -m pytest tests/test_sources.py::ScoreCandidateTests -v` → FAIL (`_score_candidate` undefined).

- [ ] **Step 3: Implement**

In `openbrain/sources.py` add `import difflib` at the top, and near `_normalize_title`:

```python
MATCH_THRESHOLD = 0.8


def _title_similarity(a: str | None, b: str | None) -> float:
    """0..1 ratio of two normalized titles via difflib (stdlib)."""
    return difflib.SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _ref_surnames(authors) -> set[str]:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_sources.py::ScoreCandidateTests -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add openbrain/sources.py tests/test_sources.py
git commit -m "feat(sources): shared fuzzy reference scorer (difflib title sim + corroboration)"
```

---

### Task 2: `get_papers`/`resolve_reference` → fuzzy (use the shared scorer)

Converts the merged Phase 1 exact-normalized title equality to fuzzy scoring, so internal lookups tolerate misspellings too. DOI stays exact.

**Files:**
- Modify: `openbrain/brain.py` (`resolve_reference`, ~line added in Phase 1)
- Test: `openbrain/tests/test_brain.py`

**Interfaces:**
- Consumes: `sources._score_candidate`, `sources.MATCH_THRESHOLD`.
- Produces: `resolve_reference(*, doi, title, journal, authors, year, min_score=None) -> list[dict]` (unchanged shape; title gate now fuzzy).

- [ ] **Step 1: Update the title-match tests**

In `openbrain/tests/test_brain.py` `ReferenceResolveTests`, the exact-equality cases change. Replace `test_title_apostrophe_noise_matches` and add a misspelling case (keep DOI-exact + ambiguity + author-corroboration tests):

```python
    async def test_title_fuzzy_match_tolerates_misspelling(self):
        rows = [
            _lib_row(id="1", title="Cattlemen's Association", year=1990),
            _lib_row(id="2", title="Totally Unrelated", year=1990),
        ]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="catllemens asociation")  # typos
        self.assertEqual([r["id"] for r in out], ["1"])

    async def test_title_below_threshold_returns_empty(self):
        rows = [_lib_row(id="1", title="Some Paper")]
        conn = _FetchConnection(rows)
        with patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(conn))):
            out = await brain.resolve_reference(title="an entirely different subject")
        self.assertEqual(out, [])
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run python -m pytest tests/test_brain.py::ReferenceResolveTests -v` → the misspelling test FAILS (exact gate drops it).

- [ ] **Step 3: Implement fuzzy gate**

In `openbrain/brain.py` `resolve_reference`, replace the Python title-equality filter (the `if title:` block that compared `sources._normalize_title(...) == wanted`) with scoring; keep the coarse SQL fetch and the DOI-exact branch unchanged:

```python
    if title:
        threshold = sources.MATCH_THRESHOLD if min_score is None else min_score
        scored = [
            (r, sources._score_candidate(title, r, year=year, authors=authors))
            for r in cands
        ]
        cands = [r for r, s in scored if s >= threshold]
        cands.sort(
            key=lambda r: sources._score_candidate(title, r, year=year, authors=authors),
            reverse=True,
        )
    if authors and not title:
        wanted_surnames = _surnames(authors)
        if wanted_surnames:
            cands = [r for r in cands if wanted_surnames & _surnames(r.get("authors") or [])]
    return cands
```

Add `min_score: float | None = None` to the `resolve_reference` signature. (The standalone author-only filter is kept for the no-title case; when a title is given, corroboration already rides inside `_score_candidate`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_brain.py -v` → PASS (fuzzy + existing DOI/ambiguity tests).

- [ ] **Step 5: Commit**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(brain): get_papers/resolve_reference use shared fuzzy scorer (tolerate misspellings)"
```

---

### Task 3: `fetch` known-item — short-circuit across sources + enrich winner

**Files:**
- Modify: `openbrain/sources.py` (`resolve` known-item branch)
- Test: `openbrain/tests/test_sources.py`

**Interfaces:**
- Produces: `resolve(identifier=None, topic=None, id_kind=None, order=None, limit=10, min_score=None, year=None, authors=None)`. Known-item: returns the first source's above-threshold match, enriched from later sources; topic: unchanged fan-out.

- [ ] **Step 1: Write the failing tests**

Replace `TitleExactMatchTests` in `openbrain/tests/test_sources.py` with:

```python
class KnownItemFetchTests(unittest.TestCase):
    def test_short_circuits_on_first_source_above_threshold(self):
        hit = sources.PaperMeta(title="Land Speculation and Development",
                                doi="10.1/x", source_name="openalex")
        later_called = {"n": 0}

        def later_search(q, n):
            later_called["n"] += 1
            return []

        registry = {
            "openalex": (lambda i, k: None, lambda q, n: [hit]),
            "crossref": (lambda i, k: None, later_search),
        }
        with patch.object(sources, "_REGISTRY", registry):
            merged = sources.resolve(
                identifier="Land Speculation and Development",
                order=["openalex", "crossref"],
            )
        self.assertEqual(merged["doi"], "10.1/x")
        self.assertEqual(later_called["n"], 0)  # never searched the 2nd source

    def test_enriches_winner_from_remaining_by_doi(self):
        winner = sources.PaperMeta(title="A Paper", doi="10.1/x",
                                   abstract=None, source_name="openalex")
        cross = sources.PaperMeta(title="A Paper", doi="10.1/x",
                                  abstract="The abstract.", bibtex="@a{}",
                                  source_name="crossref")
        registry = {
            "openalex": (lambda i, k: None, lambda q, n: [winner]),
            "crossref": (lambda i, k: cross if k == "doi" else None, lambda q, n: []),
        }
        with patch.object(sources, "_REGISTRY", registry):
            merged = sources.resolve(identifier="A Paper",
                                     order=["openalex", "crossref"])
        self.assertEqual(merged["abstract"], "The abstract.")  # filled from crossref
        self.assertEqual(merged["bibtex"], "@a{}")

    def test_returns_none_when_no_source_above_threshold(self):
        wrong = sources.PaperMeta(title="QuickStats Teens", doi="10.9/c",
                                  source_name="openalex")
        registry = {"openalex": (lambda i, k: None, lambda q, n: [wrong])}
        with patch.object(sources, "_REGISTRY", registry):
            self.assertIsNone(
                sources.resolve(identifier="Land Speculation and Development",
                                order=["openalex"])
            )

    def test_doi_known_item_exact_then_enrich(self):
        oa = sources.PaperMeta(title="P", doi="10.1/x", abstract="abs",
                               source_name="openalex")
        cr = sources.PaperMeta(title="P", doi="10.1/x", bibtex="@a{}",
                               source_name="crossref")
        registry = {
            "openalex": (lambda i, k: oa, lambda q, n: []),
            "crossref": (lambda i, k: cr, lambda q, n: []),
        }
        with patch.object(sources, "_REGISTRY", registry):
            merged = sources.resolve(identifier="10.1/x", id_kind="doi",
                                     order=["openalex", "crossref"])
        self.assertEqual(merged["abstract"], "abs")
        self.assertEqual(merged["bibtex"], "@a{}")  # enriched
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run python -m pytest tests/test_sources.py::KnownItemFetchTests -v` → FAIL.

- [ ] **Step 3: Rewrite the known-item branch**

In `openbrain/sources.py` `resolve`, add `min_score=None, year=None, authors=None` to the signature, and replace the `if identifier is not None:` block with short-circuit + enrich:

```python
    if identifier is not None:
        kind = id_kind or detect_identifier_kind(identifier)
        threshold = MATCH_THRESHOLD if min_score is None else min_score

        winner: PaperMeta | None = None
        remaining: list[str] = []
        for idx, name in enumerate(names):
            lookup_fn, search_fn = _REGISTRY[name]
            if kind == "title":
                cands = search_fn(identifier, limit)
                scored = [
                    (c, _score_candidate(identifier, c, year=year, authors=authors))
                    for c in cands
                ]
                if scored:
                    best, best_score = max(scored, key=lambda t: t[1])
                    if best_score >= threshold:
                        winner = best
            else:  # doi / arxiv — exact identifier
                rec = lookup_fn(identifier, kind)
                if rec and rec.get("title"):
                    winner = rec
            if winner is not None:
                remaining = names[idx + 1 :]
                break

        if winner is None:
            return None

        # Enrich the winner from the remaining sources by its DOI (fills missing
        # abstract/bibtex/issn). Winner is first, so its own fields win.
        enrich_recs: list[PaperMeta] = [winner]
        doi = winner.get("doi")
        if doi:
            for name in remaining:
                lookup_fn, _ = _REGISTRY[name]
                extra = lookup_fn(doi, "doi")
                if extra and extra.get("title"):
                    enrich_recs.append(extra)
        return _merge(enrich_recs)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_sources.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add openbrain/sources.py tests/test_sources.py
git commit -m "feat(sources): known-item fetch short-circuits across sources, enriches winner"
```

---

### Task 4: Brain — `store_papers` (enrich + canon + store)

**Files:**
- Modify: `openbrain/brain.py` (replace `store_one`; add `from openbrain import venues` if absent)
- Test: `openbrain/tests/test_brain.py`

**Interfaces:**
- Consumes: `store_acquired_paper`, `sources.crossref_lookup`, `venues.match`.
- Produces: `store_papers(papers: list[dict], *, force=False) -> {"stored","deduped","results":[{id,title,deduped}]}`.

- [ ] **Step 1: Write the failing tests**

```python
class StorePapersTests(unittest.IsolatedAsyncioTestCase):
    async def test_enriches_then_stores_and_summarizes(self):
        seen = []

        async def fake_store(meta, *, force=False):
            seen.append(meta)
            return {"id": f"id-{len(seen)}", "title": meta["title"],
                    "embedded": True, "deduped": False}

        with (
            patch.object(brain, "store_acquired_paper", fake_store),
            patch.object(brain, "get_pool", AsyncMock(return_value=_Pool(_FetchConnection([])))),
            patch.object(brain.sources, "crossref_lookup",
                         lambda d, k: {"abstract": "filled"}),
            patch.object(brain.venues, "match", AsyncMock(return_value="AJAE")),
        ):
            out = await brain.store_papers(
                [{"title": "A", "doi": "10.1/a"}]  # no abstract -> enriched
            )
        self.assertEqual(out["stored"], 1)
        self.assertEqual(seen[0]["abstract"], "filled")
        self.assertEqual(seen[0]["venue_abbrev"], "AJAE")

    async def test_empty_is_noop(self):
        out = await brain.store_papers([])
        self.assertEqual(out, {"stored": 0, "deduped": 0, "results": []})
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run python -m pytest tests/test_brain.py::StorePapersTests -v` → FAIL.

- [ ] **Step 3: Implement**

In `openbrain/brain.py`, ensure `from openbrain import venues` is imported (add if missing), and replace `store_one` (~line 1807) with:

```python
async def store_papers(papers: list[dict], *, force: bool = False) -> dict:
    """Store a caller-chosen list of paper metadata dicts. Per paper: crossref
    enrich (fill missing abstract/issn/bibtex when a DOI is present), venue canon
    (venue_abbrev), then store_acquired_paper (DOI dedup, abstract embedded,
    findings inline; no thought emitted). Returns a roll-up + per-paper rows."""
    if not papers:
        return {"stored": 0, "deduped": 0, "results": []}
    pool = await get_pool()
    loop = asyncio.get_event_loop()
    results: list[dict] = []
    stored = deduped = 0
    for raw in papers:
        meta = dict(raw)
        if meta.get("doi") and not meta.get("abstract"):
            extra = await loop.run_in_executor(
                None, lambda d=meta["doi"]: sources.crossref_lookup(d, "doi")
            )
            if extra:
                for key in ("abstract", "issn_l", "bibtex"):
                    if not meta.get(key) and extra.get(key):
                        meta[key] = extra[key]
                if extra.get("concepts"):
                    meta["concepts"] = list(
                        dict.fromkeys((meta.get("concepts") or []) + extra["concepts"])
                    )
        if not meta.get("venue_abbrev"):
            meta["venue_abbrev"] = await venues.match(
                pool,
                issn_l=meta.get("issn_l"),
                source_id=meta.get("venue_source_id"),
                name=meta.get("source"),
            )
            meta["issn"] = meta.get("issn_l")
        outcome = await store_acquired_paper(meta, force=force)
        if outcome.get("deduped"):
            deduped += 1
        else:
            stored += 1
        results.append({"id": outcome["id"], "title": outcome["title"],
                        "deduped": outcome.get("deduped", False)})
    return {"stored": stored, "deduped": deduped, "results": results}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_brain.py::StorePapersTests -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(brain): store_papers (enrich + venue canon + store) replacing store_one"
```

---

### Task 5: Brain — `fetch_paper` wiring (known-item store via store_papers; topic via resolve + store flag)

**Files:**
- Modify: `openbrain/brain.py` (`fetch_paper`, ~line 1870)
- Test: `openbrain/tests/test_brain.py`

**Interfaces:**
- Consumes: `sources.resolve`, `store_papers`, `_store_paper`.
- Produces: `fetch_paper(identifier=None, topic=None, doc_type="paper", source_order=None, limit=10, force=False, store=False, title_threshold=None, year=None, authors=None)`. Topic → `{"mode":"topic","candidates":[...],"stored":int,"stored_ids":[...]}`; known-item → `{"mode":"lookup","found":bool, ...stored fields}`.

- [ ] **Step 1: Write the failing tests**

```python
class FetchWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_topic_default_returns_candidates_no_store(self):
        with patch.object(brain.sources, "resolve",
                          lambda **k: [{"title": "A", "doi": "10.1/a"}]):
            out = await brain.fetch_paper(topic="water")
        self.assertEqual(out["mode"], "topic")
        self.assertEqual(out["stored"], 0)
        self.assertEqual(len(out["candidates"]), 1)

    async def test_topic_store_true_calls_store_papers(self):
        with (
            patch.object(brain.sources, "resolve",
                         lambda **k: [{"title": "A", "doi": "10.1/a"}]),
            patch.object(brain, "store_papers",
                         AsyncMock(return_value={"stored": 1, "deduped": 0,
                                                 "results": [{"id": "id-1"}]})),
        ):
            out = await brain.fetch_paper(topic="water", store=True)
        self.assertEqual(out["stored"], 1)
        self.assertEqual(out["stored_ids"], ["id-1"])

    async def test_known_item_passes_threshold_and_corroboration(self):
        captured = {}

        def fake_resolve(**kw):
            captured.update(kw)
            return {"title": "A", "doi": "10.1/a"}

        with (
            patch.object(brain.sources, "resolve", fake_resolve),
            patch.object(brain, "_store_paper",
                         AsyncMock(return_value={"id": "1", "title": "A",
                                                 "abstract_stored": True,
                                                 "embedded": True, "deduped": False})),
        ):
            out = await brain.fetch_paper(identifier="A title", year=2015,
                                          authors=["Doe"], title_threshold=0.7)
        self.assertTrue(out["found"])
        self.assertEqual(captured["min_score"], 0.7)
        self.assertEqual(captured["year"], 2015)
        self.assertEqual(captured["authors"], ["Doe"])
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run python -m pytest tests/test_brain.py::FetchWiringTests -v` → FAIL.

- [ ] **Step 3: Implement**

In `openbrain/brain.py`, rewrite `fetch_paper`:

```python
async def fetch_paper(
    identifier: str | None = None,
    topic: str | None = None,
    doc_type: str = "paper",
    source_order: list[str] | None = None,
    limit: int = 10,
    force: bool = False,
    store: bool = False,
    title_threshold: float | None = None,
    year: int | None = None,
    authors=None,
) -> dict:
    """Fetch from external sources. Topic mode fans out all sources and returns
    candidates (stores only when store=True). Known-item mode short-circuits +
    enriches one paper and stores it (force replaces an existing DOI match)."""
    loop = asyncio.get_event_loop()
    order = source_order or _paper_sources_order()

    if topic is not None:
        candidates = await loop.run_in_executor(
            None, lambda: sources.resolve(topic=topic, order=order, limit=limit)
        ) or []
        stored = 0
        stored_ids: list[str] = []
        if store and candidates:
            report = await store_papers(candidates, force=force)
            stored = report["stored"]
            stored_ids = [r["id"] for r in report["results"] if not r.get("deduped")]
        return {"mode": "topic", "candidates": candidates,
                "stored": stored, "stored_ids": stored_ids}

    if not identifier:
        raise ValueError("fetch_paper requires either identifier or topic.")

    meta = await loop.run_in_executor(
        None,
        lambda: sources.resolve(
            identifier=identifier, order=order,
            min_score=title_threshold, year=year, authors=authors,
        ),
    )
    if not meta:
        return {"mode": "lookup", "found": False, "identifier": identifier}
    stored = await _store_paper(meta, doc_type, force)
    return {"mode": "lookup", "found": True, **stored}
```

Note: the batch `fetch_papers` brain helper (list of identifiers) still loops `fetch_paper` per identifier — leave it, but add the new kwargs pass-through if it forwards them (it currently forwards `identifier`/`doc_type`/`source_order`/`force`; that is fine — batch is DOI/identifier-oriented).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/test_brain.py -v` → PASS (update/repoint any old topic test that patched `sources.resolve` differently).

- [ ] **Step 5: Commit**

```bash
git add openbrain/brain.py tests/test_brain.py
git commit -m "feat(brain): fetch_paper topic via multi-source resolve+store flag; known-item threshold/corroboration"
```

---

### Task 6: Tools — `fetch_papers`, `store_papers`; remove `acquire`; delete `acquire.py` pipeline

**Files:**
- Modify: `openbrain/tools/library.py` (`fetch_paper` → `fetch_papers`, +`store`/`author`/`year`/`title_threshold`)
- Modify: `openbrain/tools/driver.py` (`store_paper` → `store_papers`)
- Modify: `openbrain/tools/acquire.py` (remove `acquire` tool; keep query_library/coverage/list_venues/list_concepts/export_bibtex)
- Modify: `openbrain/acquire.py` (delete `acquire()` + `prefilter()`; keep `dedup_records` if still referenced, else remove the module if empty)
- Modify: `openbrain/{server.py,resources.py}`, `integrations/**`, `tests/{test_driver,test_acquire}.py`

**Interfaces:**
- Produces tools: `fetch_papers(identifier=None, identifiers=None, topic=None, doc_type="paper", sources=None, store=False, author=None, year=None, title_threshold=None, force=False) -> str`; `store_papers(papers: list[dict]) -> str`.

- [ ] **Step 1: Enumerate references**

Run: `grep -rn "\bacquire\b\|store_paper\b\|fetch_paper\b\|store_one\b\|prefilter\|min_overlap" openbrain/ integrations/ tests/`
Record. Do NOT touch `store_acquired_paper`, `_store_paper`, `dedup_records` (unless removing), `venues`.

- [ ] **Step 2: Rename `fetch_paper` tool → `fetch_papers` + params**

In `openbrain/tools/library.py`: rename `async def fetch_paper(` → `async def fetch_papers(`; add `store: bool = False, author: str | None = None, year: int | None = None, title_threshold: float | None = None` to the signature. In the single-identifier/topic path, forward them:

```python
        author_list = (
            [a.strip() for a in author.split(",") if a.strip()] if author else None
        )
        result = await brain.fetch_paper(
            identifier=identifier, topic=topic, doc_type=doc_type,
            source_order=source_order, force=force, store=store,
            title_threshold=title_threshold, year=year, authors=author_list,
        )
```

In topic-mode formatting, show a stored summary when `result.get("stored")` (as in the prior plan revision). Update the docstring: known-item fetches+stores (title tolerates misspellings; author/year/title_threshold tune the match); topic returns candidates unless `store=true`.

- [ ] **Step 3: `store_paper` tool → `store_papers`**

In `openbrain/tools/driver.py`, replace the `store_paper` tool with one taking `papers: list[dict]` and calling `brain.store_papers(papers)`, returning the JSON roll-up (per the prior plan revision's block).

- [ ] **Step 4: Remove `acquire` tool + pipeline**

- `openbrain/tools/acquire.py`: delete the `acquire` `@mcp.tool`. Keep the rest.
- `openbrain/acquire.py`: delete `acquire()` and `prefilter()`. If `dedup_records` is referenced nowhere after this (grep), remove the module and its `server.py` import; otherwise keep only the still-used helpers. (Phase 3 store path does its own dedup via `store_acquired_paper`'s DOI check, so intra-batch `dedup_records` may be unused — verify with grep before deleting.)

- [ ] **Step 5: Prose + tests (hard cut)**

- `server.py`/`resources.py`/`integrations/**`: `fetch_paper`→`fetch_papers`, `store_paper`→`store_papers`; drop `acquire` (point bulk use at `fetch_papers(topic, store=true)`).
- `tests/test_driver.py`: `tools["store_paper"]`→`tools["store_papers"]` (list arg); patch `brain.store_papers`.
- `tests/test_acquire.py`: remove/repoint tests that exercised `acquire.acquire`/`prefilter`/the `acquire` tool. Pipeline coverage is replaced by `store_papers` (Task 4) and `resolve` (Task 3) tests; keep venues/query_library/coverage/export tests.

- [ ] **Step 6: Verify + suite**

Run: `grep -rn "tools\[.acquire.\]\|\bfetch_paper\b\|\bstore_paper\b\|store_one\|prefilter\|min_overlap" openbrain/ integrations/ tests/` → zero hits for removed names (`store_acquired_paper`/`_store_paper`/`dedup_records` ok).
Run: `uv run python -m pytest -q` → PASS.
Run: `uv run python -c "import openbrain.server"` → clean (catches a dangling acquire import).

- [ ] **Step 7: Commit**

```bash
git add openbrain/ integrations/ tests/
git commit -m "feat(tools): fetch_papers(+store) & store_papers replace fetch_paper/store_paper/acquire; remove acquire pipeline"
```

---

## Self-Review

**Spec coverage (umbrella + this session's decisions):**
- One fuzzy scorer for BOTH get_papers and fetch_papers → Task 1 + 2 (internal) + 3 (external). ✓
- "Deterministic" = rule-based fuzzy, not exact, not embeddings → Task 2. ✓
- Known-item short-circuit + enrich winner → Task 3. ✓
- Topic fans out all sources → Task 5 (routes to `sources.resolve(topic)`, which iterates all `_REGISTRY` sources + dedup). ✓
- `fetch_papers` known-item store-by-default; topic candidates unless `store=True` → Task 5 + 6. ✓
- `store_papers` list, enrich + canon + store, callable directly → Task 4 + 6. ✓
- `acquire` tool + pipeline removed; relevance gate/agenda gone; mechanical prep retained in store path → Task 4 (enrich/canon) + 6 (delete). ✓
- Hard cut → Task 6 grep gate. ✓
- Researcher migration = separate lockstep issue (below).

**Placeholder scan:** Novel logic carries full code; ref-removal steps grep-then-edit named files. No TBDs.

**Type consistency:** `_score_candidate(ref_title, cand, *, year, authors)` (Task 1) called by `resolve_reference` (Task 2) and `resolve` (Task 3). `store_papers(list)->{stored,deduped,results}` (Task 4) consumed by `fetch_paper` topic store (Task 5) and the tool (Task 6). `resolve(... min_score, year, authors)` (Task 3) called by `fetch_paper` (Task 5). `fetch_papers` tool params (Task 6) match `brain.fetch_paper` kwargs (Task 5).

## Dependent issue (separate repo, lockstep)

agentic-researcher uses the removed `acquire` tool (`src/researcher/collect.py:27`, `src/researcher/openbrain.py:55-65`). Migration (own queue item, depends on this phase): replace with `fetch_papers(topic=term, since/limit, store=False)` → driver's **own** relevance gate (score_relevance, or a reimplemented lexical overlap — this is where openbrain's removed `min_overlap` now lives) → `store_papers(keepers)`.

## Notes for later phases
- Phase 4 (schema/status removal + `chunks.source`), Phase 5 (driver split: `fetch_citations`/`score_relevance`) remain.
- `since`/venue search filters were OpenAlex-paginated in old `acquire`; multi-source `resolve(topic)` does not push them down. If venue-scoped harvest is wanted later, it is a driver concern or a follow-up.
- Carry-over Minors still open (no-arg `get_papers` guard; journal-abbrev clause now subsumed by fuzzy `_score_candidate`).
