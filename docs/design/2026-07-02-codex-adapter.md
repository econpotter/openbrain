# Codex adapter: closing the gap with the Claude Code plugin

Date: 2026-07-02
Status: proposed (design conversation with operator)
Related plans: H (thin client, landed), I (Claude Code plugin, landed), J (this doc's plan)
Supersedes: the "Other providers (deferred — future Plan J)" section of
`docs/design/2026-07-01-thin-client-and-plugin.md`, which assumed facts about
Codex/Cursor hook capability that were true on 2026-07-01 and are partly stale
one day later (see below). Cursor stays deferred — by operator choice now, not
by missing capability.

## Problem

The Claude Code plugin (Plan I) gives every session, in any directory: openbrain
MCP tools, `openbrain:*` skills, push-recall at SessionStart, and deterministic
capture at Stop/PreCompact. Codex gets none of this automatically today — MCP
registration exists on at least one machine but is manually done and broken
(see pinned fact 3), skills are copied by a script but hooks are the old dead
prompt-type template that never worked (same failure mode Plan I fixed for
Claude Code).

Goal: bring Codex to the same effective behavior — push recall, auto-capture,
MCP tools everywhere — without assuming Codex's hook/plugin surface is
identical to Claude's. Every claim below was checked against a live machine
or live docs today, not carried over from the 2026-07-01 doc.

## Pinned facts (verified live, 2026-07-02)

1. **Codex command hooks are structurally identical to Claude Code's.**
   Verified against `developers.openai.com/codex/hooks`. Same event names
   (`SessionStart`, `Stop`, `PreCompact`/`PostCompact`, `UserPromptSubmit`,
   etc. — same PascalCase), same `"type": "command"` hook shape, same stdin
   JSON fields (`session_id`, `transcript_path`, `cwd`, `hook_event_name`),
   same `SessionStart` output contract
   (`{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}`).
   Practical effect: `openbrain brief` and `openbrain capture-transcript`
   (Plan H's thin CLI) need **no code change** to parse Codex hook stdin or
   emit Codex-readable output — only the transcript *content* parser differs
   (fact 2).

2. **Codex transcript JSONL schema is not Claude's schema.** Confirmed against
   a real file, `~/.codex/sessions/2026/06/22/rollout-2026-06-22T17-23-40-*.jsonl`:
   - Top-level line shape: `{"timestamp": ..., "type": "session_meta" | "event_msg" | "response_item" | "turn_context", "payload": {...}}`.
   - Conversation text lives at `type == "response_item"`, `payload.type == "message"`,
     `payload.role` in `{user, assistant, developer}`, `payload.content` is a
     list of `{"type": "input_text" | "output_text", "text": ...}` blocks.
   - `developer`-role messages carry injected AGENTS.md/system boilerplate on
     *every turn* (confirmed in the sample — hundreds of words of permissions
     and instructions text repeated). Must be excluded from the capture delta,
     same noise-exclusion principle `transcript.py` already applies to
     Claude's `tool_use`/`thinking` lines.
   - `openbrain/client/transcript.py`'s current parser (`_line_text`) only
     understands Claude's flat `{type: user|assistant, message: {role, content}}`
     shape and will silently extract nothing from a Codex transcript — a
     **silent capture failure**, not a crash, so it would go unnoticed without
     this check.
   - Session files live nested by date —
     `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl` — not flat
     like Claude's `~/.claude/projects/**/*.jsonl`. No code change needed here:
     the sweep already globs `**/*.jsonl`, which matches arbitrary depth.

3. **A live, broken, manual MCP registration already exists.** This machine's
   `~/.codex/config.toml` has:
   ```toml
   [mcp_servers.openbrain]
   url = "https://potter.nicholas-fuji.nord:8443/mcp"
   bearer_token_env_var = "MCP_ACCESS_KEY"
   ```
   `MCP_ACCESS_KEY` is **not exported anywhere on this machine** — only
   `OPENBRAIN_MCP_KEY` is (in `~/.zshrc`), which is the name the thin client
   and the Claude Code plugin both already use. This matches a duplicate/stale
   env-var issue already flagged in a recent openbrain thought capture
   (2026-07-02, "Environment variable export bug for MCP auth"). Practical
   effect: Codex's openbrain MCP tools are silently absent in every session on
   this machine today. Fix: standardize on `OPENBRAIN_MCP_KEY` everywhere,
   including this `bearer_token_env_var` value.

4. **`~/.codex/skills/` is a real, live directory** and
   `scripts/install_assistant_integrations.py`'s target
   (`~/.codex/skills/custom-skills/<name>`) matches it. No change needed here.

5. **The global Codex hooks file can be a symlink into an unrelated project.**
   On this machine, `~/.codex/hooks.json` → symlink →
   `~/projects/agentic-economist/.codex/hooks.json`, already wired with that
   project's own `session_start`/`user_prompt_submit`/`stop` entries, unrelated
   to openbrain. `install_assistant_integrations.py`'s `merge_codex_hooks()`
   already resolves the symlink before writing (`hooks_path.resolve()`), so
   merging is non-destructive by construction — but it currently writes the
   **dead prompt-type template** (`docs/hooks/codex-hooks.json`), which cannot
   call MCP tools, exactly the failure mode Plan I already fixed once for
   Claude Code. `is_openbrain_entry()` also only recognizes prompt-shaped
   entries (`entry.get("prompt")`), not command-shaped ones — needs updating
   before it can detect and safely replace its own command-type entries on a
   re-run.

## Decisions

**Config-merge, not a Codex plugin bundle, for v1.** Codex's changelog
mentions distributable "plugin bundles" (skills + MCP + hooks together,
Claude-Code-plugin-equivalent), but the manifest/marketplace schema for that
is not confirmed against live docs the way the hook schema was (fact 1). Per
the same discipline Plan I used ("verify the plugin manifest schema against
live docs before writing files; do not assume"), ship the lower-risk
config-merge path first: `~/.codex/config.toml` MCP block +
`~/.codex/hooks.json` command hooks + `~/.codex/skills/custom-skills/` copy,
all via `scripts/install_assistant_integrations.py`. Revisit a real Codex
plugin bundle once its schema is verified live — tracked as a future
enhancement, not blocking this plan.

**`transcript.py` auto-detects provider by line shape, no `--provider` flag.**
Claude lines and Codex lines are structurally distinguishable at the top
level (Claude: `type` in `{user, assistant}` with a sibling `message` key;
Codex: `type` in `{session_meta, event_msg, response_item, turn_context}`
with a nested `payload`). A single machine can have both kinds of transcript
on disk (nightly sweep scans both). Auto-detection keeps the thin client one
provider-agnostic binary, matching its existing design principle ("just
another MCP client, like `gh` is to the GitHub API") rather than forking it
per provider.

**`source` becomes explicit, not a hardcoded default.** Both
`capture_transcript` (CLI command) and `_sweep_transcripts` currently hardcode
`"source": "claude-code"`. Under two real providers this silently mislabels
every Codex capture. Hooks pass `--source codex` explicitly; the sweep tags
each configured directory with its own source label.

## Out of scope

- **Cursor** — deferred by operator choice for this round. Note for whoever
  revisits it: the 2026-07-01 doc's claim that Cursor has "no hook
  lifecycle, recall-only" is **stale** — Cursor shipped real command hooks
  (`sessionStart`, `stop`, `preCompact`, etc., confirmed against
  `cursor.com/docs/hooks` today) after that doc was written. Re-verify
  against live docs when picked up; don't carry this doc's Codex-specific
  findings over to Cursor without checking Cursor's own transcript/output
  schema, which differs from both Claude's and Codex's (flat
  `additional_context`, no `hookSpecificOutput` wrapper; `conversation_id`
  instead of `session_id`).
- **Codex plugin-bundle packaging** — future enhancement once the manifest
  schema is verified live (see Decisions).
- **pi.dev** — no evidence in this repo of what it supports; out of scope
  until its hook/MCP capabilities are known.

## Consequences for existing work

- `scripts/install_assistant_integrations.py`'s Codex branch needs real
  changes: MCP block merge (new), hook merge target (command-type, not
  prompt-type), and `is_openbrain_entry()` updated to recognize command
  entries.
- `docs/hooks/codex-hooks.json` (prompt-type) is dead on arrival, same as the
  old Claude Code prompt hooks were — retire it, replace with a command-type
  template.
- `openbrain/client/transcript.py` gains a second parser branch and
  auto-detection; `openbrain/client/cli.py`'s `capture_transcript` and
  `sweep`/`_sweep_transcripts` gain explicit `source` plumbing.
- The stale "Other providers" section of
  `docs/design/2026-07-01-thin-client-and-plugin.md` should be amended to
  point here for Codex and to correct the Cursor hook-lifecycle claim.
