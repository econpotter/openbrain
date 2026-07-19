# openbrain — Claude Code plugin

One `enabledPlugins` entry gives every Claude Code session, in any directory,
the openbrain MCP tools, the `openbrain:*` skills, push-recall at session start,
and deterministic capture at Stop / PreCompact. It replaces the old
`scripts/install_assistant_integrations.py` Claude Code path and the dead
prompt-type hooks in `docs/hooks/`.

## What the plugin bundles

| Component | File | Effect |
|---|---|---|
| MCP server | `.mcp.json` | Registers `openbrain` (streamable HTTP) in **every** session, regardless of cwd. Fixes the `~/.mcp.json` `$HOME`-scope failure. |
| Skills | `skills/openbrain-*` (symlinks to `../../skills/`) | Load as `openbrain:*` in a fresh session. Fixes the nested `custom-skills/` failure. |
| Hooks | `hooks/hooks.json` | `SessionStart` → `openbrain brief` (push recall via `additionalContext`); `Stop` / `PreCompact` → `openbrain capture-transcript`. All `"type": "command"`, all fail-soft. |

The heavy logic stays server-side. The hooks call the thin `openbrain` CLI
(Plan H), which speaks MCP to the same `:8443/mcp` endpoint every other client
uses and spools captures locally when the deployment is unreachable.

## Install

1. **Thin client** — the hooks shell out to the `openbrain` CLI, so it must be
   on `PATH`:

   ```bash
   uv tool install openbrain            # from PyPI once published, or:
   uv tool install /path/to/openbrain   # from this checkout
   ```

2. **Bearer token** — the plugin's `.mcp.json` reads it from the environment; it
   is never committed. Export it where Claude Code (and the hook shells) can see
   it, e.g. in your shell profile:

   ```bash
   export OPENBRAIN_MCP_KEY="<bearer token>"
   # optional: point at your deployment endpoint (replace with your deployment's URL)
   export OPENBRAIN_MCP_URL="https://openbrain.example.com:8443/mcp"
   ```

3. **Add the marketplace and enable the plugin.** This repo doubles as its own
   marketplace (`.claude-plugin/marketplace.json` at the root):

   ```
   /plugin marketplace add /path/to/openbrain      # or the GitHub owner/repo
   /plugin install openbrain@openbrain
   ```

Start a fresh session in any project directory: the `openbrain` MCP tools and
`openbrain:*` skills are present, and the SessionStart briefing appears as
injected context.

## Migrating off the old install path

If you previously ran `scripts/install_assistant_integrations.py`, remove the
stale nested skill copies it left one directory too deep (they never loaded):

```bash
rm -rf ~/.claude/skills/custom-skills/openbrain-*
```

Also drop any hand-merged openbrain prompt hooks from `~/.claude/settings.json`
— the plugin now owns Claude Code hooks. The installer no longer touches Claude
Code; it handles Codex and Cursor only.

## Configuration

- **Token** — `OPENBRAIN_MCP_KEY` (required), via env expansion in `.mcp.json`.
- **Endpoint** — `OPENBRAIN_MCP_URL` (optional; defaults to the tunnel URL).
- **TLS** — `OPENBRAIN_MCP_INSECURE=1` for a self-signed internal endpoint (the
  bearer token is the real authentication; see the thin client config).

Nothing here holds a secret: the token flows only through the environment.
