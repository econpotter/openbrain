"""
resources.py — read-only MCP context resources.
"""


TOOL_GUIDE = """
openbrain tool guide

Default retrieval:
- Use search_brain for broad "what do I know about X?" questions.
- Use search_library for document text.
- Use get_library_record for document metadata, status, DOI, and filepath.
- Use get_person or get_project for known entity profiles.
- Use get_briefing for "today", "this week", and follow-up context.

Default capture:
- Use capture_thought for general durable memory.
- Use remember_preference for stable user preferences.
- Use capture_decision for decisions and rationale.
- Use capture_project_update for milestones, blockers, constraints, and next steps.

Maintenance:
- Use run_memory_maintenance(dry_run=True) before any write/archival action.
- Use dry_run=False only with the exact approved synthesis and source thought IDs.
""".strip()


SCHEMA_SUMMARY = """
openbrain schema summary

thoughts:
- Append-only memory log for notes, memories, decisions, preferences, sessions,
  ingest events, and maintenance syntheses.
- Key metadata arrays: tags, topics, people, projects, action_items.
- Structured fields include type, source_type, importance, quality_score,
  sensitivity_tier, and enriched.

library and chunks:
- library stores document catalog metadata, filepath, DOI, BibTeX, status, and type.
- chunks stores searchable embedded text from library documents.

people and projects:
- Mutable entity records with embeddings.
- Entity search improves when thoughts include people/projects metadata.

links:
- Reserved generic relationship table between core records.
""".strip()


CAPTURE_POLICY = """
openbrain capture policy

Capture:
- Durable decisions and their reasons.
- Stable user preferences and project conventions.
- Non-obvious fixes, constraints, and verified environment facts.
- Project milestones, blockers, and named follow-ups.
- Research context likely to matter in future sessions.

Skip:
- Raw transcripts.
- Routine progress updates.
- Obvious duplicates.
- Guesses or unresolved hypotheses.
- Temporary command output unless it establishes a durable fact.

Sensitivity:
- standard: normal project and technical context.
- personal: private but ordinary personal or relationship context.
- restricted: highly sensitive material that should be excluded unless
  explicitly requested by a tool that supports restricted recall.
""".strip()


def register(mcp):
    @mcp.resource(
        "openbrain://tool-guide",
        name="openbrain tool guide",
        description="How to choose openbrain tools for common user requests.",
        mime_type="text/plain",
    )
    def tool_guide() -> str:
        return TOOL_GUIDE

    @mcp.resource(
        "openbrain://schema-summary",
        name="openbrain schema summary",
        description="Readable summary of core openbrain tables and metadata.",
        mime_type="text/plain",
    )
    def schema_summary() -> str:
        return SCHEMA_SUMMARY

    @mcp.resource(
        "openbrain://capture-policy",
        name="openbrain capture policy",
        description="Guidance for what should and should not be captured.",
        mime_type="text/plain",
    )
    def capture_policy() -> str:
        return CAPTURE_POLICY
