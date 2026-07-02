# Plan J: Codex adapter — MCP + skills + command hooks, Claude-parity capture

## Goal

Bring Codex to the same effective behavior as the Claude Code plugin (Plan I):
openbrain MCP tools registered in every session, `openbrain-*` skills present,
push-recall at SessionStart, deterministic capture at Stop/PreCompact — via
`scripts/install_assistant_integrations.py` config-merge (not a Codex plugin
bundle; see design doc for why).

Design: `docs/design/2026-07-02-codex-adapter.md`. Read it first — it pins
several facts verified live on 2026-07-02 that this plan depends on (Codex
transcript schema, the broken `MCP_ACCESS_KEY` registration found on this
machine, hook stdin/output shapes).

## Dependency

**Plan H** (thin client) — landed. This plan extends it; does not replace it.

---

## Pinned constraints (verified live 2026-07-02 — not choices)

- Codex command hooks use the same event names, stdin JSON fields
  (`session_id`, `transcript_path`, `cwd`, `hook_event_name`), and
  `SessionStart` output contract as Claude Code. `openbrain brief` and
  `openbrain capture-transcript` need no change to speak the hook protocol
  itself.
- Codex transcript JSONL is **not** Claude's schema. Real shape:
  `{"type": "response_item", "payload": {"type": "message", "role": "user"|"assistant"|"developer", "content": [{"type": "input_text"|"output_text", "text": ...}]}}`.
  Exclude `developer` role (injected AGENTS.md/system boilerplate every turn).
- `~/.codex/hooks.json` may be a symlink into an unrelated project's hook
  file; any merge must resolve the symlink before writing (the installer
  already does this — `is_openbrain_entry()` is what needs updating).
- `~/.codex/config.toml`'s `[mcp_servers.openbrain]` block, if present, may
  reference the wrong env var (`MCP_ACCESS_KEY` found live, unexported)
  instead of `OPENBRAIN_MCP_KEY`. The installer must fix this, not just skip
  writing because an entry already exists.
- Verify Codex's actual `PreCompact` matcher values (`auto`/`manual`, assumed
  from Claude Code) against a live triggered compaction before shipping —
  unconfirmed.

## 1. Transcript parser: Codex line shape + auto-detect

`openbrain/client/transcript.py`:
- Add a Codex-shaped line parser: `type == "response_item"`, drill into
  `payload`, require `payload.type == "message"`, `payload.role in
  {"user", "assistant"}` (drop `developer`), join `content[].text` for
  blocks where `type in {"input_text", "output_text"}`.
- Auto-detect per-line: try Claude's shape (`type in {"user","assistant"}` at
  the top level with a sibling `message` key) first; if absent, try the Codex
  shape. No `--provider` flag — a single sweep pass may see both kinds of
  transcript.
- Keep `_line_text`'s existing fail-soft behavior: an unparseable or
  unrecognized line contributes empty text, never raises.

Test: unit test against a fixture built from the real sample structure (one
`session_meta`, one `developer` message, one `user` message, one `assistant`
message) — assert the delta contains only user/assistant text, in order,
`developer` excluded.

## 2. Explicit `source`, not a hardcoded default

`openbrain/client/cli.py`:
- `capture_transcript` command: accept `--source` (default falls back to
  `hook.get("source")` if present, else error rather than silently mislabel
  — no more hardcoded `"claude-code"` default eaten by every caller).
- `_sweep_transcripts`: change `dirs: list[Path]` to an ordered mapping of
  `{source_label: Path}` so each swept directory tags its own captures
  correctly. Update `DEFAULT_TRANSCRIPT_DIRS` accordingly:
  ```python
  DEFAULT_TRANSCRIPT_DIRS = {
      "claude-code": Path.home() / ".claude" / "projects",
      "codex": Path.home() / ".codex" / "sessions",
  }
  ```
- `sweep` command's `--transcript-dirs` option: decide whether it still
  accepts bare paths (source inferred from a fixed dir→label table) or needs
  a `dir:source` pairing syntax. Prefer the fixed table plus auto-detected
  parser (item 1) — the source label is really just a naming/analytics
  convenience once the parser can already tell the transcripts apart by
  shape.

## 3. Codex hook template (command-type, not prompt-type)

