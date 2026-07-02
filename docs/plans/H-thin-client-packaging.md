# Plan H: Thin client CLI + split packaging + server-side transcript capture

## Goal

A small `openbrain` CLI, installable on any workstation with
`uv tool install`, that talks MCP to the existing server on rhythm and gives
hooks/timers a deterministic way to capture and recall — while the server
(in the ai-stack compose stack) remains the only place heavy logic runs.

Design: `docs/design/2026-07-01-thin-client-and-plugin.md`. This plan
implements the thin-client half and **amends Plan G** (summarization moves
server-side). Plan I (plugin) wires hooks to this CLI and depends on it.

## Dependency

**Plan F (#010)** — the write path must enrich bare-content captures. Already
awaiting review; land it first.

---

## 1. Packaging split

- `pyproject.toml`: base dependencies = thin-client only (the `mcp` Python SDK
  or equivalent HTTP JSON-RPC client; keep it light — no asyncpg, no embedding
  or model stacks). Move server dependencies to an optional extra `[server]`.
- Console script entry point `openbrain` → the client CLI. The existing ingest
  CLI (`python -m openbrain.cli`) and server (`python -m openbrain.server`)
  keep working under `[server]`.
- `Dockerfile` installs `.[server]`. The ai-stack compose build must still
  produce a working server image (verify: `docker build` succeeds; server
  module imports).
- Guard: importing the client package must not import server-only modules.
  `uv tool install` from the repo on a machine without server deps yields a
  working `openbrain` command.

## 2. Client transport

MCP streamable-HTTP client against `https://rhythm.herknet:8443/mcp`:
initialize handshake, session header, bearer auth. Config resolution order:
env (`OPENBRAIN_MCP_URL`, `OPENBRAIN_MCP_KEY`) → `~/.openbrain/config.yaml` →
error with a clear message. **Trial-run against the real endpoint through the
tunnel early** — do not develop against a mock only.

## 3. Server: `capture_transcript` MCP tool

New tool (server-side, lives with the other capture tools):

    capture_transcript(content: str, session_id: str, trigger: str, source: str = "claude-code")

- Summarizes `content` (a transcript delta) with the existing `extraction:`
  model config — the same block Plan F uses; no new model keys.
- Stores the summary via the #010 write path (`capture_thought`-equivalent,
  bare content, `type="conversation"`) so enrichment/embedding/entity links
  happen asynchronously server-side.
- Returns whether a capture was stored; a trivial/no-op delta (nothing
  substantive to summarize) stores nothing and says so.
- Fingerprint dedup applies as usual (repeated delta → merge, not duplicate).

## 4. Client subcommands

- `openbrain capture-transcript <transcript_path> --session <id> --trigger <stop|auto|manual>`
  — reads the JSONL **delta** past the per-session watermark (Plan G §3:
  state file `~/.openbrain/capture-state.json`, keyed by session id), sends it
  to `capture_transcript`, advances the watermark only on acknowledged store
  or explicit trivial-skip. Also accepts hook JSON on stdin (Claude Code
  passes `transcript_path`, `session_id`, `cwd`) so a hook body needs no
  argument plumbing.
- `openbrain capture "<text>" [--type --tags --projects --people]` — direct
  passthrough to `capture_thought`.
- `openbrain brief [--cwd PATH] [--query TEXT]` — calls `get_briefing` plus a
  `search_brain` scoped to the project inferred from cwd; prints a compact
  plain-text block suitable for hook `additionalContext` injection. Must be
  fast (single round trip, short timeout) and exit 0 with empty output when
  the server is unreachable.
- `openbrain sweep [--transcript-dirs ...]` — (a) flush the spool; (b) walk
  transcript directories (default `~/.claude/projects`) and capture any
  session whose watermark lags its transcript. Idempotent.
- `openbrain status` — connectivity + spool depth + watermark summary, for
  humans debugging.

## 5. Fail-soft spool

Any capture that cannot reach the server is serialized to
`~/.openbrain/spool/*.json` and the command exits 0 (hooks must never block a
session or surface stack traces to the user). `sweep` flushes the spool
first-in-first-out; a flushed entry is removed only on acknowledged store.

## 6. Local sweep timer

systemd **user** units (`openbrain-sweep.service` + `.timer`, nightly) shipped
in `scripts/` with an install one-liner. This is the local, capture-side
counterpart to the server-side `openbrain-maintenance` container — that
container is untouched.

## Out of scope

- Hook wiring / plugin / skills — **Plan I**.
- Codex/Cursor adapters — future Plan J.
- Write-path enrichment — **Plan F (#010)**.

## Acceptance criteria

1. On a workstation, `uv tool install` from the repo yields a working
   `openbrain` CLI whose install pulls no server-only dependencies; the
   ai-stack Docker image still builds and serves with `.[server]`.
2. `openbrain capture "test thought"` performs a live round trip through the
   tunnel to rhythm — verified by finding the thought via `search_brain`
   afterward (real endpoint, not a mock).
3. `openbrain capture-transcript` on a real Claude Code transcript stores a
   server-side summary enriched by the #010 write path; called twice, the
   second run captures nothing new (watermark); a trivial transcript stores
   nothing.
4. With the server unreachable, `capture-transcript` and `capture` exit 0 and
   spool; a later `sweep` with the server up flushes the spool and the
   captures become searchable — verified live.
5. `openbrain brief` returns briefing + project-relevant thoughts in one round
   trip, and exits 0 with empty output when the server is down.
6. `capture_transcript` (server tool) summarizes with the existing
   `extraction:` config block — no new model keys, no nested `claude` process
   anywhere in the pipeline.
7. systemd user timer installs and a manual `systemctl --user start
   openbrain-sweep.service` completes a sweep on real transcript dirs.
