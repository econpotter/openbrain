# openbrain integrations

Assistant-facing glue for openbrain. The openbrain **MCP server is the
contract** — it exposes the tools (`capture_thought`, `ingest_document`,
`search_brain`, `get_briefing`, …) and is provider-agnostic: any MCP client can
connect. The directories here are thin per-provider wrappers that give those
tools ergonomic commands (`/ob:capture`, etc.). They add no new capability —
only routing and nicer invocation.

```
integrations/
  README.md          ← this file: canonical command catalog (source of truth)
  claude/            ← Claude Code plugin (local marketplace)
  gemini/            ← (future) Gemini extension
  codex/             ← (future) Codex prompts
```

When adding a new provider, restate the **canonical catalog** below in that
provider's format. Keep the *semantics* (which tool, routing, args) identical —
this README is the single source of truth.

## Canonical command catalog

| Command | Purpose | openbrain tool(s) |
|---|---|---|
| `capture` | Persist a thought/decision/preference/project-update | route to `remember_preference` / `capture_decision` / `capture_project_update` / `capture_thought` by content type |
| `ingest`  | Add a document or directory to the library | same-host: `ingest_document` / `ingest_directory`; remote: `prepare_document_ingest` → copy → `ingest_staged_document`; fix metadata with `update_library` (never re-ingest) |
| `search`  | "What do I know about X?" | `search_brain` (+ `search_library`, `search_by_entity`, `get_library_record`) |
| `briefing`| What's on my plate | `get_briefing` + `list_follow_ups` (+ calendar merge if available) |

Routing detail for each command lives in `claude/skills/<command>/SKILL.md` —
those bodies are the reference wording; mirror them per provider.

## Prerequisite (all providers)

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

> Commands fail when embeddings are down (every capture/search embeds text).
> If you see `All embedding providers failed: …`, fix the embed provider
> (`config.yaml: embed`) — not the command.

## Claude Code (`integrations/claude/`)

A self-contained local marketplace exposing `/ob:capture`, `/ob:ingest`,
`/ob:search`, `/ob:briefing`.

### Install

```
/plugin marketplace add <path-to-repo>/openbrain/integrations/claude
/plugin install ob@ob
```

Then the `/ob:*` commands are available in every project on the machine.
Restart the session if they don't appear immediately. Verify with `/plugin`
(should list `ob@ob` enabled) and that `mcp__openbrain__*` tools are present.

### Update after editing a skill

```
/plugin marketplace update ob
```

(or remove + re-add the marketplace). Skill edits are picked up on next session.
