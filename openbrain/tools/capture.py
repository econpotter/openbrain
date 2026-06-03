"""
tools/capture.py — capture_thought tool.
"""

from openbrain import brain

PROJECT_STATUSES = {"active", "paused", "completed", "archived"}


def register(mcp):
    @mcp.tool
    async def capture_thought(
        content: str,
        type: str = "thought",
        tags: list[str] | None = None,
        people: list[str] | None = None,
        projects: list[str] | None = None,
        importance: int | None = None,
        sensitivity_tier: str | None = None,
        quality_score: float | None = None,
        action_items: list[str] | None = None,
        source: str = "claude",
    ) -> str:
        """
        Store a thought, memory, note, or observation.

        Use this for durable facts, decisions, user preferences, project
        context, and session summaries that should be available later.
        Do not use it for raw transcripts, routine progress, or guesses.

        Extracts and stores metadata alongside the content. All arguments
        except `content` are optional — fill in whatever the conversation
        makes clear.

        Args:
            content:  The full text of the thought or memory to preserve.
            type:     One of: thought | note | memory | conversation | ingest_event.
                      Default is "thought". Use "note" when the user says "capture notes
                      about ...", "write up notes on ...", or otherwise signals long-form
                      or structured notes rather than a quick observation.
            tags:     Freeform topic keywords (e.g. ["bandwidth", "kernel methods"]).
            people:   Names of people mentioned (e.g. ["Imbens", "Kalyanaraman"]).
            projects: Project names mentioned (e.g. ["SDC", "dissertation"]).
            importance: 0–6 importance score. Higher means more durable.
            sensitivity_tier: standard | personal | restricted.
            quality_score: 0–100 confidence/usefulness score.
            action_items: Concrete follow-up actions extracted from the thought.
            source:   Originating system — "claude", "gemini", or "ingest_script".
        """
        metadata = {"type": type, "source": source}
        if tags:
            metadata["tags"] = tags
        if people:
            metadata["people"] = people
        if projects:
            metadata["projects"] = projects
        if importance is not None:
            metadata["importance"] = importance
        if sensitivity_tier is not None:
            metadata["sensitivity_tier"] = sensitivity_tier
        if quality_score is not None:
            metadata["quality_score"] = quality_score
        if action_items:
            metadata["action_items"] = action_items

        thought_id = await brain.store_thought(content, metadata)
        return f"Stored thought {thought_id}"

    @mcp.tool
    async def remember_preference(
        content: str,
        scope: str | None = None,
        tags: list[str] | None = None,
        projects: list[str] | None = None,
    ) -> str:
        """
        Store a durable user preference.

        Use this when the user states a stable preference about workflow,
        writing style, tooling, or project conventions.

        Args:
            content: The preference to remember.
            scope:   Optional scope such as "global", "openbrain", or a project name.
            tags:    Freeform topic keywords.
            projects: Related project names.
        """
        metadata = {
            "type": "memory",
            "source": "claude",
            "tags": ["preference"] + (tags or []),
            "importance": 5,
            "quality_score": 80,
        }
        if scope:
            metadata["scope"] = scope
        if projects:
            metadata["projects"] = projects
        thought_id = await brain.store_thought(content, metadata)
        return f"Stored preference {thought_id}"

    @mcp.tool
    async def capture_decision(
        decision: str,
        rationale: str | None = None,
        tags: list[str] | None = None,
        people: list[str] | None = None,
        projects: list[str] | None = None,
    ) -> str:
        """
        Store a decision and optional rationale.

        Use this when a design, research, analysis, or workflow choice is made
        and future assistants should preserve the reason.

        Args:
            decision:  The decision that was made.
            rationale: Why the decision was made.
            tags:      Freeform topic keywords.
            people:    People involved or mentioned.
            projects:  Related project names.
        """
        content = decision if rationale is None else f"{decision}\n\nRationale: {rationale}"
        metadata = {
            "type": "memory",
            "source": "claude",
            "tags": ["decision"] + (tags or []),
            "importance": 5,
            "quality_score": 85,
        }
        if people:
            metadata["people"] = people
        if projects:
            metadata["projects"] = projects
        thought_id = await brain.store_thought(content, metadata)
        return f"Stored decision {thought_id}"

    @mcp.tool
    async def capture_project_update(
        project: str,
        update: str,
        status: str | None = None,
        tags: list[str] | None = None,
        people: list[str] | None = None,
        action_items: list[str] | None = None,
    ) -> str:
        """
        Store a durable update about one project.

        Use this for project milestones, blockers, constraints, or next steps.

        Args:
            project: Project name.
            update:  The update to preserve.
            status:  Optional project status: active | paused | completed | archived.
            tags:    Freeform topic keywords.
            people:  People involved or mentioned.
            action_items: Concrete follow-up actions.
        """
        if status and status not in PROJECT_STATUSES:
            return (
                "Invalid status. Use one of: "
                + ", ".join(sorted(PROJECT_STATUSES))
                + "."
            )

        metadata = {
            "type": "memory",
            "source": "claude",
            "tags": ["project-update"] + (tags or []),
            "projects": [project],
            "importance": 4,
            "quality_score": 80,
        }
        if people:
            metadata["people"] = people
        if action_items:
            metadata["action_items"] = action_items
        if status:
            metadata["project_status"] = status
        status_text = f" [{status}]" if status else ""
        content = f"Project update for {project}{status_text}: {update}"
        thought_id = await brain.store_thought(content, metadata)
        if status:
            matches = await brain.find_project_by_name(project)
            if len(matches) == 1:
                await brain.update_project(str(matches[0]["id"]), {"status": status})
        return f"Stored project update {thought_id}"
