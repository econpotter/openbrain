# Plan D: Auto-Capture Protocol

## Goal

Memory capture should be a passive side effect of normal agent work, not a
manual step. Agents should recall context at session start, capture high-value
facts at natural milestones, and flush durable session state on teardown without
asking the user to save information.

---

## Design Position

Auto-capture has to be installed where agents actually load instructions or
hooks. A Markdown file in this repository is only source text; it does not affect
global Codex, Claude, Claude.ai, or custom harness behavior unless those systems
include it.

Use three layers:

1. **Global or project instructions** define the capture policy.
2. **Lifecycle hooks** enforce recall and teardown capture where a client
   supports them.
3. **openbrain MCP tools** execute the reads and writes.

The OB1 `auto-capture` recipe is the right near-term model: it is a behavioral
workflow around evaluated session output. The OB1 adaptive capture
classification recipe is deferred because it needs a capture interface with a
confirmation loop and feedback storage.

---

## Status

- [x] Canonical auto-capture instruction text added:
      `docs/agent-instructions/auto-capture.md`
- [x] Copyable hook prompt templates added under `docs/hooks/`
- [x] Plan updated to distinguish source text from installed global behavior
- [ ] Install the instruction text into global Codex instructions
- [ ] Install the instruction text into Claude / Claude.ai instructions
- [ ] Verify Codex hook schema and install adapted hooks
- [ ] Add custom harness teardown calls where relevant

---

## Step 1: Install Global Instructions

Use `docs/agent-instructions/auto-capture.md` as the source of truth.

Install or include that text in every surface that should auto-capture:

| Surface | Install Location |
|---------|------------------|
| Codex global | `~/.codex/AGENTS.md` |
| Codex per repo | repo `AGENTS.md` |
| Claude Code global | `~/.claude/CLAUDE.md` |
| Claude Code per repo | repo `CLAUDE.md` |
| Claude.ai web | Project instructions or custom instructions |
| Custom harness | The system or developer prompt builder |

Keep the instruction short enough that it remains actionable. The agent should
capture durable facts, decisions, fixes, user preferences, milestones, and
follow-ups. It should not capture raw transcripts, command noise, or routine
progress.

---

## Step 2: Install Hooks Where Supported

Hooks make auto-capture more reliable than instructions alone.

Use `docs/hooks/` as copyable source material:

- Codex — retired the prompt-type template; Codex now installs command-type
  hooks + MCP + skills via `scripts/install_assistant_integrations.py` (Plan J).
- `claude-code-hooks.json` — prompt template for Claude Code settings hooks.
- `teardown-prompt.md` — final prompt for custom CLI or script harnesses.
- `README.md` — install notes and caveats.

Preferred hook behavior:

| Hook | Behavior |
|------|----------|
| `session_start` | Remind the agent to search openbrain for relevant context on substantial or unfamiliar tasks. |
| `stop` / teardown | Ask the agent to call `capture_thought` for durable session output before closing. |

Do not install hook templates blindly. First verify the hook schema and payload
format for the client or harness. A prompt-only hook is useful only if the hook
actually re-enters the agent with openbrain MCP tools available.

---

## Step 3: Keep MCP Tool Instructions Aligned

The MCP server should explain when `capture_thought` and `search_brain` are
appropriate, but it cannot force clients to call them. The client instruction or
hook layer owns initiation.

Use MCP instructions for tool affordances:

- "What do I know about X?" should call `search_brain`.
- "Remember this" should call `capture_thought`.
- Session close and natural milestones should capture durable facts when the
  client instruction or hook layer asks for that behavior.

---

## Step 4: Defer Adaptive Classification

Do not add OB1 adaptive classification tables yet.

That recipe becomes useful when openbrain has a capture surface with a real
confirmation loop, such as:

- a quick-capture CLI,
- Telegram or Slack capture,
- a browser/dashboard capture form,
- a custom agent wrapper that can ask for typed feedback.

At that point, evaluate:

- capture thresholds by type,
- classification outcome logs,
- user correction learning,
- A/B model comparisons.

Until then, the canonical capture path remains `capture_thought` plus nightly
maintenance synthesis.

---

## Completion Criteria

- Global Codex and Claude instruction files include the auto-capture protocol or
  an explicit include/copy of it.
- Hook templates are adapted and installed for clients that support lifecycle
  hooks.
- A test session produces searchable openbrain captures without the user saying
  "remember this."
- The nightly maintenance job consolidates captured raw/session memories into
  synthesis thoughts.
