"""
server.py — FastMCP server exposing Open Brain tools.

Transport is controlled by config.yaml:
  stdio  — for local development and testing
  http   — streamable HTTP for remote access via Claude.ai / Gemini
"""

import os
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from openbrain import brain
from openbrain.tools import capture, search, library, entities, briefing


def _build_auth() -> StaticTokenVerifier | None:
    access_key = os.getenv("MCP_ACCESS_KEY", "").strip()
    if not access_key:
        return None

    return StaticTokenVerifier(
        tokens={
            access_key: {
                "client_id": "openbrain",
                "scopes": [],
            }
        }
    )


def _build_server() -> FastMCP:
    auth = _build_auth()
    return FastMCP(
        "Open Brain",
        instructions="""
Open Brain is a personal AI memory and knowledge base. Use these patterns when interacting with it:

- "What's on my plate today/this week?" — call get_briefing(days=1|7) AND gcal_list_events for the same date range, then synthesize both into a unified response.
- "What do I know about X?" — call search_brain(query).
- "Remember that..." — call capture_thought AND save to local memory files.
- Mentioning a project or person in a thought — if that project/person doesn't exist in the DB, call add_project or add_person first, then capture_thought.
- "Ingest this file" from a remote client machine — SCP the file to the MCP server first, appending a random alphanumeric suffix to avoid collisions (e.g. scp reason:/path/to/paper.pdf rhyme:/tmp/paper-<random>.pdf), then call ingest_document(filepath="/tmp/paper-<random>.pdf"). The pipeline copies the file to LIBRARY_PATH with a standardized name and cleans up the tmp file automatically.
- If ingest_document reports missing metadata, do NOT re-ingest. Instead ask the user for a BibTeX entry or title, then call update_library(query=<title>, bibtex=...) or update_library(query=<title>, title=...) to fix the record in place.
""".strip(),
        auth=auth,
    )

mcp = _build_server()
capture.register(mcp)
search.register(mcp)
library.register(mcp)
entities.register(mcp)
briefing.register(mcp)


def main():
    server_config = brain._cfg.get("server", {})
    transport = server_config.get("transport", "stdio")
    port = int(server_config.get("port", 8000))
    if transport == "http":
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
