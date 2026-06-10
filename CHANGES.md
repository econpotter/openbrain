# Stateful Swarms & Fractal Embeddings — Notes for OpenBrain & Orchestra

## Fractal Embeddings (moonshot-fractal-embeddings)

Multi-scale embedding architecture where coarse scales capture high-level categories and fine scales capture specific distinctions. Key result: correct hierarchy improves retrieval (+0.72% vs flat); wrong hierarchy actively hurts (-0.10%).

**For OpenBrain:** Tag captures with a two-level hierarchy (coarse: `research/personal/professional`, fine: existing topic tags) and do a two-stage pgvector search — filter by coarse label first, rank by fine embedding similarity. ~20-line change to `search_brain`, no training required.

---

## Stateful Swarms (irys-stateful-swarms)

A typed, append-only blackboard with provenance tracking. Workers extract grounded evidence and explicitly write gap entries when something is missing. Entry types: `observation`, `analysis`, `gap`, `strategy`.

**Benchmark result:** Gemini Flash Lite + Flash 3.5 coordinated through a stateful swarm achieved 17.75% strict all-pass on Harvey LAB vs. 0% for the same models run statelessly. ~39x cheaper per task than Harvey's published baseline. Caveat: public benchmark only, not the private holdout.

**Key failure pattern:** In every near-miss, the raw information was present in the blackboard — the gap was in the final cross-reference/verification step, not retrieval.

---

## Ideas for OpenBrain

### 1. Add entry types to the capture store
Replace undifferentiated text chunks with typed entries: `[observation]`, `[analysis]`, `[gap]`, `[decision]`. One new column, same Postgres instance. Changes what `search_brain` can answer — "what are the open questions on X?" becomes possible.

### 2. Signal table
When starting a research thread, write explicit signals (open questions) before capturing notes. Notes optionally address a signal. `get_daily_briefing` surfaces open signals first. Pays off especially given frequent context switching across research, products, and trading.

### 3. Routing heuristic
- **Cheap tier (local/Haiku):** extraction and observation writing — coverage and recall tasks
- **Expensive tier (Sonnet):** cross-document analysis, synthesis, judgment calls

Make tier assignment explicit and logged for cost visibility.

---

## Orchestra — Diagnosis & Threshold for Revival

### Why it failed
Agents operated statelessly against an underspecified task representation. Each agent re-derived context from scratch, made locally-plausible moves that were globally wrong, and left no auditable trace.

### The architectural change that would matter
**The blackboard becomes the contract, not the issue.** Instead of an issue describing *what to do*, the blackboard describes *what is known, what has been concluded, what is still open*. Agents can't repeat ruled-out approaches or re-derive already-established facts.

**Mandatory gap logging before task close.** No task closes without enumerating: what was checked, what was found, what couldn't be verified. Reviewable without re-running.

### The honest caveat
Irys is validated on iterative document analysis — workers can't corrupt each other's extractions. Coding agents can corrupt each other's diffs. A partially-applied refactor is worse than no refactor.

**Revival threshold:** If the orchestration backlog is primarily research/analysis/writing tasks, the stateful blackboard is likely the change that makes it worth trying. If primarily code modification across repos, the bottleneck is sandboxed execution and rollback — a harder problem irys doesn't solve.
