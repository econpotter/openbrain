# Plan G: Auto-capture session summaries via a Claude Code Stop hook

## Goal

End a Claude Code session and a durable summary lands in openbrain **without the
user saying "remember this"** and without interrupting mid-work. The trigger is
dumb: it hands raw session text to `capture_thought`. All derivation
(metadata extraction, embedding, entity links) is the write path's job — see
**Plan F**, which this plan depends on.

This is the concrete near-term slice of the broader **Plan D** (Auto-Capture
Protocol). Plan D covers every surface (Codex, Claude.ai, custom harness); this
plan ships the one that pays off first: Claude Code's `Stop` hook.

## Dependency

**Requires Plan F.** Without server-side extraction, an automated capture that
supplies only `content` would land with empty metadata and (on any embed hiccup)
no searchable embedding. Plan F makes a bare `capture_thought(content=...)`
self-sufficient. Land F first.

---

## Why a hook, not an instruction

A shell hook can't summarize — it sees tool I/O, not reasoning. But the Claude
Code `Stop` hook payload carries `transcript_path` and re-enters the agent, so
the *agent* can produce the summary and call the MCP tool. The prior abandoned
experiment (a PostToolUse Bash hook appending raw shell commands to
`.openbrain-queue.jsonl`) captured the wrong thing and never drained — do not
revive that shape.

Two layers, both cheap:

1. **Instruction (soft, ship immediately):** one sentence in global
   `~/.claude/CLAUDE.md` — "At session end, capture a one-paragraph summary of
   durable decisions, project updates, and follow-ups via the openbrain-capture
   skill; skip routine/no-op sessions." Harmless, covers sessions before/without
   the hook. (This is the user's approved "#3".)
2. **Stop hook (reliable):** a `Stop` hook in `~/.claude/settings.json` that
   re-enters the agent with a teardown prompt: summarize the session and call
   `capture_thought(content=<summary>, type="conversation", source="claude")`,
   with no metadata (the server extracts). Skip if the session produced nothing
   durable.

---

## Design

### 1. Instruction text

Reuse `docs/agent-instructions/auto-capture.md` (already exists, Plan D). Trim to
the teardown rule for the one-sentence global include. Source of truth stays in
the repo; installation is a copy into `~/.claude/CLAUDE.md` (and, per Plan D,
other surfaces later).

### 2. Stop hook

- Add a `Stop` entry to `~/.claude/settings.json` (dotfiles repo) whose prompt
  instructs the teardown capture. Model: `docs/hooks/claude-code-hooks.json`
  (exists, Plan D) — verify the current Claude Code hook schema/payload before
  install; do not install blind.
- The hook must re-enter with the openbrain MCP server connected (the capture
  skill already stops with a clear message if it isn't).
- **Idempotency / no double-capture:** the `capture_thought` write path already
  dedups on `content_fingerprint` (upsert merges), so a re-fired teardown on the
  same summary merges rather than duplicates. Rely on that; don't build a second
  guard.
- **No mid-work interruption:** `Stop` fires at session end only. Do not add
  PostToolUse/PreToolUse capture hooks — that was the failed experiment.

### 3. Session-start recall (optional, same hook family)

Per Plan D, a `SessionStart` hook can remind the agent to `search_brain` for
context on substantial tasks. Include it if cheap; it's not required for the
capture goal and can be a follow-up.

---

## Out of scope

- The write-path derivation pipeline — **Plan F**.
- Non-Claude-Code surfaces (Codex global, Claude.ai) — **Plan D** tracks those.
- Adaptive capture classification / confirmation loop — Plan D Step 4, deferred.

---

## Acceptance criteria

1. A substantive Claude Code session, ended normally, produces a searchable
   openbrain thought summarizing it — **without** the user issuing any capture
   phrase — verified by searching the brain after a real test session.
2. That auto-captured thought, having been stored with only `content`, is fully
   enriched (metadata + embedding + links) by the Plan F write path — confirming
   the two plans compose.
3. A trivial/no-op session does **not** create a junk capture (the teardown
   prompt's skip condition works).
4. Re-firing teardown on the same session does not create a duplicate thought
   (fingerprint dedup).
5. The Stop hook fires only at session end; no mid-session capture hooks are
   installed.
6. The one-sentence global instruction is installed in `~/.claude/CLAUDE.md` and
   is self-contained (works even if the hook is absent).
