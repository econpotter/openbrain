"""
tools/capture.py — capture_thought tool (Phase 1).
"""

import brain


def register(mcp):
    @mcp.tool
    async def capture_thought(
        content: str,
        type: str = "thought",
        tags: list[str] | None = None,
        people: list[str] | None = None,
        projects: list[str] | None = None,
        source: str = "claude",
    ) -> str:
        """
        Store a thought, memory, note, or observation.

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
            source:   Originating system — "claude", "gemini", or "ingest_script".
        """
        metadata = {"type": type, "source": source}
        if tags:
            metadata["tags"] = tags
        if people:
            metadata["people"] = people
        if projects:
            metadata["projects"] = projects

        thought_id = await brain.store_thought(content, metadata)
        return f"Stored thought {thought_id}"
