"""
server.py — FastMCP server exposing Open Brain tools.

Transport is controlled by the MCP_TRANSPORT environment variable:
  stdio  — for local development and testing (default)
  http   — streamable HTTP for remote access via Claude.ai / Gemini
"""

import os
import secrets
from fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from openbrain.tools import capture, search, library, entities, briefing


class StaticTokenVerifier:
    """Accept exactly one bearer token from MCP clients."""

    def __init__(self, token: str):
        self.token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self.token):
            return None
        return AccessToken(
            token=token,
            client_id="openbrain",
            scopes=[],
            expires_at=None,
        )


def _build_auth() -> tuple[AuthSettings | None, StaticTokenVerifier | None]:
    access_key = os.getenv("MCP_ACCESS_KEY", "").strip()
    if not access_key:
        return None, None

    caddy_hostname = os.getenv("CADDY_HOSTNAME", "").strip()
    if caddy_hostname:
        public_url = f"https://{caddy_hostname}:8443/mcp"
    else:
        port = int(os.getenv("PORT", "8000"))
        public_url = f"http://127.0.0.1:{port}/mcp"

    auth = AuthSettings(
        issuer_url=public_url,
        resource_server_url=public_url,
    )
    return auth, StaticTokenVerifier(access_key)


def _build_server() -> FastMCP:
    auth, token_verifier = _build_auth()
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
        token_verifier=token_verifier,
    )

mcp = _build_server()
capture.register(mcp)
search.register(mcp)
library.register(mcp)
entities.register(mcp)
briefing.register(mcp)


def main():
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    port = int(os.getenv("PORT", "8000"))
    if transport == "http":
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
