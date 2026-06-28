"""Findings MCP tools — thin 1:1 adapters over brain.search_findings / findings.backfill_findings."""

import json

from openbrain import brain, findings


def register(mcp):
    @mcp.tool
    async def search_findings(
        text: str | None = None,
        cause: str | None = None,
        effect: str | None = None,
        stat: str | None = None,
        venue: str | None = None,
        year_min: int | None = None,
        status: str | None = None,
        group_by: str | None = None,
        limit: int = 50,
    ) -> str:
        """Search extracted findings; each row returned with its source paper citation.

        Findings are never returned without provenance — every result carries the
        source paper's citekey/title/year/doi, so a fact can be verified against its
        paper. text matches either side of the causal edge; cause/effect/stat/venue/
        year_min/status are filters; group_by='paper' collapses findings under one source.
        """
        out = await brain.search_findings(
            text=text,
            cause=cause,
            effect=effect,
            stat=stat,
            venue=venue,
            year_min=year_min,
            status=status,
            group_by=group_by,
            limit=limit,
        )
        return json.dumps(out, indent=2, default=str)

    @mcp.tool
    async def backfill_findings(limit: int = 100) -> str:
        """Extract findings for papers still pending (e.g. extraction model was down at ingest)."""
        res = await findings.backfill_findings(limit=limit)
        if res["pending_before"] == 0:
            return "No papers pending findings extraction."
        return (
            f"Findings backfill: {res['extracted_papers']} extracted, "
            f"{res['failed']} failed, {res['pending_after']} still pending."
        )
