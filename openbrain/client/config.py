"""Client configuration resolution.

Resolution order for the MCP endpoint and key:
    1. Environment (`OPENBRAIN_MCP_URL`, `OPENBRAIN_MCP_KEY`)
    2. `~/.openbrain/config.yaml`
    3. Error with a clear message.

TLS verification defaults on but can be disabled for an internal/self-signed
endpoint via `OPENBRAIN_MCP_INSECURE=1` or `insecure: true` in the config file
(the bearer token is the real authentication; TLS is transport only).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

OPENBRAIN_HOME = Path(os.environ.get("OPENBRAIN_HOME", Path.home() / ".openbrain"))
CONFIG_PATH = OPENBRAIN_HOME / "config.yaml"
SPOOL_DIR = OPENBRAIN_HOME / "spool"
STATE_PATH = OPENBRAIN_HOME / "capture-state.json"


class ConfigError(RuntimeError):
    """Raised when the endpoint or key cannot be resolved."""


@dataclass(frozen=True)
class ClientConfig:
    url: str
    key: str
    verify_tls: bool = True
    timeout: float = 30.0


def _load_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH}: expected a YAML mapping.")
    return data


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(timeout: float | None = None) -> ClientConfig:
    """Resolve the client configuration or raise ConfigError with guidance."""
    file_cfg = _load_file()

    url = os.environ.get("OPENBRAIN_MCP_URL") or file_cfg.get("url")
    key = os.environ.get("OPENBRAIN_MCP_KEY") or file_cfg.get("key")

    if not url or not key:
        missing = []
        if not url:
            missing.append("OPENBRAIN_MCP_URL (or `url` in config)")
        if not key:
            missing.append("OPENBRAIN_MCP_KEY (or `key` in config)")
        raise ConfigError(
            "openbrain client is not configured: missing "
            + ", ".join(missing)
            + f".\nSet the environment variables or create {CONFIG_PATH} with:\n"
            "  url: https://rhythm.herknet:8443/mcp\n"
            "  key: <bearer token>\n"
        )

    insecure_env = os.environ.get("OPENBRAIN_MCP_INSECURE")
    if insecure_env is not None:
        verify_tls = not _as_bool(insecure_env, False)
    else:
        verify_tls = not _as_bool(file_cfg.get("insecure"), False)

    resolved_timeout = timeout
    if resolved_timeout is None:
        resolved_timeout = float(file_cfg.get("timeout", 30.0))

    return ClientConfig(
        url=str(url),
        key=str(key),
        verify_tls=verify_tls,
        timeout=resolved_timeout,
    )
