"""findings.py — structured findings extraction (the second derivation).

Findings mirror the embedding pipeline in shape and lifecycle but share no engine:
a generative LLM reads each paper's text and emits provenance-linked causal
findings. ``extract_findings`` is the per-paper compute step (mirrors ``brain.embed``);
``backfill_findings`` heals papers left ``pending`` when the model was unreachable at
ingest (mirrors ``brain.backfill_chunk_embeddings``). Deliberately NOT a generic
derivation registry — two parallel pipelines, not a shared framework.

Extraction routes through ``brain.complete_extraction_prompt`` (the configurable
``extraction`` LiteLLM block with fallback). Model output is validated and off-schema
findings are dropped before the DB. A paper's findings rows and its
``library.findings_status`` update land in one transaction.
"""

import json
import logging

from openbrain import brain

logger = logging.getLogger(__name__)


def build_prompt(title: str, text: str) -> str:
    """Build the deterministic extraction prompt for one paper."""
    return (
        "Extract causal findings from this paper as a JSON array. Each finding: "
        '{"cause_text","effect_text","magnitude_value"(number|null),"magnitude_unit",'
        '"magnitude_stat"(coefficient|elasticity|correlation|p_value|null),'
        '"conditions","raw_quote","confidence"(0-1)}. '
        "Only findings the paper actually estimates. Return ONLY the JSON array.\n\n"
        f"TITLE: {title}\n\nTEXT:\n{text}"
    )


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_findings(raw: str) -> list[dict]:
    """Parse the model's JSON array, keeping only well-formed findings.

    A finding survives only with non-empty ``cause_text`` and ``effect_text``;
    ``magnitude_value``/``confidence`` are coerced to float-or-None (an uncoercible
    value does NOT drop the row, it just nulls that field). Anything that is not a
    JSON object, or any output that is not a parseable JSON array, is dropped.
    """
    try:
        data = json.loads(raw[raw.index("[") : raw.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for finding in data if isinstance(data, list) else []:
        if not isinstance(finding, dict):
            continue
        cause = (finding.get("cause_text") or "").strip()
        effect = (finding.get("effect_text") or "").strip()
        if not cause or not effect:
            continue
        out.append(
            {
                "cause_text": cause,
                "effect_text": effect,
                "magnitude_value": _to_float(finding.get("magnitude_value")),
                "magnitude_unit": finding.get("magnitude_unit"),
                "magnitude_stat": finding.get("magnitude_stat"),
                "conditions": finding.get("conditions"),
                "raw_quote": finding.get("raw_quote"),
                "confidence": _to_float(finding.get("confidence")),
            }
        )
    return out


async def _paper_text(conn, paper_id: str):
    """Title + best-available body text for a paper (body chunks, else abstract)."""
    row = await conn.fetchrow(
        "SELECT title, abstract FROM library WHERE id = $1", paper_id
    )
    if row is None:
        return None, ""
    body = await conn.fetchval(
        "SELECT string_agg(content, '\n') FROM chunks "
        "WHERE library_id = $1 AND kind = 'body'",
        paper_id,
    )
    return row["title"], (body or row["abstract"] or "")


async def extract_findings(paper_id: str) -> dict:
    """Extract, validate, and persist findings for one paper.

    Loads the paper's text, prompts the extraction model, parses/validates the
    output, and inserts the surviving rows + sets ``findings_status='done'`` in one
    transaction. A paper with no text is marked ``done`` with zero findings. A
    completion failure marks the paper ``error`` (the nightly backfill will NOT
    retry an ``error`` — only ``pending``) and is handled, not re-raised.

    Returns ``{paper_id, extracted, status}``.
    """
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        title, text = await _paper_text(conn, paper_id)

    if not text:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE library SET findings_status = 'done' WHERE id = $1", paper_id
            )
        return {"paper_id": paper_id, "extracted": 0, "status": "done"}

    try:
        raw = await brain.complete_extraction_prompt(build_prompt(title, text))
    except Exception as exc:
        logger.warning("Findings extraction failed for %s: %s", paper_id, exc)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE library SET findings_status = 'error' WHERE id = $1", paper_id
            )
        return {"paper_id": paper_id, "extracted": 0, "status": "error"}

    rows = parse_findings(raw)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for finding in rows:
                await conn.execute(
                    """
                    INSERT INTO findings
                        (paper_id, cause_text, effect_text, magnitude_value,
                         magnitude_unit, magnitude_stat, conditions, raw_quote, confidence)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    paper_id,
                    finding["cause_text"],
                    finding["effect_text"],
                    finding["magnitude_value"],
                    finding["magnitude_unit"],
                    finding["magnitude_stat"],
                    finding["conditions"],
                    finding["raw_quote"],
                    finding["confidence"],
                )
            await conn.execute(
                "UPDATE library SET findings_status = 'done' WHERE id = $1", paper_id
            )
    return {"paper_id": paper_id, "extracted": len(rows), "status": "done"}


async def backfill_findings(limit: int = 100) -> dict:
    """Extract findings for papers still ``pending`` (model down at ingest, etc.).

    Mirrors ``brain.backfill_chunk_embeddings``: count + select the oldest pending
    papers, run ``extract_findings`` on each, tally. Re-runnable; only touches
    ``pending`` papers (``error`` papers are left for a deliberate retry, ``done``
    papers are never reprocessed). Returns
    ``{pending_before, extracted_papers, failed, pending_after}``.
    """
    pool = await brain.get_pool()
    async with pool.acquire() as conn:
        pending_before = await conn.fetchval(
            "SELECT count(*) FROM library "
            "WHERE findings_status = 'pending' AND doc_type = 'paper'"
        )
        rows = await conn.fetch(
            "SELECT id FROM library "
            "WHERE findings_status = 'pending' AND doc_type = 'paper' "
            "ORDER BY created_at LIMIT $1",
            limit,
        )

    extracted = 0
    failed = 0
    for row in rows:
        try:
            res = await extract_findings(str(row["id"]))
        except Exception as exc:
            failed += 1
            logger.warning("Findings backfill failed for %s: %s", row["id"], exc)
            continue
        if res["status"] == "done":
            extracted += 1
        elif res["status"] == "error":
            failed += 1

    return {
        "pending_before": pending_before,
        "extracted_papers": extracted,
        "failed": failed,
        "pending_after": pending_before - extracted,
    }
