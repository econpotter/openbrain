"""
tools/acquire.py — MCP tools for deterministic library browsing and export:
structured query + affordances (list_papers, coverage, list_venues,
list_concepts, remove_paper) and bibtex export. All return JSON-friendly text
so an external agent drives the loop without parsing prose.

Bulk paper acquisition lives in fetch_papers/store_papers (tools/library.py,
tools/driver.py); the relevance gate and research agenda are the driver's, not
openbrain's.
"""

import asyncio
import json

from openbrain import brain, sources


def register(mcp):
    @mcp.tool
    async def list_papers(
        venue: str | None = None,
        kind: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        concept: str | None = None,
        has_abstract: bool | None = None,
        sort: str = "year",
        limit: int = 50,
    ) -> str:
        """Deterministic structured query over the library: exact filters on
        venue (canonical abbrev), kind (journal/preprint/…), year range, and
        concept. This is "search by journal/year" done right —
        distinct from the semantic find_papers tool."""
        rows = await brain.query_library_struct(
            venue=venue,
            kind=kind,
            year_min=year_min,
            year_max=year_max,
            concept=concept,
            has_abstract=has_abstract,
            sort=sort,
            limit=limit,
        )
        return json.dumps(rows, indent=2, default=str)

    @mcp.tool
    async def coverage(venue: str | None = None, concept: str | None = None) -> str:
        """Counts of library papers by year (optionally filtered by venue or
        concept) — so an agent crafts gap-filling queries instead of re-fetching."""
        rows = await brain.coverage_stats(venue=venue, concept=concept)
        return json.dumps(rows, indent=2, default=str)

    @mcp.tool
    async def list_venues(
        field: str | None = None,
        kind: str | None = None,
        unknown: bool = False,
    ) -> str:
        """List the venue registry so an agent targets `venue=` precisely.
        unknown=True instead lists discovered-but-uncategorized venues (seen
        during acquisition, not yet in the registry) for promotion."""
        if unknown:
            rows = await brain.list_unknown_venues()
        else:
            rows = await brain.list_registry_venues(field=field, kind=kind)
        return json.dumps(rows, indent=2, default=str)

    @mcp.tool
    async def list_concepts(term: str) -> str:
        """Look up OpenAlex concepts matching `term` (with work counts) so a query
        is expanded with real index vocabulary, not guessed synonyms."""
        loop = asyncio.get_event_loop()
        concepts = await loop.run_in_executor(
            None, lambda: sources.openalex_concepts(term)
        )
        return json.dumps(concepts, indent=2, default=str)

    @mcp.tool
    async def remove_paper(
        id: str | None = None,
        doi: str | None = None,
        title: str | None = None,
        journal: str | None = None,
        authors: str | None = None,
        year: int | None = None,
    ) -> str:
        """Delete one library record — by `id` (the uuid get_papers/find_papers/
        store_papers return) or by bibliographic reference (doi/title/journal/
        authors/year, matched deterministically like get_papers, tolerant of
        slight title misspellings). Refuses an ambiguous reference (>1 match) so
        the wrong paper is never deleted. The post-hoc prune for stored
        false-positives — judgment stays with the agent, execution here.

        Args:
            id:      Library uuid (exact). Preferred when you have it.
            doi/title/journal/authors/year: Bibliographic reference to resolve.
        """
        author_list = (
            [a.strip() for a in authors.split(",") if a.strip()] if authors else None
        )
        result = await brain.delete_paper(
            paper_id=id,
            doi=doi,
            title=title,
            journal=journal,
            authors=author_list,
            year=year,
        )
        return json.dumps(result, indent=2, default=str)

    @mcp.tool
    async def export_bibtex(
        venue: str | None = None,
        year_min: int | None = None,
        path: str | None = None,
    ) -> str:
        """Generate a .bib from the matching library rows with stable citekeys.
        Writes to `path` if given (returns a confirmation), else returns the
        bibtex string."""
        bibtex = await brain.export_bibtex_records(
            venue=venue, year_min=year_min
        )
        if path:
            with open(path, "w") as fh:
                fh.write(bibtex)
            return f"Wrote {bibtex.count('@')} record(s) to {path}"
        return bibtex
