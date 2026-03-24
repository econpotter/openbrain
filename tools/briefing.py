"""
tools/briefing.py — search_brain, get_daily_briefing tools (Phase 4).
"""

from datetime import date, timedelta

import brain


def register(mcp):
    @mcp.tool
    async def get_briefing(days: int = 1) -> str:
        """
        Return a briefing covering active projects, recent thoughts, people, and
        ingested documents for a given time window.

        Always call gcal_list_events for the same date range alongside this tool
        to give a complete picture of what's on the user's plate.

        Args:
            days: How many days back to cover. 1=today, 7=week, 30=month.
        """
        data = await brain.get_briefing(days)

        today = date.today()
        since = today - timedelta(days=days - 1)
        period = (
            "today" if days == 1
            else f"this week ({since.strftime('%b %d')} – {today.strftime('%b %d')})"
            if days <= 7
            else f"past {days} days ({since.strftime('%b %d')} – {today.strftime('%b %d')})"
        )

        sections = [f"BRIEFING — {period}"]

        # Projects
        if data["projects"]:
            lines = ["PROJECTS:"]
            for p in data["projects"]:
                status = "" if p["status"] == "active" else f" [{p['status']}]"
                desc = f" — {p['description']}" if p["description"] else ""
                lines.append(f"  • {p['name']}{status}{desc}")
            sections.append("\n".join(lines))
        else:
            sections.append("PROJECTS: none active")

        # Thoughts
        if data["thoughts"]:
            lines = [f"THOUGHTS ({len(data['thoughts'])}):"]
            for t in data["thoughts"]:
                ts = t["created_at"].strftime("%Y-%m-%d %H:%M")
                snippet = t["content"][:200].replace("\n", " ")
                lines.append(f"  [{ts}] {snippet}")
            sections.append("\n".join(lines))
        else:
            sections.append(f"THOUGHTS: none in the past {days} day(s)")

        # People
        if data["people"]:
            lines = ["PEOPLE mentioned:"]
            for name, count in data["people"]:
                lines.append(f"  {name} ({count} thought{'s' if count > 1 else ''})")
            sections.append("\n".join(lines))

        # Library
        if data["library"]:
            lines = [f"INGESTED ({len(data['library'])} document(s)):"]
            for doc in data["library"]:
                authors = doc["authors"] or []
                author_str = " & ".join(authors[:2]) + (" et al." if len(authors) > 2 else "")
                line = f"  • {doc['title']}"
                if author_str:
                    line += f" — {author_str}"
                if doc["year"]:
                    line += f" ({doc['year']})"
                lines.append(line)
            sections.append("\n".join(lines))

        # Follow-ups
        if data["follow_ups"]:
            lines = ["FOLLOW-UPS DUE:"]
            for p in data["follow_ups"]:
                due = p["follow_up_date"]
                overdue = due < date.today()
                tag = " ⚠ overdue" if overdue else ""
                lines.append(f"  • {p['name']} — {due}{tag}")
                if p["notes"]:
                    lines.append(f"    {p['notes']}")
            sections.append("\n".join(lines))

        # Footer hint for calendar
        sections.append(
            f"NOTE: Also check gcal_list_events for {since.isoformat()} to "
            f"{today.isoformat()} to include calendar items."
        )

        return "\n\n".join(sections)

    @mcp.tool
    async def search_brain(
        query: str,
        threshold: float = 0.35,
        count: int = 5,
    ) -> str:
        """
        Unified search across everything — thoughts, library, people, projects.

        Uses hybrid search: vector similarity catches semantic relationships,
        full-text search catches exact names and technical terms. Library results
        are deduplicated to one result per document. People and projects are
        surfaced by who and what appears in the matching thoughts.

        Use this for "what do I know about X?" style questions.

        Args:
            query:     Natural-language query.
            threshold: Minimum vector similarity to include (0–1). Default 0.35.
            count:     Max results per section. Default 5.
        """
        results = await brain.search_brain(query, threshold, count)

        sections = []

        if results["thoughts"]:
            lines = [f"THOUGHTS ({len(results['thoughts'])} found):"]
            for t in results["thoughts"]:
                ts = t["created_at"].strftime("%Y-%m-%d")
                score = f"{t['score']:.2f}"
                snippet = t["content"][:250].replace("\n", " ")
                lines.append(f"  [{score}] [{ts}] {snippet}")
            sections.append("\n".join(lines))

        if results["library"]:
            lines = [f"LIBRARY ({len(results['library'])} documents):"]
            for r in results["library"]:
                score = f"{r['score']:.2f}"
                authors = r["authors"] or []
                author_str = (
                    " & ".join(authors[:2]) + (" et al." if len(authors) > 2 else "")
                )
                header = f"  [{score}] {r['title']}"
                if author_str:
                    header += f" — {author_str}"
                if r["year"]:
                    header += f" ({r['year']})"
                if r["source"]:
                    header += f" [{r['source']}]"
                snippet = r["chunk_text"][:200].replace("\n", " ")
                lines.append(f"{header}\n    {snippet}")
            sections.append("\n".join(lines))

        if results["people"]:
            lines = ["PEOPLE (mentioned in related thoughts):"]
            for name, count_ in results["people"]:
                lines.append(f"  {name} ({count_} thought{'s' if count_ > 1 else ''})")
            sections.append("\n".join(lines))

        if results["projects"]:
            lines = ["PROJECTS (mentioned in related thoughts):"]
            for name, count_ in results["projects"]:
                lines.append(f"  {name} ({count_} thought{'s' if count_ > 1 else ''})")
            sections.append("\n".join(lines))

        if not sections:
            return f"Nothing found for '{query}'."

        return "\n\n".join(sections)
