# Public-Release Prep Implementation Plan (openbrain)

> **For agentic workers:** Execute task-by-task. This plan PREPARES the repo
> for public release. Do NOT change repository visibility — the flip is a
> separate operator action after operator review.

**Goal:** Parameterize the private mesh hostname out of docs/scripts and give
the README a public-facing pass. Recon (2026-07-18) found no secrets and no
real personal data in 165 commits; MIT LICENSE present. The operator's gmail
in two plugin-manifest author blocks stays (operator decision 2026-07-18).

**Tech Stack:** Python/uv, Markdown, docker compose.

## Global Constraints

- Do NOT run `gh repo edit`, change visibility, or push. Local commits only.
- Do NOT commit any real hostname, token, or URL of the operator's private
  deployment. `rhythm.herknet` must not appear in the tree when done.
- README and prose follow `~/.config/agents/references/writing-style.md`.
- This repo runs in production on the operator's mesh — change documentation
  and defaults only; do not alter runtime behavior for configured deployments
  (env-var overrides must keep working exactly as they do now).

---

### Task 1: Parameterize the private mesh hostname

**Files:**
- Modify: every file matching `grep -rln "rhythm\.herknet" .` (~10 docs/scripts per recon)

- [ ] **Step 1: Inventory.** `grep -rn "rhythm.herknet" .` — list every hit
with context. Classify: docs prose, example commands, script defaults.

- [ ] **Step 2: Replace.**
  - Docs/examples: use a neutral placeholder host — `https://openbrain.example.com:8443/mcp` — with one sentence: "replace with your deployment's URL."
  - Script/config defaults: read from the existing `OPENBRAIN_MCP_URL` env var (the plugin `.mcp.json` already uses `${OPENBRAIN_MCP_URL:-...}` — change any real-hostname fallback to the example.com placeholder). The operator's real value lives in untracked local env, which keeps working.

- [ ] **Step 3: Verify.**

```bash
grep -rn "rhythm" . && echo "FIX" || echo "clean"
uv run pytest -q 2>&1 | tail -2
```

Expected: `clean`; test suite unchanged from baseline (record baseline first).

- [ ] **Step 4: Commit** — `git add -A && git commit -m "docs: parameterize deployment hostname for public release"`

---

### Task 2: Public-facing README pass

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Review as a stranger,** then edit. Keep existing technical
content (recon rated it solid). Required touches:
  - Open with 2-3 plain sentences: personal AI memory and knowledge base —
    MCP server over Postgres+pgvector storing thoughts/decisions/documents,
    with capture, semantic search, and extraction pipelines (state what the
    code actually does; verify claims against the tool list in the source).
  - Status line: "Alpha — in active development and daily personal use."
  - Deployment section uses the example.com placeholder from Task 1.
  - Install/run commands verified against the repo (files referenced exist).
  - License: MIT.

- [ ] **Step 2: Verify** — `grep -nE "rhythm|/home/potterzot" README.md` → no
output; style matches the guide.

- [ ] **Step 3: Commit** — `git commit -am "docs: public-facing README pass"`

- [ ] **Step 4: Report** — README diff summary + Task 1 replacement table for
operator review; explicit "ready for operator flip after README approval."
No visibility change, no push.
