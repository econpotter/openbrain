"""
tools/maintenance.py — memory consolidation tools.
"""

from openbrain import brain


def register(mcp):
    @mcp.tool
    async def run_memory_maintenance(
        dry_run: bool = True,
        approved_synthesis: str | None = None,
        thought_ids: list[str] | None = None,
    ) -> str:
        """
        Consolidate recent memory records into a durable synthesis.

        Use this only when the user asks to consolidate, clean up, or run
        memory maintenance. Default dry_run=True returns the proposed synthesis
        and source thought IDs without archiving anything. Set dry_run=False
        only after the user approves applying the exact dry-run synthesis.

        Args:
            dry_run: Preview proposed synthesis without writing or archiving.
            approved_synthesis: Exact synthesis text approved from a dry run.
            thought_ids: Exact source thought IDs approved from a dry run.
        """
        if dry_run:
            result = await brain.preview_maintenance_once()
            if result["processed"] == 0:
                return "No maintenance candidates found."
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

        result = await brain.apply_maintenance_synthesis(thought_ids, approved_synthesis)
        return (
            f"Processed {result['processed']} thought(s); "
            f"created {result['syntheses']} synthesis record(s)."
        )
