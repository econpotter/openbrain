"""
tools/entities.py — people and project list/edit tools (Phase 3).
"""

import brain


def register(mcp):
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
    async def update_person(
        name: str,
        new_name: str | None = None,
        affiliation: str | None = None,
        relationship: str | None = None,
        notes: str | None = None,
    ) -> str:
        """
        Update fields on a person record. Looks up the person by their current name.

        Args:
            name:         Current name of the person to update.
            new_name:     Rename to this value.
            affiliation:  New affiliation.
            relationship: New relationship type (collaborator | author | contact | advisor).
            notes:        New notes.
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
        if not updates:
            return "No fields to update."
        updated = await brain.update_person(str(person["id"]), updates)
        if not updated:
            return "Update failed."
        return f"Updated: {updated['name']}"

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
