"""
tools/acquire.py — MCP tools for the deterministic acquire subsystem:
acquisition (acquire), structured query + affordances (query_library, coverage,
list_venues, list_concepts, remove_record), saved-query management
(save_query, list_queries, run_query, update_query, delete_query), and bibtex
export. All return JSON-friendly text so an external agent drives the loop
without parsing prose.
"""

import asyncio
import json

from openbrain import acquire as acquire_mod
from openbrain import brain, sources


def register(mcp):
    @mcp.tool
    async def acquire(
        query: str,
        venues: list[str] | None = None,
        since: int | None = None,
        limit: int = 50,
        min_overlap: int = 2,
        dry_run: bool = False,
    ) -> str:
        """Search journal sources for `query`, deterministically prefilter, and
        store survivors into the library with embeddings (no LLM). `venues` is an
        optional list of canonical abbrevs (e.g. ['AJAE','WRR']) pushed API-side;
        `since` bounds publication year; `min_overlap` is the lexical recall gate.
        dry_run=True previews counts + a sample without storing anything."""
        report = await acquire_mod.acquire(
            query,
            venues_filter=set(venues) if venues else None,
            since=since,
            limit=limit,
            min_overlap=min_overlap,
            dry_run=dry_run,
        )
        return json.dumps(report, indent=2, default=str)

    @mcp.tool
    async def query_library(
        venue: str | None = None,
        kind: str | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        status: str | None = None,
        concept: str | None = None,
        has_abstract: bool | None = None,
        sort: str = "year",
        limit: int = 50,
    ) -> str:
        """Deterministic structured query over the library: exact filters on
        venue (canonical abbrev), kind (journal/preprint/…), year range, reading
        status, and concept. This is "search by journal/year/status" done right —
        distinct from the semantic search_library tool."""
        rows = await brain.query_library_struct(
            venue=venue,
            kind=kind,
            year_min=year_min,
            year_max=year_max,
            status=status,
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
    async def remove_record(query: str) -> str:
        """Delete a single library record resolved by `query` (title/author/doi).
        Refuses when the query is ambiguous. The post-hoc prune for prefilter
        false-positives — judgment stays with the agent, execution here."""
        result = await brain.delete_library_record(query)
        return json.dumps(result, indent=2, default=str)

    @mcp.tool
    async def export_bibtex(
        venue: str | None = None,
        year_min: int | None = None,
        status: str | None = None,
        path: str | None = None,
    ) -> str:
        """Generate a .bib from the matching library rows with stable citekeys.
        Writes to `path` if given (returns a confirmation), else returns the
        bibtex string."""
        bibtex = await brain.export_bibtex_records(
            venue=venue, year_min=year_min, status=status
        )
        if path:
            with open(path, "w") as fh:
                fh.write(bibtex)
            return f"Wrote {bibtex.count('@')} record(s) to {path}"
        return bibtex

    # --- saved queries -----------------------------------------------------

    @mcp.tool
    async def save_query(
        name: str,
        query: str,
        venues: list[str] | None = None,
        since: int | None = None,
        min_overlap: int = 2,
        cadence: str = "weekly",
    ) -> str:
        """Save a recurring acquisition query (upsert on name). Cron runs due
        queries by cadence (nightly/weekly/monthly), harvesting only papers
        indexed since the last run."""
        row = await brain.save_query(
            name,
            query,
            venues=venues,
            since=since,
            min_overlap=min_overlap,
            cadence=cadence,
        )
        return json.dumps(row, indent=2, default=str)

    @mcp.tool
    async def list_queries() -> str:
        """List saved acquisition queries with their watermark + last yield."""
        rows = await brain.list_queries()
        return json.dumps(rows, indent=2, default=str)

    @mcp.tool
    async def run_query(name: str) -> str:
        """Run one saved query now (incremental harvest), advancing its watermark."""
        report = await brain.run_saved_query(name)
        return json.dumps(report, indent=2, default=str)

    @mcp.tool
    async def update_query(
        name: str,
        query: str | None = None,
        venues: list[str] | None = None,
        since: int | None = None,
        min_overlap: int | None = None,
        cadence: str | None = None,
    ) -> str:
        """Update fields on a saved query (only the provided fields change)."""
        row = await brain.update_query(
            name,
            query=query,
            venues=venues,
            since=since,
            min_overlap=min_overlap,
            cadence=cadence,
        )
        if row is None:
            return json.dumps({"updated": 0, "name": name}, default=str)
        return json.dumps(row, indent=2, default=str)

    @mcp.tool
    async def delete_query(name: str) -> str:
        """Delete a saved acquisition query."""
        result = await brain.delete_query(name)
        return json.dumps(result, indent=2, default=str)
