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
            projects: Every project the thought applies to (e.g. ["SDC", "dissertation"]);
                      several or none. Leave unset when no project applies.
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
    async def capture_transcript(
        content: str,
        session_id: str,
        trigger: str,
        source: str = "claude-code",
    ) -> str:
        """
        Summarize a session-transcript delta and store the summary as memory.

        The thin client sends the transcript text appended since the last
        capture; this tool summarizes it server-side with the configured
        extraction model and stores the summary through the standard write path
        (bare content, type="conversation") so enrichment, embedding, and entity
        linking happen asynchronously. Fingerprint dedup applies — a repeated
        delta merges rather than duplicates.

        A trivial or no-op delta (nothing substantive to remember) stores
        nothing and says so.

        Args:
            content:    Transcript delta text (user/assistant turns).
            session_id: Originating session id, recorded on the stored thought.
            trigger:    What fired the capture: stop | auto | manual.
            source:     Originating tool. Default "claude-code".
        """
        summary = await brain.summarize_transcript(content)
        if summary is None:
            return "No substantive content to capture (skipped)."

        metadata = {
            "type": "conversation",
            "source": source,
            "session_id": session_id,
            "trigger": trigger,
        }
        thought_id = await brain.store_thought(summary, metadata)
        return f"Stored conversation summary {thought_id}"

    @mcp.tool
    async def capture_friction(
        user: str,
        agent: str = "",
        cwd: str = "",
        session_id: str = "",
        source: str = "claude-code",
    ) -> str:
        """
        Store a user turn as a friction thought when it shows frustration with the agent.

        The thin client's prompt hook sends every user turn here. Jev judges the
        turn against the agent text before it; below the configured threshold
        nothing is stored. A stored friction holds the agent's text, the user's
        words and the working directory, tagged `friction`, so later analysis can
        pick which frustrations become hooks or checks.

        Args:
            user:       The user's turn, verbatim.
            agent:      The agent's text just before the turn.
            cwd:        The session's working directory.
            session_id: Originating session id.
            source:     Originating tool. Default "claude-code".
        """
        probability = await brain.judge_friction(agent, user)
        if probability < float(brain.JUDGE_CONFIG["friction_threshold"]):
            return f"Not a friction (p={probability:.2f}); nothing stored."
        content = (
            f"Friction in {cwd or 'an unknown directory'}.\n\n"
            f"The agent wrote:\n{agent.strip()[-1500:]}\n\n"
            f"The user replied:\n{user.strip()}"
        )
        metadata = {
            "type": "thought",
            "source": source,
            "tags": ["friction"],
            "session_id": session_id,
            "friction_probability": round(probability, 3),
        }
        thought_id = await brain.store_thought(content, metadata)
        return f"Stored friction {thought_id} (p={probability:.2f})"

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
        content = (
            decision if rationale is None else f"{decision}\n\nRationale: {rationale}"
        )
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
