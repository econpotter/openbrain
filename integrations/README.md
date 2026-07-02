# openbrain integrations

Assistant-facing skill files for openbrain. The openbrain **MCP server is the
contract**: it exposes the tools (`capture_thought`, `ingest_document`,
`search_brain`, `get_briefing`, ...) and is provider-agnostic. Any MCP client
can connect.

The files in this directory add no new capability. They teach assistants how to
route common requests to the right openbrain MCP tools.

```
integrations/
  README.md
  skills/                       # canonical, provider-neutral skill sources
    openbrain-capture/SKILL.md
    openbrain-ingest/SKILL.md
    openbrain-search/SKILL.md
    openbrain-briefing/SKILL.md
    openbrain-extraction/SKILL.md
  claude-code/                  # Claude Code plugin (MCP + skills + hooks)
    .claude-plugin/plugin.json
    .mcp.json
    hooks/hooks.json
    skills/ -> ../../skills/*   # symlinks to the canonical sources
```

## Skills

| Skill | Purpose | openbrain tool(s) |
|---|---|---|
| `openbrain-capture` | Persist a thought, decision, preference, or project update | Route to `remember_preference`, `capture_decision`, `capture_project_update`, or `capture_thought` by content type |
| `openbrain-ingest` | Add a document or directory to the library | Same-host: `ingest_document` / `ingest_directory`; remote: `prepare_document_ingest` -> copy -> `ingest_staged_document`; fix metadata with `update_paper` |
| `openbrain-search` | Answer "what do I know about X?" | `search_brain`, plus `find_papers`, `search_by_entity`, or `get_papers` when useful |
| `openbrain-briefing` | Show what's on the user's plate | `get_briefing`, plus calendar merge if available; `list_follow_ups` only for follow-up-specific or wider-window queries |
| `openbrain-extraction` | Consolidate recent memory | `run_memory_extraction(dry_run=True)` first; apply only after exact user approval |

The `openbrain-*` names are intentionally namespaced so they do not collide with
generic skills such as `search`, `capture`, or `ingest`.

## Prerequisite

The openbrain MCP server must be connected in the client session. With the
Compose stack up, it serves streamable HTTP on `:8000` (behind Caddy on `:8443`
for remote). MCP connection snippet:

```json
{
  "mcpServers": {
    "openbrain": {
      "type": "http",
      "url": "https://<CADDY_HOSTNAME>:8443/mcp",
      "headers": { "Authorization": "Bearer <MCP_ACCESS_KEY>" }
    }
  }
}
```

For a local stdio dev run, set `server.transport: stdio` in `config.yaml` and
point the client at `python -m openbrain.server`.

## Installing

- **Claude Code** — install the bundled plugin, which registers the MCP server,
  loads these skills as `openbrain:*`, and wires the SessionStart / Stop /
  PreCompact hooks in one step. See `claude-code/README.md`.
- **Codex / Cursor** — run `scripts/install_assistant_integrations.py`, or copy
  or link the `skills/` dirs to your preferred skill location.

The `claude-code/skills/` directory symlinks back to `skills/` here, so this
stays the single canonical source for every provider.
