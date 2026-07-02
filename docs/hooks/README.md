# Auto-Capture Hook Templates

These files are copyable templates for agent harnesses that support lifecycle
hooks. They are not active while they live in this repository.

Use hooks to make auto-capture more reliable than instructions alone:

- session start or prompt-submit hooks should remind the agent to recall
  relevant openbrain context.
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

`codex-hooks.json` uses a Codex-like prompt hook shape with
`session_start`, `user_prompt_submit`, `pre_compact`, and `session_end`
events. If your Codex build supports this schema, place an adapted copy where
Codex loads hooks, commonly a global file such as:

```text
~/.codex/hooks.json
```

Do not copy a template blindly. First confirm the hook schema and payload format
for your Codex build.

## Claude Code

Claude Code is handled by the bundled plugin, not by a template here. The old
prompt-type template (`claude-code-hooks.json`) was retired: prompt hooks are
decision evaluators with no MCP tool access, so they could never call
`capture_thought`, and `SessionEnd` fires after the turn is over with nothing
left to inject.

The plugin under `integrations/claude-code/` uses `"type": "command"` hooks that
call the thin `openbrain` CLI: `SessionStart` push-recall (briefing + recall as
`additionalContext`) and `Stop` / `PreCompact` capture. Install it via the
repo-root `.claude-plugin/marketplace.json`; see
`integrations/claude-code/README.md`.

## Custom Harnesses

For a custom CLI or script, call the agent once at teardown with the stop prompt
from this directory and ensure the openbrain MCP tools are available to that
agent call.
