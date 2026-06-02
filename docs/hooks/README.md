# Auto-Capture Hook Templates

These files are copyable templates for agent harnesses that support lifecycle
hooks. They are not active while they live in this repository.

Use hooks to make auto-capture more reliable than instructions alone:

- session start or prompt-submit hooks should remind the agent to recall
  relevant Open Brain context.
- stop or session-teardown hooks should ask the agent to capture durable
  facts before the session closes.

The exact hook file format is client-specific. Keep the canonical capture policy
in `docs/agent-instructions/auto-capture.md`, then adapt the templates here to
the harness that will execute them.

These templates are drafts to copy from, not installed configuration. Do not
symlink this directory into a client config path; copy the relevant template,
verify it against the client's current hook schema, and then install the adapted
copy outside the repository.

## Codex

`codex-hooks.template.json` uses a Codex-like prompt hook shape with
`session_start`, `user_prompt_submit`, and `stop` events. If your Codex build
supports this schema, place an adapted copy where Codex loads hooks, commonly a
global file such as:

```text
~/.codex/hooks.json
```

Do not copy a template blindly. First confirm the hook schema and payload format
for your Codex build.

## Claude Code

`claude-code-hooks.template.json` uses Claude Code's direct settings format for
`~/.claude/settings.json`, not the plugin `{"hooks": ...}` wrapper format. It
uses prompt hooks for `UserPromptSubmit` and `Stop`, because those are the
events where Claude Code prompt hooks can re-enter the agent with context.

If you install it in a plugin `hooks/hooks.json`, wrap the object in a top-level
`{"hooks": ...}` object. If your Claude Code installation does not support
prompt hooks, use global `~/.claude/CLAUDE.md`, project `CLAUDE.md`, or
Claude.ai project instructions instead.

## Custom Harnesses

For a custom CLI or script, call the agent once at teardown with the stop prompt
from this directory and ensure the Open Brain MCP tools are available to that
agent call.
