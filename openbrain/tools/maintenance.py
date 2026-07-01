"""
tools/maintenance.py — memory extraction and embedding maintenance tools.
"""

from openbrain import brain


def register(mcp):
    @mcp.tool
    async def run_memory_extraction(
        dry_run: bool = True,
        approved_synthesis: str | None = None,
        thought_ids: list[str] | None = None,
    ) -> str:
        """
        Extract recent memory records into a durable synthesis.

        Use this only when the user asks to consolidate, clean up, or run
        memory extraction. Default dry_run=True returns the proposed synthesis
        and source thought IDs without archiving anything. Set dry_run=False
        only after the user approves applying the exact dry-run synthesis.

        Args:
            dry_run: Preview proposed synthesis without writing or archiving.
            approved_synthesis: Exact synthesis text approved from a dry run.
            thought_ids: Exact source thought IDs approved from a dry run.
        """
        if dry_run:
            result = await brain.preview_extraction_once()
            if result["processed"] == 0:
                return "No extraction candidates found."
            lines = [
                f"DRY RUN: {result['processed']} thought(s) would be consolidated.",
                "",
                "Proposed synthesis:",
                result["synthesis"] or "(empty)",
                "",
                "Source thought IDs:",
            ]
            lines.extend(f"  {thought_id}" for thought_id in result["thought_ids"])
            return "\n".join(lines)

        if not approved_synthesis or not thought_ids:
            return (
                "Run dry_run=True first, get user approval, then call with "
                "dry_run=False, approved_synthesis=<approved text>, and "
                "thought_ids=<approved source IDs>."
            )

        result = await brain.apply_extraction_synthesis(thought_ids, approved_synthesis)
        return (
            f"Processed {result['processed']} thought(s); "
            f"created {result['syntheses']} synthesis record(s)."
        )

    @mcp.tool
    async def backfill_embeddings(limit: int = 500) -> str:
        """
        Embed chunks stored without an embedding (e.g. the embed model was down
        when a paper was fetched or a document ingested). Run this after the
        local embedding model is back up. Safe to re-run; it only touches chunks
        whose embedding is still missing.

        Args:
            limit: Maximum number of pending chunks to embed in this run.
        """
        result = await brain.backfill_chunk_embeddings(limit=limit)
        if result["pending_before"] == 0:
            return "No chunks pending embedding."
        return (
            f"Backfill: {result['embedded']} embedded, {result['failed']} failed, "
            f"{result['pending_after']} still pending "
            f"(of {result['pending_before']} before this run)."
        )

    @mcp.tool
    async def list_pending_embeddings(limit: int = 100) -> str:
        """
        List library records that still have chunks without an embedding (stored
        text, embedding deferred because the model was down at fetch/ingest time).

        Use this to see *which* papers are unsearchable, not just how many. Run
        backfill_embeddings once the embedding model is back up to fix them.

        Args:
            limit: Maximum number of records to list.
        """
        records = await brain.list_pending_embeddings(limit=limit)
        if not records:
            return "No records pending embedding."
        lines = [f"{len(records)} record(s) pending embedding:"]
        lines.extend(
            f"- {r['title']} ({r['pending']} chunk(s)) [{r['id']}]" for r in records
        )
        return "\n".join(lines)

    @mcp.tool
    async def list_pending_thoughts(limit: int = 100) -> str:
        """
        List thoughts still missing a derived asset, naming which step is pending:
        an embedding (embed model was down at capture) or extraction (enrichment
        not yet complete). Use this to see *which* thoughts are unsearchable or
        unenriched. Run backfill_thought_embeddings and/or
        backfill_thought_extractions to fix them.

        Args:
            limit: Maximum number of thoughts to list.
        """
        records = await brain.list_pending_thoughts(limit=limit)
        if not records:
            return "No thoughts pending embedding or extraction."
        lines = [f"{len(records)} thought(s) pending:"]
        for r in records:
            pending = []
            if r["needs_embedding"]:
                pending.append("embedding")
            if r["needs_extraction"]:
                pending.append("extraction")
            lines.append(f"- [{', '.join(pending)}] {r['content']} [{r['id']}]")
        return "\n".join(lines)

    @mcp.tool
    async def backfill_thought_embeddings(limit: int = 500) -> str:
        """
        Embed thoughts stored without an embedding (e.g. the embed model was down
        when the thought was captured). Run this after the local embedding model
        is back up. Safe to re-run; it only touches thoughts whose embedding is
        still missing.

        Args:
            limit: Maximum number of pending thoughts to embed in this run.
        """
        result = await brain.backfill_thought_embeddings(limit=limit)
        if result["pending_before"] == 0:
            return "No thoughts pending embedding."
        return (
            f"Backfill: {result['embedded']} embedded, {result['failed']} failed, "
            f"{result['pending_after']} still pending "
            f"(of {result['pending_before']} before this run)."
        )

    @mcp.tool
    async def backfill_thought_extractions(limit: int = 100) -> str:
        """
        Enrich thoughts left unenriched because the extraction model was down when
        they were captured. Extracts tags/people/projects/action_items/importance
        and creates entity links. Safe to re-run; it only touches thoughts that are
        not yet enriched.

        Args:
            limit: Maximum number of unenriched thoughts to process in this run.
        """
        result = await brain.backfill_thought_extractions(limit=limit)
        if result["pending_before"] == 0:
            return "No thoughts pending extraction."
        return (
            f"Backfill: {result['extracted']} enriched, {result['failed']} failed, "
            f"{result['pending_after']} still pending "
            f"(of {result['pending_before']} before this run)."
        )

    @mcp.tool
    async def backfill_links(limit: int = 1000) -> str:
        """
        Populate the `links` graph table for thoughts that mention people or
        projects but have no edges yet (e.g. thoughts captured before graph
        traversal was added). Safe to re-run.

        Args:
            limit: Maximum number of thoughts to process in this run.
        """
        result = await brain.backfill_links(limit=limit)
        if result["processed"] == 0:
            return "No thoughts pending link backfill."
        return (
            f"Backfill: {result['processed']} thought(s) processed, "
            f"{result['linked']} link(s) created."
        )
