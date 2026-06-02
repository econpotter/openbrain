"""
tools/search.py — search_thoughts, browse_recent, get_stats tools.
"""

from openbrain import brain


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
    async def get_thought_connections(
        thought_id: str,
        limit: int = 20,
        include_restricted: bool = False,
    ) -> str:
        """
        Find memories connected to a specific thought through shared metadata.

        Results are ranked by overlap across topics, tags, people, and projects.
        Restricted thoughts are excluded unless explicitly requested.

        Args:
            thought_id:          UUID of the source thought.
            limit:               Maximum number of connected thoughts. Default 20.
            include_restricted:  Include restricted thoughts. Default false.
        """
        results = await brain.get_thought_connections(
            thought_id,
            limit,
            include_restricted,
        )
        if not results:
            return "No connected thoughts found."
        lines = []
        for r in results:
            shared = []
            for key in ("topics", "tags", "people", "projects"):
                values = r.get(f"shared_{key}") or []
                if values:
                    shared.append(f"{key}: {', '.join(values)}")
            lines.append(
                f"[{r['overlap_count']} overlap] {r['preview']}\n"
                f"  Shared: {'; '.join(shared)}"
            )
        return "\n\n".join(lines)

    @mcp.tool
    async def get_stats() -> str:
        """
        Return row counts and grouped activity statistics for the brain database.
        """
        stats = await brain.table_stats()
        lines = [
            f"Thoughts total:    {stats['thoughts_total']}",
            f"Thoughts last 24h: {stats['thoughts_last_24h']}",
        ]
        lines.append(f"Library documents: {stats['library_total']}")
        for label, key in (
            ("Types", "types"),
            ("Tags", "tags"),
            ("Topics", "topics"),
            ("Sources", "sources"),
            ("Sensitivity tiers", "sensitivity_tiers"),
        ):
            values = stats.get(key) or {}
            if values:
                summary = ", ".join(
                    f"{name}: {count}"
                    for name, count in sorted(
                        values.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                )
                lines.append(f"{label}: {summary}")
        return "\n".join(lines)