New file, e.g. `integrations/codex/hooks.json` (canonical source, mirroring
`integrations/claude-code/hooks/hooks.json`'s layout):

| Event | Matcher | Body |
|---|---|---|
| `SessionStart` | `startup`, `resume` (confirm exact matcher vocabulary live — Codex may not have `clear`) | `openbrain brief` |
| `Stop` | — | `openbrain capture-transcript --trigger stop --source codex` |
| `PreCompact` | `auto`, `manual` (verify live per pinned constraints) | `openbrain capture-transcript --trigger <matcher> --source codex` |

All `"type": "command"`, short timeouts, fail-soft (matches Plan I's Claude
Code hooks exactly — same CLI, same contract).

Retire `docs/hooks/codex-hooks.json` (prompt-type, dead — cannot call MCP
tools, same failure Plan I already fixed for Claude Code) and update
`docs/hooks/README.md`'s Codex section to point at the new command-type
template instead of describing the prompt-type shape as a draft to adapt.

## 4. Installer: MCP block + hook merge, both idempotent and self-healing

`scripts/install_assistant_integrations.py`:
- Add `merge_codex_mcp()`: write `[mcp_servers.openbrain]` into
  `~/.codex/config.toml` if absent. If present but pointing at the wrong
  `bearer_token_env_var` (i.e. not `OPENBRAIN_MCP_KEY`) or missing entirely,
  **fix it in place** — don't just skip because a same-named block exists.
  Back up the file first (existing `backup()` helper).
- Replace `merge_codex_hooks()`'s source template with the new command-type
  `integrations/codex/hooks.json`.
- Update `is_openbrain_entry()` to also recognize command-type entries —
  match on the `command` string containing `"openbrain "` (the CLI binary
  name), not just `entry.get("prompt")` text. Needed so re-running the
  installer replaces its own prior entries instead of duplicating them.
- Leave the Cursor branch (`copy_skills()`'s `targets["cursor"]`) untouched —
  deferred per design doc.

## 5. Docs

- `README.md` — add a Codex subsection under "Connect your AI clients"
  parallel to the Claude Code one: thin client install, `OPENBRAIN_MCP_KEY`
  export, run the installer.
- `integrations/README.md` — update the "Installing" section: Codex now gets
  MCP + hooks + skills from the installer, not just skills.
- Amend `docs/design/2026-07-01-thin-client-and-plugin.md`'s "Other providers
  (deferred — future Plan J)" section: Codex is no longer deferred (this
  plan), point to `docs/design/2026-07-02-codex-adapter.md`; correct the
  Cursor "no hook lifecycle" claim (stale — hooks exist, deferred by choice).

## Out of scope

- Cursor adapter — deferred (design doc).
- Codex plugin-bundle packaging — future, once manifest schema is verified
  live (design doc Decisions).
- pi.dev — unknown capabilities.

## Acceptance criteria

1. On a machine with only `OPENBRAIN_MCP_KEY` set (no `MCP_ACCESS_KEY`), a
   fresh Codex session has the openbrain MCP tools available — verified live,
   including on *this* machine where the registration is currently broken.
2. SessionStart injects a briefing/recall block as additional context in a
   Codex session — verified live.
3. Ending a substantive Codex session produces a searchable openbrain thought
   tagged `source: codex`, with real conversation text extracted from the
   actual Codex transcript schema (not silently empty) — verified live
   against a real session file, not a synthetic one.
4. Nightly sweep captures lagging Codex sessions from
   `~/.codex/sessions/**/*.jsonl` tagged `source: codex`, distinct from
   `source: claude-code` captures from the same sweep pass — verified live.
5. Trivial sessions produce no capture; tunnel-down sessions spool without
   error (reuses Plan H's spool — no new failure path introduced).
6. Re-running the installer is idempotent: it fixes a broken
   `bearer_token_env_var`, replaces its own prior hook entries without
   duplicating them, and does not touch the unrelated project's hook entries
   living in the same (possibly symlinked) `hooks.json`.
7. `docs/hooks/codex-hooks.json` prompt-type file retired; no doc describes
   it as installable.
8. No secret committed; `OPENBRAIN_MCP_KEY` is the only client-side env var
   Codex needs, matching Claude Code.
