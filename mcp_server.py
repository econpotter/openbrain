"""
mcp_server.py — FastMCP server exposing Open Brain tools to Claude/Gemini.

Transport is controlled by the MCP_TRANSPORT environment variable:
  stdio  — for local development and testing (default)
  sse    — for remote access via Claude.ai / Gemini with Tailscale
"""

import os
from fastmcp import FastMCP
import tools.capture as capture
import tools.search as search
import tools.library as library
import tools.entities as entities
import tools.briefing as briefing

mcp = FastMCP("Open Brain")

# Phase 1 — core capture & recall
capture.register(mcp)
search.register(mcp)

# Phase 2–4 — stubs, uncomment as each phase is implemented
library.register(mcp)
entities.register(mcp)
briefing.register(mcp)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    port = int(os.getenv("PORT", "8000"))

    if transport == "sse":
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run()
