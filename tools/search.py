"""
tools/search.py — search_thoughts, browse_recent, get_stats tools (Phase 1).
"""

import brain


def register(mcp):
    @mcp.tool
    async def search_thoughts(
        query: str,
        threshold: float = 0.5,
        count: int = 10,
    ) -> str:
        """
        Semantic search over stored thoughts and memories.

        Returns the most similar stored thoughts, ranked by cosine similarity.

        Args:
            query:     Natural-language search query.
            threshold: Minimum similarity score to include (0–1). Default 0.5.
            count:     Maximum number of results to return. Default 10.
        """
        results = await brain.search_thoughts_vec(query, threshold, count)
        if not results:
            return "No results found."
        lines = []
        for r in results:
            score = f"{r['similarity']:.2f}"
            snippet = r["content"][:300].replace("\n", " ")
            meta = r.get("metadata") or {}
            tags = ", ".join(meta.get("tags", []))
            tag_str = f"  [{tags}]" if tags else ""
            lines.append(f"[{score}]{tag_str} {snippet}")
        return "\n\n".join(lines)

    @mcp.tool
    async def browse_recent(
        hours: int = 24,
        limit: int = 20,
    ) -> str:
        """
        Retrieve thoughts from the past N hours, newest first.

        Args:
            hours: How many hours back to look. Default 24.
            limit: Maximum number of thoughts to return. Default 20.
        """
        results = await brain.recent_thoughts(hours, limit)
        if not results:
            return f"No thoughts recorded in the past {hours} hours."
        lines = []
        for r in results:
            ts = r["created_at"].strftime("%Y-%m-%d %H:%M")
            snippet = r["content"][:300].replace("\n", " ")
            lines.append(f"[{ts}] {snippet}")
        return "\n\n".join(lines)

    @mcp.tool
    async def get_stats() -> str:
        """
        Return row counts and recent activity summary for the brain database.
        """
        stats = await brain.table_stats()
        lines = [
            f"Thoughts total:    {stats['thoughts_total']}",
            f"Thoughts last 24h: {stats['thoughts_last_24h']}",
        ]
        if stats["library_total"] is not None:
            lines.append(f"Library documents: {stats['library_total']}")
        return "\n".join(lines)
