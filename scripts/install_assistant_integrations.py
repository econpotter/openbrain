#!/usr/bin/env python3
"""Install openbrain into user-level Codex and Cursor configs.

For Codex this brings full Claude-Code parity via config-merge: the MCP server
block in `~/.codex/config.toml`, command-type lifecycle hooks in
`~/.codex/hooks.json`, and the `openbrain-*` skills. Cursor gets the skills only
(hooks/MCP deferred by choice — see `docs/design/2026-07-02-codex-adapter.md`).

Claude Code is NOT handled here: it installs via the bundled Claude Code plugin
(`integrations/claude-code/`, listed in the repo-root `.claude-plugin/
marketplace.json`), which registers the MCP server, skills, and lifecycle hooks
in one `enabledPlugins` entry. See `integrations/claude-code/README.md`.

Idempotent and self-healing: re-running replaces only openbrain's own entries
(never an unrelated project's), fixes a stale MCP `bearer_token_env_var` in
place, and backs up each file it modifies (to <file>.bak).
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS = REPO_ROOT / "integrations"
SKILLS_SRC = INTEGRATIONS / "skills"

# Command-type hook template (mirrors the Claude Code plugin's layout). The old
# prompt-type docs/hooks/codex-hooks.json was retired: prompt hooks cannot call
# MCP tools, so they could never capture.
CODEX_HOOKS = INTEGRATIONS / "codex" / "hooks.json"

HOME = Path.home()

# openbrain MCP registration written into ~/.codex/config.toml. The bearer token
# is read by Codex from this env var — the same one the thin client and the
# Claude Code plugin use, so a single OPENBRAIN_MCP_KEY export drives every
# client. The value itself is never written to disk here.
MCP_KEY_ENV_VAR = "OPENBRAIN_MCP_KEY"
DEFAULT_MCP_URL = "https://rhythm.herknet:8443/mcp"


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def is_openbrain_entry(entry: dict) -> bool:
    """True for an openbrain hook entry, command-type or legacy prompt-type.

    Command-type entries nest `{"hooks": [{"type": "command", "command": ...}]}`;
    match on the `openbrain ` CLI invocation. The legacy prompt-type check is
    kept so a re-run over an older install still replaces its own dead entries.
    """
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and "openbrain " in str(h.get("command", "")):
            return True
    prompt = entry.get("prompt", [])
    text = "".join(prompt) if isinstance(prompt, list) else str(prompt)
    return "open brain" in text.lower() or "openbrain" in text.lower()


def merge_codex_hooks() -> bool:
    """Merge the command-type openbrain hooks into ~/.codex/hooks.json.

    Codex's hooks file uses Claude Code's schema: `{"hooks": {Event: [entry]}}`.
    Only openbrain entries are replaced, and only inside the `hooks` sub-object,
    so an unrelated project's entries in the same (possibly symlinked) file are
    left untouched. The symlink is resolved before writing.
    """
    hooks_path = HOME / ".codex" / "hooks.json"
    target_path = hooks_path.resolve() if hooks_path.is_symlink() else hooks_path
    incoming = load_json(CODEX_HOOKS).get("hooks", {})
    existing = load_json(target_path)
    existing_hooks = existing.setdefault("hooks", {})

    changed = False
    for event, entries in incoming.items():
        current = existing_hooks.get(event, [])
        kept = [e for e in current if not is_openbrain_entry(e)]
        merged = kept + entries
        if merged != current:
            existing_hooks[event] = merged
            changed = True

    # Sweep out our own dead prompt-type entries left by an older installer at
    # legacy top-level event keys (session_start, pre_compact, ...). Only
    # openbrain entries are dropped; an unrelated project's top-level entries in
    # the same file are preserved.
    for event in list(existing.keys()):
        entries = existing.get(event)
        if event == "hooks" or not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and is_openbrain_entry(e))]
        if kept != entries:
            if kept:
                existing[event] = kept
            else:
                del existing[event]
            changed = True

    if changed:
        backup(target_path)
        write_json(target_path, existing)
    return changed


def _codex_mcp_block() -> str:
    url = os.environ.get("OPENBRAIN_MCP_URL") or DEFAULT_MCP_URL
    return (
        "[mcp_servers.openbrain]\n"
        f'url = "{url}"\n'
        f'bearer_token_env_var = "{MCP_KEY_ENV_VAR}"\n'
    )


def _rewrite_bearer_env_var(text: str) -> str:
    """Rewrite bearer_token_env_var inside the [mcp_servers.openbrain] table to
    MCP_KEY_ENV_VAR, preserving url, comments, and every other table."""
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    fixed = False
    new_line = f'bearer_token_env_var = "{MCP_KEY_ENV_VAR}"'
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_block and not fixed:  # leaving the block without a bearer line
                out.append(new_line)
                fixed = True
            in_block = stripped == "[mcp_servers.openbrain]"
            out.append(line)
            continue
        if in_block and stripped.startswith("bearer_token_env_var"):
            out.append(new_line)
            fixed = True
            continue
        out.append(line)
    if in_block and not fixed:
        out.append(new_line)
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def merge_codex_mcp() -> str:
    """Register the openbrain MCP server in ~/.codex/config.toml, self-healing.

    Adds the [mcp_servers.openbrain] block if absent; if present but pointing at
    the wrong bearer_token_env_var (e.g. a stale MCP_ACCESS_KEY), fixes it in
    place rather than skipping. Returns one of: added | fixed | unchanged |
    unparseable. Backs up the file before any write.
    """
    path = HOME / ".codex" / "config.toml"
    text = path.read_text() if path.exists() else ""
    try:
        parsed = tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:
        return "unparseable"  # never clobber a config we can't safely edit
    ob = parsed.get("mcp_servers", {}).get("openbrain")

    if ob is None:
        block = _codex_mcp_block()
        if text and not text.endswith("\n"):
            text += "\n"
        new_text = f"{text}\n{block}" if text else block
        backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text)
        return "added"

    if ob.get("bearer_token_env_var") == MCP_KEY_ENV_VAR:
        return "unchanged"

    new_text = _rewrite_bearer_env_var(text)
    if new_text == text:
        return "unchanged"
    backup(path)
    path.write_text(new_text)
    return "fixed"


def copy_skills() -> list[str]:
    """Copy each integrations/skills/<name> dir into the user-level skills
    directory of each assistant. Returns list of skill names (re)installed.

    Claude Code is intentionally absent: it loads these skills through the
    bundled plugin's `skills/` directory, not a copied `custom-skills/` dir."""
    targets = {
        "codex": HOME / ".codex" / "skills" / "custom-skills",
        "cursor": HOME / ".cursor" / "skills-cursor",
    }
    installed = []
    for skill_dir in sorted(SKILLS_SRC.iterdir()):
        if not skill_dir.is_dir():
            continue
        for target_root in targets.values():
            dest = target_root / skill_dir.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
        installed.append(skill_dir.name)
    return installed


def main() -> None:
    codex_mcp = merge_codex_mcp()
    codex_changed = merge_codex_hooks()
    skills = copy_skills()

    print("Claude Code:       install via the plugin (integrations/claude-code/README.md)")
    print(f"Codex MCP server:  {codex_mcp}")
    print(f"Codex hooks:       {'updated' if codex_changed else 'already up to date'}")
    print(f"Skills installed/updated for codex, cursor: {', '.join(skills)}")


if __name__ == "__main__":
    main()
