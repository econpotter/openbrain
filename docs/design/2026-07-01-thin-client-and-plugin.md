# Thin client + Claude Code plugin: making openbrain's interface actually fire

Date: 2026-07-01
Status: approved (design conversation with operator)
Related plans: F (#010, write-path unification), G (#011, auto-capture — **amended by this doc**), H (thin client), I (Claude Code plugin)

## Problem

openbrain works as an MCP server but its interface fails in practice, in two ways:

1. **Captures never fire automatically.** The hook definitions in
   `docs/hooks/claude-code-hooks.json` were never merged into
   `~/.claude/settings.json`, and they use `"type": "prompt"` hooks — which are
   decision evaluators with **no MCP tool access**, so they could never have
   called `capture_thought` anyway. SessionEnd additionally fires after the
   conversation is over; nothing can inject turns there.
2. **Agents never recall.** The MCP server is only registered in `~/.mcp.json`,
   which is scoped to sessions started in `$HOME` — in any project directory the
   openbrain tools simply do not exist. The assistant skills were installed one
   directory too deep (`~/.claude/skills/custom-skills/openbrain-*/` instead of
   `~/.claude/skills/openbrain-*/`), so they never load. No standing instruction
   in `~/.claude/CLAUDE.md` mentions openbrain, and tool descriptions are
   user-reactive ("when the user asks…") rather than proactive.

## Deployment reality (constrains the design)

- The server runs **on rhythm, inside the ai-stack docker compose stack**,
  behind Caddy at `https://rhythm.herknet:8443/mcp` (bearer auth), reached from
  workstations via the `rhythm-tunnel` ssh tunnel. Nothing openbrain runs
  locally today.
- Session transcripts live **on the local machine**
  (`~/.claude/projects/…/*.jsonl`, `~/.codex/sessions/…`). Any transcript
  processing trigger must run locally.
- Hooks are shell commands: they **cannot call MCP tools**. Deterministic
  capture/recall at session boundaries therefore requires a local executable.
  The only design question is whether that executable is *thin* or *fat*.

## Decision: thin client, server stays the only brain

Rejected: installing the full openbrain package locally with direct Postgres +
file-share access over the tunnel ("fat client"). It duplicates config, keys,
and schema knowledge on every machine, drifts across N installs, exposes
Postgres instead of one authed HTTP port, and abandons the core invariant that
**the MCP server is the contract**.

Adopted: a **thin client CLI** — one small entry point, installable with
`uv tool install`, that speaks MCP JSON-RPC (streamable HTTP) to the same
`:8443/mcp` endpoint every other client uses. It is *just another MCP client*,
like `gh` is to the GitHub API. All heavy logic (summarization, extraction,
embedding, entity linking) stays server-side, where Plan F (#010) already put
it.

### Interface layers after this design

| Consumer | Path |
|---|---|
| Agents in-session | MCP tools directly (server registered user-wide via plugin) |
| Hooks (Stop / PreCompact capture, SessionStart brief) | thin CLI → same MCP endpoint |
| Nightly sweep / spool flush | thin CLI (local systemd user timer) |
| Server-side maintenance (backfills, backup, consolidation) | existing `openbrain-maintenance` compose container — unchanged |

## Amendment to Plan G

Plan G §1 had `capture-transcript` summarize the transcript delta *locally*
using openbrain's configured `extraction:` model. That assumed a fat local
install (model config + API keys on every machine). Under the thin client:

- The client parses the transcript JSONL delta locally (watermark logic from
  Plan G §3 is unchanged) and sends the **delta text** to the server.
- A new server-side MCP tool (`capture_transcript`) summarizes the delta with
  the existing `extraction:` model **on rhythm** and stores the summary through
  the #010 write path (bare content → enriched). Trivial/no-op deltas are
  skipped server-side.
- Everything else in Plan G (Stop + PreCompact wiring, watermark, no nested
  `claude`, fingerprint dedup backstop) stands.

## Reliability: spool for tunnel-down

Remote-only brain means captures can race a down tunnel. The thin client fails
soft: when the server is unreachable, capture payloads are written to a local
spool (`~/.openbrain/spool/`); the sweep flushes the spool on next success.
Hooks never block session end and never lose a capture.

## Packaging

One repo, one `pyproject.toml`:

- **Base install** (`uv tool install openbrain`): thin client only — MCP client
  + stdlib; no asyncpg, no embedding stacks.
- **`[server]` extra**: FastMCP, asyncpg, embedding/model deps. The Dockerfile
  installs `.[server]`; a server upgrade is `docker compose build && up -d` on
  rhythm, in exactly one place, while thin clients stay dumb.

## Distribution: Claude Code plugin (Plan I)

The repo doubles as its own plugin marketplace (`.claude-plugin/marketplace.json`
at the root; plugin under `integrations/claude-code/`). The plugin bundles:

- **MCP registration** — server config with the bearer token referenced via
  env expansion (`${OPENBRAIN_MCP_KEY}`), never committed.
- **Skills** — the existing `integrations/skills/` files (canonical location),
  exposed through the plugin so they load in every session. Fixes the nested
  `custom-skills/` bug.
- **Hooks** — all `"type": "command"`, calling the thin client:
  - `SessionStart` → `openbrain brief` → emits `additionalContext` (briefing +
    top-k thoughts relevant to cwd). **Push recall**: memory arrives without
    the agent having to think of it.
  - `Stop` + `PreCompact` (matchers `auto`, `manual`) → `openbrain
    capture-transcript` (Plan G wiring, thin-client edition).

One `enabledPlugins` entry replaces the three config surfaces the old installer
poked (hooks merge, skills copy, MCP registration) and eliminates the drift that
caused failure 1 and half of failure 2.

## Other providers

The provider-agnostic layer is the MCP server + thin CLI, not the plugin.
Adapters are thin config merges.

**Codex — done, no longer deferred.** Plan J brought Codex to Claude-Code
parity (MCP block in `~/.codex/config.toml` + command-type hooks in
`~/.codex/hooks.json` + skills dir), all via
`scripts/install_assistant_integrations.py`. Codex uses Claude Code's hook
schema; only the transcript *content* parser differs. See
`docs/design/2026-07-02-codex-adapter.md`.

**Cursor — deferred by choice, not capability.** The earlier "recall-only —
no hook lifecycle" claim here is stale: Cursor shipped real command hooks
(`sessionStart`, `stop`, `preCompact`) after this doc was written. When picked
up, re-verify against live Cursor docs; its transcript/output schema differs
from both Claude's and Codex's (flat `additional_context`, no
`hookSpecificOutput` wrapper; `conversation_id` instead of `session_id`).

The nightly transcript sweep gives capture coverage even on providers with no
hooks.

## Consequences for existing work

- `scripts/install_assistant_integrations.py` is superseded for the Claude Code
  path (plugin replaces it) and will be retired or reduced to non-Claude
  providers.
- `docs/hooks/claude-code-hooks.json` prompt-type hooks are dead on arrival and
  are replaced by the plugin's command hooks.
- Queue #011 (Plan G as originally scoped) is superseded by Plans H + I; its
  unchanged parts (watermark, dual-event wiring, acceptance criteria) are
  carried into them.
- Plan F (#010) remains the enrichment foundation both plans compose with.
