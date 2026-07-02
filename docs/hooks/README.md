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

Codex is handled by the installer, not by a template here. The old prompt-type
template (`codex-hooks.json`) was retired for the same reason the Claude Code one
was: prompt hooks are decision evaluators with no MCP tool access, so they could
never call `capture_thought`.

Codex uses Claude Code's hook schema — command-type entries under a top-level
`{"hooks": {...}}` object, PascalCase event names. The canonical template lives
at `integrations/codex/hooks.json`: `SessionStart` push-recall (`openbrain
brief`) plus `Stop` / `PreCompact` capture (`openbrain capture-transcript
--source codex`).

Run `scripts/install_assistant_integrations.py` to install it. The installer
merges the hooks into `~/.codex/hooks.json`, registers the MCP server in
`~/.codex/config.toml`, and copies the skills — all idempotent. Do not symlink
this repo's directory into a Codex config path.

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
