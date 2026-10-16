"""
tools/search.py — search_thoughts, browse_recent, get_stats tools.
"""

import json

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

        Use this for narrow searches over captured memory only. For broad
        questions across memory, documents, people, and projects, use
        search_brain instead.

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
            content = r["content"].replace("\n", " ")
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            tags = ", ".join(meta.get("tags", []))
            tag_str = f"  [{tags}]" if tags else ""
            lines.append(f"[{score}]{tag_str} {content}")
        return "\n\n".join(lines)

    @mcp.tool
    async def browse_recent(
        hours: int = 24,
        limit: int = 20,
    ) -> str:
        """
        Retrieve thoughts from the past N hours, newest first.

        Prefer browse_recent_thoughts when choosing by tool name. This alias is
        kept for compatibility.

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
            content = r["content"].replace("\n", " ")
            lines.append(f"[{ts}] {content}")
        return "\n\n".join(lines)

    @mcp.tool
    async def browse_recent_thoughts(
        hours: int = 24,
        limit: int = 20,
    ) -> str:
        """
        Retrieve recent thoughts and memory records, newest first.

        Use this when the user asks what was recently captured, what happened
        in openbrain recently, or asks to inspect recent memory writes.

        Args:
            hours: How many hours back to look. Default 24.
            limit: Maximum number of thoughts to return. Default 20.
        """
        return await browse_recent(hours, limit)

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
    async def get_related_entities(name: str, depth: int = 2, limit: int = 20) -> str:
        """
        Find people, projects, and thoughts connected to a named person or
        project via the links graph, up to N hops away.

        Use this for "who/what is connected to X" questions that go beyond
        direct co-mentions, e.g. a person two hops away through a shared
        project.

        Args:
            name:  Name of the person or project to start from.
            depth: Maximum number of hops to traverse. Default 2.
            limit: Maximum number of related entities to return. Default 20.
        """
        entity = await brain.resolve_entity(name)
        if entity is None:
            return f"No person or project found matching '{name}'."
        if "multiple" in entity:
            return f"Multiple matches for '{name}': {', '.join(entity['multiple'])}"

        results = await brain.traverse_graph(
            entity["table"], str(entity["id"]), depth, limit
        )
        if not results:
            return f"No related entities found for {entity['name']} (depth <= {depth})."

        lines = [f"## Related to {entity['name']} (depth <= {depth})", ""]
        by_depth: dict[int, list[dict]] = {}
        for r in results:
            by_depth.setdefault(r["depth"], []).append(r)
        for d in sorted(by_depth):
            lines.append(f"Depth {d}:")
            for r in by_depth[d]:
                lines.append(f"  [{r['table']}] {r['label']} — {r['relationship']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @mcp.tool
    async def get_stats() -> str:
        """
        Return row counts and grouped activity statistics for the brain database.

        Use this for health checks and high-level database inventory, not for
        answering substantive questions about a topic.
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
