"""openbrain thin client.

A small MCP HTTP/JSON-RPC client and CLI installable with ``uv tool install``.
It speaks to the same ``:8443/mcp`` endpoint every other openbrain client uses;
all heavy logic (summarization, extraction, embedding) stays server-side.

Nothing in this subpackage may import server-only modules (``openbrain.brain``,
fastmcp, asyncpg, …). Base install pulls httpx + pyyaml + typer + rich only.
"""
