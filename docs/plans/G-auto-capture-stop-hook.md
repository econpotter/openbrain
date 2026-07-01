# Plan G: Auto-capture session summaries on Stop **and** compaction

## Goal

A durable summary lands in openbrain **without the user saying "remember this"** —
both when a Claude Code session ends *and* every time context is compacted (auto
or manual). Compaction is where context is actually dropped, and a long session
compacts several times before it ever stops, so covering only Stop loses most of
the material. The trigger is dumb: it hands transcript text to openbrain; all
derivation (metadata extraction, embedding, entity links) is the write path's job
— see **Plan F**, which this plan depends on.

Concrete near-term slice of the broader **Plan D** (Auto-Capture Protocol).

## Dependency

**Requires Plan F (#010).** A bare `capture_thought(content=...)` must be
self-sufficient — F adds the server-side extraction + straggler backstop that
enriches a raw capture. Land F first.

---

## Why not "the same Stop hook, also on PreCompact"

Verified against the Claude Code hook docs (code.claude.com/docs/en/hooks):

- **Stop** supports `decision:"block"`, which re-prompts the agent — so a Stop
  hook *can* make the live agent summarize itself and call the MCP tool.
- **PreCompact** supports `decision:"block"` too, but it is a **veto only**: it
  does not re-prompt the agent or inject context. The "live agent summarizes
  itself" mechanism **cannot run on PreCompact**.

Both events expose `transcript_path`. So a unified trigger must **not** rely on
the agent re-entry trick. Move summarization out of the in-session agent into a
single command driven by the transcript. Same command on both events → genuinely
identical behavior, plus determinism and reuse outside Claude Code.

The prior abandoned experiment (PostToolUse Bash hook appending raw shell
commands to `.openbrain-queue.jsonl`, never drained) is the wrong shape — do not
revive it.

---

## Design

### 1. openbrain CLI: `capture-transcript` (the deliverable)

Add a subcommand parallel to the PDF ingest path (`python -m openbrain.cli`):

    python -m openbrain.cli capture-transcript <transcript_path> \
        --session <session_id> --trigger <stop|auto|manual>

Behavior:
- Read the transcript JSONL **delta** since this session's watermark (see §3).
- Summarize the delta with openbrain's **existing `extraction:` model**
  (`brain.complete_extraction_prompt` / the deepseek-v4-flash + gemini block).
  **Do not** shell out to a nested `claude` — that would recurse (a launched
  `claude` ends → fires its own Stop hook → captures again). Keep summarization
  inside openbrain's configured model.
- Call `capture_thought(content=<summary>, type="conversation", source="claude")`
  with no curated metadata — Plan F's write path extracts/embeds/links it.
- Advance the watermark.
- Skip (no capture) when the delta is trivial/no-op.

### 2. Hook wiring (dotfiles `~/.claude/settings.json`)

One-line command hook body, wired to **both** events:

| Event | Matcher | Body |
|-------|---------|------|
| `Stop` | (none — fires every turn end) | `capture-transcript … --trigger stop` |
| `PreCompact` | `auto` and `manual` | `capture-transcript … --trigger <auto\|manual>` |

Verify the current hook schema/payload before install; `docs/hooks/` (Plan D)
holds copyable templates. The hook passes `transcript_path` / `session_id` from
the event JSON on stdin.

### 3. Per-session watermark (prevents overlap across compactions)

A long session fires PreCompact several times then Stop once. Without a
watermark, each fire would re-summarize the whole session. Track the last-captured
transcript offset per `session_id` in a small state file (e.g.
`~/.claude/.openbrain-capture-state.json`). Each fire summarizes only lines past
the watermark and advances it. Server-side `content_fingerprint` dedup (upsert
merge) is the backstop; the watermark is the primary guard.

### 4. Instruction fallback (the approved "#3")

One sentence in global `~/.claude/CLAUDE.md`: "At session end, capture a
one-paragraph summary of durable decisions, project updates, and follow-ups via
the openbrain-capture skill; skip routine sessions." Self-contained; covers
sessions before/without the hook. Trim from `docs/agent-instructions/auto-capture.md`.

---

## Out of scope

- Write-path derivation — **Plan F (#010)**.
- Non-Claude-Code surfaces (Codex, Claude.ai) — **Plan D**.
- Adaptive capture classification — Plan D Step 4, deferred.

---

## Acceptance criteria

1. `capture-transcript` summarizes a transcript delta with openbrain's existing
   `extraction:` model and stores it via `capture_thought` — no nested `claude`
   process spawned (no recursion).
2. Wired to both `Stop` and `PreCompact` (matchers `auto` + `manual`), a real
   Claude Code session produces a searchable openbrain thought **on session end
   and on each compaction**, with no capture phrase from the user — verified live.
3. In a session that compacts multiple times, each capture covers only the delta
   since the prior watermark; captures do not overlap or duplicate (watermark +
   fingerprint) — verified.
4. The auto-captured thought, stored with only `content`, is fully enriched by
   the #010 write path (plans compose).
5. A trivial/no-op delta produces no capture.
6. The per-session watermark persists across fires and is keyed by `session_id`.
7. One-sentence teardown instruction installed in `~/.claude/CLAUDE.md`, working
   even if the hooks are absent.
