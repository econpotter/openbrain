"""Minimal MCP streamable-HTTP JSON-RPC client.

Speaks the same protocol FastMCP's streamable-HTTP transport serves: a POST of a
JSON-RPC request, with the response returned either as `application/json` or as a
single Server-Sent-Events `message` frame. Enough to `initialize`, send the
`notifications/initialized` acknowledgement, and call tools. No dependency on the
MCP Python SDK — keeps the thin client thin.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """A transport- or protocol-level failure talking to the MCP server."""


class MCPToolError(RuntimeError):
    """The tool ran but returned an error result (`isError: true`)."""


def _parse_sse(body: str) -> dict:
    """Pull the JSON payload out of a single-message SSE response body."""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise MCPError("SSE response contained no data frame")


class MCPClient:
    """Synchronous MCP client over streamable HTTP.

    Use as a context manager so the underlying HTTP connection is closed::

        with MCPClient(cfg) as client:
            client.call_tool("capture_thought", {"content": "hi"})
    """

    def __init__(self, config) -> None:
        self._config = config
        self._client = httpx.Client(
            timeout=config.timeout,
            verify=config.verify_tls,
            headers={"Authorization": f"Bearer {config.key}"},
        )
        self._session_id: str | None = None
        self._request_id = 0
        self._initialized = False

    # -- context management -------------------------------------------------
    def __enter__(self) -> MCPClient:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- low-level RPC ------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(self, payload: dict) -> httpx.Response:
        try:
            return self._client.post(
                self._config.url, headers=self._headers(), json=payload
            )
        except httpx.HTTPError as exc:  # network/TLS/timeout — surface uniformly
            raise MCPError(f"request failed: {exc}") from exc

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            raise MCPError(f"{method} → HTTP {response.status_code}: {response.text[:200]}")

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            message = _parse_sse(response.text)
        else:
            message = response.json()

        if "error" in message:
            err = message["error"]
            raise MCPError(f"{method} → {err.get('code')}: {err.get('message')}")
        return message.get("result")

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        if response.status_code >= 400:
            raise MCPError(
                f"{method} notification → HTTP {response.status_code}: {response.text[:200]}"
            )

    # -- handshake ----------------------------------------------------------
    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "openbrain-cli", "version": "0.1.0"},
            },
        )
        # `_rpc` has already captured the Mcp-Session-Id header off the response;
        # subsequent calls send it back automatically.
        if result is None:
            raise MCPError("initialize returned no result")
        self._notify("notifications/initialized")
        self._initialized = True

    # -- tools --------------------------------------------------------------
    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call a tool and return its concatenated text content.

        Raises MCPToolError if the tool result is flagged `isError`.
        """
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise MCPError(f"tools/call {name} returned {type(result).__name__}, expected object")

        text = _text_from_content(result.get("content"))
        if result.get("isError"):
            raise MCPToolError(text or f"tool {name} reported an error")
        return text


def _text_from_content(content: Any) -> str:
    """Flatten an MCP content array into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts)
