"""
tools/driver.py — MCP tools for the external research driver (agentic-researcher).

Thin wrappers over the deterministic primitives in `sources` and `brain`:
citation-neighbor fetch, anchor-relative relevance scoring, and single-paper
store. They take policy as parameters and own no agenda — the citation walk,
relevance gate, frontier/budget logic all live in the driver. Embeddings never
leave openbrain; `score_relevance` returns cosines, not vectors.

Each tool returns a JSON string whose keys match the agentic-researcher Openbrain
client contract exactly: get_citations -> {citations}, score_relevance ->
{scores}, store_paper -> {id, title, deduped}. The id-returning library search
lives in tools/library.py as `find_papers` -> {ids}.
"""

import asyncio
import json

from openbrain import brain, sources


def register(mcp):
    @mcp.tool
    async def get_citations(paper_id: str) -> str:
        """Citation neighbors of an OpenAlex work, for the driver's citation walk.

        `paper_id` is an OpenAlex work id (W… or full URL). Returns the work's
        referenced works (outbound) and the first page of its citing works
        (inbound), deduplicated as paper metadata records. No walk or gating
        happens here — the driver decides which neighbors to pursue."""
        loop = asyncio.get_event_loop()
        neighbors = await loop.run_in_executor(
            None, lambda: sources.openalex_citation_neighbors(paper_id)
        )
        return json.dumps({"citations": neighbors}, indent=2, default=str)

    @mcp.tool
    async def score_relevance(core_paper_ids: list[str], texts: list[str]) -> str:
        """Cosine of each candidate text against the centroid of the core papers.

        The centroid is the mean of the stored embeddings for `core_paper_ids`
        (anchors lacking an embedding are skipped); each text in `texts` is
        embedded and scored in input order. Raises an error when no anchors are
        given or none has a usable embedding — the driver's walk has no centroid
        to score against. Returns {scores: [float, …]}, one per text."""
        scores = await brain.relevance(core_paper_ids, texts)
        return json.dumps({"scores": scores}, indent=2, default=str)

    @mcp.tool
    async def store_paper(paper: dict) -> str:
        """Store a single paper's metadata into the library (with embedding).

        Thin wrapper over the acquire store path so the driver can land a paper
        it found via the citation walk. Returns {id, title, deduped}; `deduped`
        is true when the paper already existed and no new row was created."""
        result = await brain.store_one(paper)
        return json.dumps(result, indent=2, default=str)
