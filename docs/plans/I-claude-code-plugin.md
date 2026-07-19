# Plan I: Claude Code plugin — one install for MCP + skills + hooks

## Goal

A single `enabledPlugins` entry gives every Claude Code session, in any
directory, the openbrain MCP tools, the openbrain-* skills, push-recall at
session start, and deterministic capture at Stop/PreCompact. Replaces the
Claude Code path of `scripts/install_assistant_integrations.py` and the dead
prompt-type hooks in `docs/hooks/claude-code-hooks.json`.

Design: `docs/design/2026-07-01-thin-client-and-plugin.md`.

## Dependency

**Plan H** — hook bodies call the thin `openbrain` CLI. Land H first.

---

## Pinned constraints (not choices — verified against current hook/plugin docs)

- All hooks are `"type": "command"`. Prompt-type hooks cannot call MCP tools;
  do not use them.
- Hook commands reference bundled scripts via `${CLAUDE_PLUGIN_ROOT}` and read
  the event JSON (`transcript_path`, `session_id`, `cwd`, matcher) from stdin.
- SessionStart context injection uses the documented hook output shape
  (`hookSpecificOutput.additionalContext`).
- The bearer token is **never committed**: the plugin's MCP config references
  it via env expansion (e.g. `${OPENBRAIN_MCP_KEY}`). Verify expansion works
  live in headers; if plugin `.mcp.json` header expansion is unsupported,
  fall back to a small stdio shim script that reads the env var — do not
  inline the token.
- Verify the current plugin manifest/marketplace schema against the live docs
  before writing files; the layout below is the expected shape, not gospel.

## 1. Repo as marketplace

- `.claude-plugin/marketplace.json` at the repo root listing one plugin,
  source path `integrations/claude-code`.
- `integrations/claude-code/.claude-plugin/plugin.json` — name `openbrain`,
  version, description.

Install story (README + docs): add marketplace (local path or GitHub), enable
plugin, set `OPENBRAIN_MCP_KEY` in the environment, `uv tool install` the thin
client.

## 2. MCP registration

Plugin-bundled MCP config pointing at `https://openbrain.example.com:8443/mcp`
(streamable HTTP) with `Authorization` from `${OPENBRAIN_MCP_KEY}`. URL
overridable via env for future non-tunnel access. Result: tools exist in every
session regardless of cwd — closes the `~/.mcp.json` `$HOME`-scope failure.

## 3. Skills

Expose the five existing `integrations/skills/openbrain-*` skills through the
plugin (copy or symlink into the plugin's `skills/`; `integrations/skills/`
stays the canonical provider-neutral source). Must load as `openbrain:*` skills
in a fresh session — closes the nested `custom-skills/` failure.

While here: sharpen the proactive trigger language in `search`/`briefing`
skill descriptions and in the `search_brain`/`get_briefing` MCP tool
docstrings — "check FIRST when starting work on a project or when the user
references past context you don't have", not only "when the user asks".

## 4. Hooks (all command-type, all fail-soft)

| Event | Matcher | Body |
|---|---|---|
| `SessionStart` | startup/resume/clear | `openbrain brief` with cwd from stdin JSON → emit `additionalContext` (push recall) |
| `Stop` | — | `openbrain capture-transcript --trigger stop` (stdin JSON) |
| `PreCompact` | `auto`, `manual` | `openbrain capture-transcript --trigger <matcher>` |

Fail-soft is mandatory: short timeouts, exit 0 on any failure, no user-facing
noise when the tunnel is down (H's spool absorbs captures).

## 5. Migration / cleanup

- Remove the stale nested skills at `~/.claude/skills/custom-skills/openbrain-*`
  on install (or document the one-liner).
- Retire `docs/hooks/claude-code-hooks.json` (prompt-type; never worked) and cut
  the Claude Code branch from `scripts/install_assistant_integrations.py`,
  pointing it at the plugin. Codex/Cursor branches stay until Plan J.
- Add the one-sentence fallback instruction to `~/.claude/CLAUDE.md`
  (Plan G §4): capture durable session outcomes via the openbrain-capture
  skill even when hooks are absent.

## Out of scope

- Thin client internals, spool, sweep — **Plan H**.
- Codex/Cursor adapters — future Plan J.

## Acceptance criteria

1. With the plugin enabled and no other openbrain config on the machine, a
   fresh Claude Code session **in an arbitrary project directory** has the
   openbrain MCP tools and the `openbrain:*` skills — verified live.
2. SessionStart injects a briefing/recall block as additional context in that
   session — verified live (visible project-relevant memory without any user
   prompt).
3. Ending a substantive session produces a searchable openbrain thought with
   no capture phrase from the user; a compaction mid-session captures the
   pre-compaction delta (Plan G semantics via H's CLI) — verified live.
4. Trivial sessions produce no capture; with the tunnel down, session
   start/end show no errors and captures spool (flushed by H's sweep) —
   verified live.
5. No secret appears in the repo or plugin files; the token flows only through
   the environment.
6. Old install path cleaned up: nested `custom-skills` copies removed,
   prompt-type hook file retired, installer no longer offers the Claude Code
   branch.
