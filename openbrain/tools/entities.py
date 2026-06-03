"""
tools/entities.py — people and project list/edit tools.
"""

from openbrain import brain


def register(mcp):
    @mcp.tool
    async def refresh_embeddings(entity_type: str = "all") -> str:
        """
        Re-embed all people and/or projects using enriched context from related thoughts.

        Run this after adding many thoughts or entities to ensure semantic search
        reflects the latest context. Embeddings are also refreshed automatically
        in the background whenever a thought is captured.

        Args:
            entity_type: "people", "projects", or "all". Default: all.
        """
        counts = await brain.refresh_all_embeddings(entity_type)
        parts = [f"{v} {k}" for k, v in counts.items()]
        return f"Re-embedded: {', '.join(parts)}."

    @mcp.tool
    async def add_note(person: str, note: str) -> str:
        """
        Add a timestamped note about a person.

        The note is stored as a thought tagged to the person, so it appears in
        search_brain, search_by_entity, and get_briefing automatically.

        Args:
            person: Name of the person (partial match).
            note:   The note to record.
        """
        try:
            p = await brain.add_person_note(person, note)
            return f"Note added for {p['name']}."
        except ValueError as e:
            return str(e)

    @mcp.tool
    async def get_person(name: str) -> str:
        """
        Get a full profile for a person: their record, all notes and thoughts
        mentioning them, and co-occurring projects and people.

        Args:
            name: Person's name (partial match).
        """
        result = await brain.get_person_profile(name)
        if result is None:
            return f"No person found matching '{name}'."
        if "multiple" in result:
            return f"Multiple matches: {', '.join(result['multiple'])}. Please be more specific."

        p = result["person"]
        lines = [f"## {p['name']}"]
        if p["affiliation"]:
            lines.append(f"Affiliation: {p['affiliation']}")
        if p["relationship"]:
            lines.append(f"Relationship: {p['relationship']}")
        if p["follow_up_date"]:
            lines.append(f"Follow-up: {p['follow_up_date']}")
        if p["notes"]:
            lines.append(f"Notes: {p['notes']}")

        thoughts = result["thoughts"]
        if thoughts:
            lines.append(f"\n### Thoughts & notes ({len(thoughts)}):")
            for t in thoughts:
                ts = t["created_at"].strftime("%Y-%m-%d")
                snippet = t["content"][:200].replace("\n", " ")
                lines.append(f"  [{ts}] {snippet}")

        if result["cooccurring_projects"]:
            lines.append("\n### Co-occurring projects:")
            for proj, count in result["cooccurring_projects"]:
                lines.append(f"  {proj} ({count})")

        if result["cooccurring_people"]:
            lines.append("\n### Co-occurring people:")
            for person, count in result["cooccurring_people"]:
                lines.append(f"  {person} ({count})")

        return "\n".join(lines)

    @mcp.tool
    async def get_project(name: str) -> str:
        """
        Get a full profile for a project: its record, related thoughts,
        co-occurring people/projects, and matching library records.

        Use this when the user asks about a known project, its current context,
        or recent decisions tied to that project.

        Args:
            name: Project name (partial match).
        """
        result = await brain.get_project_profile(name)
        if result is None:
            return f"No project found matching '{name}'."
        if "multiple" in result:
            return f"Multiple matches: {', '.join(result['multiple'])}. Please be more specific."

        p = result["project"]
        lines = [f"## {p['name']} [{p['status']}]"]
        if p["description"]:
            lines.append(f"Description: {p['description']}")
        if p["start_date"]:
            lines.append(f"Start date: {p['start_date']}")

        thoughts = result["thoughts"]
        if thoughts:
            lines.append(f"\n### Thoughts & notes ({len(thoughts)}):")
            for t in thoughts:
                ts = t["created_at"].strftime("%Y-%m-%d")
                snippet = t["content"][:200].replace("\n", " ")
                lines.append(f"  [{ts}] {snippet}")

        if result["cooccurring_people"]:
            lines.append("\n### Co-occurring people:")
            for person, count in result["cooccurring_people"]:
                lines.append(f"  {person} ({count})")

        if result["cooccurring_projects"]:
            lines.append("\n### Co-occurring projects:")
            for project, count in result["cooccurring_projects"]:
                lines.append(f"  {project} ({count})")

        if result["library"]:
            lines.append("\n### Library records:")
            for doc in result["library"][:10]:
                year = f" ({doc['year']})" if doc["year"] else ""
                lines.append(f"  {doc['title']}{year} [{doc['status']}]")

        return "\n".join(lines)

    @mcp.tool
    async def search_by_entity(
        name: str,
        entity_type: str = "any",
        limit: int = 20,
    ) -> str:
        """
        Find thoughts mentioning a person or project, and surface co-occurring
        people and projects from those thoughts.

        Answers questions like:
          "What have I been thinking about w.r.t. Bert?"
          "What people have been involved in the ai-outliers project?"
          "What projects have I been talking about recently?" (use name="")

        Args:
            name:        Person or project name to search for (partial match).
            entity_type: "person", "project", or "any". Default: any.
            limit:       Max thoughts to retrieve. Default 20.
        """
        thoughts = await brain.thoughts_mentioning(name, entity_type, limit)
        if not thoughts:
            return f"No thoughts found mentioning '{name}'."

        cooc = brain._extract_cooccurrences(thoughts, exclude=name)

        lines = [f"Found {len(thoughts)} thought(s) mentioning '{name}':\n"]
        for t in thoughts:
            ts = t["created_at"].strftime("%Y-%m-%d")
            snippet = t["content"][:200].replace("\n", " ")
            lines.append(f"[{ts}] {snippet}")

        if cooc["people"]:
            lines.append("\nCo-occurring people:")
            for person, count in cooc["people"]:
                lines.append(f"  {person} ({count})")

        if cooc["projects"]:
            lines.append("\nCo-occurring projects:")
            for project, count in cooc["projects"]:
                lines.append(f"  {project} ({count})")

        return "\n".join(lines)

    @mcp.tool
    async def add_person(
        name: str,
        affiliation: str | None = None,
        relationship: str | None = None,
        notes: str | None = None,
    ) -> str:
        """
        Add a new person to the database.

        Args:
            name:         Full name.
            affiliation:  Institution or organization.
            relationship: collaborator | author | contact | advisor
            notes:        Any additional notes.
        """
        row = await brain.add_person(name, affiliation, relationship, notes)
        return f"Added: {row['name']}"

    @mcp.tool
    async def add_project(
        name: str,
        description: str | None = None,
        status: str = "active",
        start_date: str | None = None,
    ) -> str:
        """
        Add a new project to the database.

        Args:
            name:        Project name.
            description: Brief description.
            status:      active | paused | completed | archived. Default: active.
            start_date:  Start date as YYYY-MM-DD.
        """
        row = await brain.add_project(name, description, status, start_date)
        return f"Added: {row['name']} [{row['status']}]"

    @mcp.tool
    async def search_people(query: str, threshold: float = 0.3, count: int = 10) -> str:
        """
        Semantic search over people — find people related to a topic or description.

        Args:
            query:     Natural-language query, e.g. "behavioral economics".
            threshold: Minimum similarity score (0–1). Default 0.3.
            count:     Maximum results to return. Default 10.
        """
        results = await brain.search_people_vec(query, threshold, count)
        if not results:
            return "No people found matching that query."
        lines = []
        for r in results:
            score = f"{r['similarity']:.2f}"
            parts = [r["name"]]
            if r["affiliation"]:
                parts.append(r["affiliation"])
            if r["relationship"]:
                parts.append(f"({r['relationship']})")
            line = f"[{score}] " + " | ".join(parts)
            if r["notes"]:
                line += f" — {r['notes']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool
    async def search_projects(query: str, threshold: float = 0.3, count: int = 10) -> str:
        """
        Semantic search over projects — find projects related to a topic or description.

        Args:
            query:     Natural-language query, e.g. "machine learning fairness".
            threshold: Minimum similarity score (0–1). Default 0.3.
            count:     Maximum results to return. Default 10.
        """
        results = await brain.search_projects_vec(query, threshold, count)
        if not results:
            return "No projects found matching that query."
        lines = []
        for r in results:
            score = f"{r['similarity']:.2f}"
            line = f"[{score}] {r['name']} [{r['status']}]"
            if r["description"]:
                line += f" — {r['description']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool
    async def list_people(search: str | None = None) -> str:
        """
        List people in the database.

        Args:
            search: Optional name filter (partial match, case-insensitive).
        """
        rows = await brain.list_people(search)
        if not rows:
            return "No people found."
        lines = []
        for r in rows:
            parts = [r["name"]]
            if r["affiliation"]:
                parts.append(r["affiliation"])
            if r["relationship"]:
                parts.append(f"({r['relationship']})")
            line = " | ".join(parts)
            if r["notes"]:
                line += f" — {r['notes']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool
    async def list_follow_ups(within_days: int = 7) -> str:
        """
        List people with follow-ups due soon.

        Use this when the user asks who needs attention, what follow-ups are
        due, or what contacts are overdue.

        Args:
            within_days: Include follow-ups due within this many days. Default 7.
        """
        rows = await brain.people_with_follow_ups(within_days)
        if not rows:
            return f"No follow-ups due within {within_days} days."
        lines = []
        for r in rows:
            line = f"{r['follow_up_date']} — {r['name']}"
            if r["affiliation"]:
                line += f" | {r['affiliation']}"
            if r["notes"]:
                line += f" — {r['notes']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool
    async def update_person(
        name: str,
        new_name: str | None = None,
        affiliation: str | None = None,
        relationship: str | None = None,
        notes: str | None = None,
        follow_up_date: str | None = None,
    ) -> str:
        """
        Update fields on a person record. Looks up the person by their current name.

        Use this to correct or maintain the person record itself. For ordinary
        dated notes about a person, use add_note instead because update_person
        overwrites the notes field.

        Args:
            name:           Current name of the person to update.
            new_name:       Rename to this value.
            affiliation:    New affiliation.
            relationship:   New relationship type (collaborator | author | contact | advisor).
            notes:          New notes (overwrites existing — use add_note to append).
            follow_up_date: Schedule a follow-up as YYYY-MM-DD. Set to "" to clear.
        """
        matches = await brain.find_person_by_name(name)
        if not matches:
            return f"No person found matching '{name}'."
        if len(matches) > 1:
            names = ", ".join(r["name"] for r in matches)
            return f"Multiple matches: {names}. Please be more specific."
        person = matches[0]
        updates = {}
        if new_name is not None:
            updates["name"] = new_name
        if affiliation is not None:
            updates["affiliation"] = affiliation
        if relationship is not None:
            updates["relationship"] = relationship
        if notes is not None:
            updates["notes"] = notes
        if follow_up_date is not None:
            updates["follow_up_date"] = follow_up_date or None
        if not updates:
            return "No fields to update."
        updated = await brain.update_person(str(person["id"]), updates)
        if not updated:
            return "Update failed."
        fu = f" — follow-up: {updated['follow_up_date']}" if updated.get("follow_up_date") else ""
        return f"Updated: {updated['name']}{fu}"

    @mcp.tool
    async def list_projects(status: str | None = None) -> str:
        """
        List research projects.

        Args:
            status: Filter by status — active | paused | completed | archived.
                    Omit to list all.
        """
        rows = await brain.list_projects(status)
        if not rows:
            return "No projects found."
        lines = []
        for r in rows:
            line = f"{r['name']} [{r['status']}]"
            if r["description"]:
                line += f" — {r['description']}"
            lines.append(line)
        return "\n".join(lines)

    @mcp.tool
    async def update_project(
        name: str,
        new_name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        start_date: str | None = None,
    ) -> str:
        """
        Update fields on a project record. Looks up the project by its current name.

        Use this to rename a project, change its status, or update the stable
        project description. For a dated project event, use capture_project_update.

        Args:
            name:         Current name of the project to update.
            new_name:     Rename to this value.
            description:  New description.
            status:       New status — active | paused | completed | archived.
            start_date:   New start date as YYYY-MM-DD.
        """
        matches = await brain.find_project_by_name(name)
        if not matches:
            return f"No project found matching '{name}'."
        if len(matches) > 1:
            names = ", ".join(r["name"] for r in matches)
            return f"Multiple matches: {names}. Please be more specific."
        project = matches[0]
        updates = {}
        if new_name is not None:
            updates["name"] = new_name
        if description is not None:
            updates["description"] = description
        if status is not None:
            updates["status"] = status
        if start_date is not None:
            updates["start_date"] = start_date
        if not updates:
            return "No fields to update."
        updated = await brain.update_project(str(project["id"]), updates)
        if not updated:
            return "Update failed."
        return f"Updated: {updated['name']} [{updated['status']}]"
