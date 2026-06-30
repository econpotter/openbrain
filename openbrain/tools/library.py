"""
tools/library.py — ingest_document, ingest_directory, fetch_papers, find_papers,
                   get_papers, update_paper tools.
"""

import json
from pathlib import Path

from openbrain import brain


async def _locate_library_record(id: str | None, match: str | None):
    """Locate exactly one library record by uuid (`id`) or a `match` (doi/title,
    resolved deterministically like get_papers). Returns the record dict, or an
    error string the caller can return verbatim."""
    if id:
        matches = await brain.resolve_reference(paper_id=id)
    elif match:
        kind = brain.sources.detect_identifier_kind(match)
        ref = {"doi": match} if kind in ("doi", "arxiv") else {"title": match}
        matches = await brain.resolve_reference(**ref)
    else:
        return "Supply id (uuid) or match (a doi or title) to identify the record."
    if not matches:
        return "No library record matched."
    if len(matches) > 1:
        listed = ", ".join(f"{r['title']} [{r['id']}]" for r in matches)
        return f"Ambiguous — {len(matches)} matches: {listed}. Pass id to disambiguate."
    return matches[0]


def register(mcp):
    @mcp.tool
    async def prepare_document_ingest(filename: str) -> str:
        """
        Reserve a safe staging path for a remote PDF or text file before ingest.

        Use this first when the file is on a remote client machine. Ask the user
        or client to copy the file to the returned destination, then call
        ingest_staged_document with the staged filepath.

        Args:
            filename: Original filename, used only to preserve a readable suffix.
        """
        result = brain.prepare_ingest_staging_path(filename)
        return (
            "Upload the file to:\n"
            f"{result['filepath']}\n\n"
            "From the client machine, run:\n"
            f"scp /path/to/{filename} {result['scp_destination']}\n\n"
            "Then call:\n"
            f'ingest_staged_document(filepath="{result["filepath"]}")'
        )

    @mcp.tool
    async def ingest_document(
        filepath: str,
        doc_type: str = "paper",
        force: bool = False,
        bibtex: str | None = None,
        title: str | None = None,
    ) -> str:
        """
        Ingest a single document into the library.

        Use this for files already present on the MCP server filesystem. For
        remote PDFs or text files, use prepare_document_ingest first, then
        ingest_staged_document.

        Extracts text, looks up metadata via DOI → CrossRef, copies the file to
        LIBRARY_PATH with a standardized filename, chunks and embeds the content.

        If no metadata can be resolved automatically, the tool returns a message
        asking you to re-call with either bibtex or title supplied.

        Args:
            filepath:  Absolute or ~ path to the file (PDF or text).
            doc_type:  Type label — paper | report | documentation | data_dictionary | note | other.
            force:     Re-ingest even if the file has already been ingested.
            bibtex:    BibTeX entry string to use when automatic metadata lookup fails.
            title:     Plain title to use when automatic metadata lookup fails and no BibTeX is available.
        """
        path = Path(filepath).expanduser().resolve()
        try:
            result = await brain.ingest_document_pipeline(
                filepath, doc_type, force, bibtex, title
            )
        except FileNotFoundError as e:
            return str(e)
        except ValueError as e:
            return str(e)
        finally:
            if brain.is_ingest_staged_path(path) and path.is_file():
                path.unlink(missing_ok=True)
        msg = (
            f"Ingested: {result['title']}\n"
            f"Saved to: {result['filepath']}\n"
            f"Chunks:   {result['chunks']}"
        )
        if result.get("metadata_missing"):
            msg += (
                f"\n\nNo metadata found (no DOI detected). The record was saved with the "
                f"filename as title. Ask the user for a BibTeX entry or a title, then call "
                f'update_paper(match="{result["title"]}", bibtex=...) to fix it.'
            )
        return msg

    @mcp.tool
    async def ingest_staged_document(
        filepath: str,
        doc_type: str = "paper",
        force: bool = False,
        bibtex: str | None = None,
        title: str | None = None,
    ) -> str:
        """
        Ingest a document previously copied to the openbrain staging directory.

        Use this after prepare_document_ingest and SCP/upload have placed the
        file at the returned staged filepath. The ingest pipeline copies the
        PDF/text file into LIBRARY_PATH and removes the staged copy.

        Args:
            filepath: Staged path returned by prepare_document_ingest.
            doc_type: Type label — paper | report | documentation | data_dictionary | note | other.
            force: Re-ingest even if the file has already been ingested.
            bibtex: BibTeX entry to use when automatic metadata lookup fails.
            title: Plain title to use when no DOI/BibTeX metadata is available.
        """
        path = Path(filepath).expanduser().resolve()
        if not brain.is_ingest_staged_path(path):
            return (
                f"Not an openbrain staged file: {filepath}. "
                "Call prepare_document_ingest first and copy the file to that path."
            )
        return await ingest_document(str(path), doc_type, force, bibtex, title)

    @mcp.tool
    async def fetch_papers(
        identifier: str | None = None,
        identifiers: list[str] | None = None,
        topic: str | None = None,
        doc_type: str = "paper",
        sources: str | None = None,
        store: bool = False,
        author: str | None = None,
        year: int | None = None,
        title_threshold: float | None = None,
        force: bool = False,
    ) -> str:
        """
        Fetch papers from external sources (OpenAlex/Crossref/Semantic Scholar/
        arXiv) into the openbrain library, without a local file.

        Prefer a DOI or arXiv ID whenever you have one — those are exact lookups.
        A title query tolerates slight misspellings (fuzzy match) but a generic or
        unrelated title still resolves to "not found"; author/year/title_threshold
        tune the match.

        Modes (by input):
          - Lookup: supply identifier (DOI, arXiv ID, or title/author text).
            Resolves one paper across sources (first above threshold wins, then
            enriched from the rest), stores it, and embeds the abstract so it
            becomes semantically searchable via find_papers.
          - Batch: supply identifiers (a list of DOIs/arXiv IDs/titles). Each is
            looked up and stored independently; one bad identifier does not abort
            the rest. Returns a per-item summary.
          - Topic: supply topic instead. Fans out all sources and returns ranked
            candidates, storing NOTHING by default — pass store=true to bulk-store
            them.

        Use ingest_document instead when you have the actual PDF/text file.

        Returns a JSON object. Mode `lookup` → {mode, found, title, doi,
        abstract_stored, embedded, deduped, …}; `batch` → {mode, summary,
        results:[…]}; `topic` → {mode, candidates:[…], stored, stored_ids}.

        Args:
            identifier:  DOI, arXiv ID, or title/author text for known-item lookup.
                         DOI/arXiv preferred; a title may have minor misspellings.
            identifiers: List of DOIs/arXiv IDs/titles for batch known-item lookup.
            topic:       Topic/keyword query for discovery (candidates by default).
            doc_type:    Type label — paper | report | preprint | other.
            sources:     Comma-separated source override (e.g. "openalex,arxiv"),
                         otherwise uses the configured order.
            store:       Topic mode only — bulk-store the candidates when true.
            author:      Comma-separated author surname(s) corroborating a title.
            year:        Publication year corroborating a title.
            title_threshold: Override the fuzzy title match threshold (0–1).
            force:       Re-fetch and replace an existing record with the same DOI.
        """
        if not identifier and not identifiers and not topic:
            return json.dumps(
                {
                    "error": "Supply identifier (DOI/arXiv/title), "
                    "identifiers (list), or topic."
                }
            )
        source_order = [s.strip() for s in sources.split(",")] if sources else None
        author_list = (
            [a.strip() for a in author.split(",") if a.strip()] if author else None
        )

        result = await brain.fetch_papers(
            identifier=identifier,
            identifiers=identifiers,
            topic=topic,
            doc_type=doc_type,
            source_order=source_order,
            force=force,
            store=store,
            title_threshold=title_threshold,
            year=year,
            authors=author_list,
        )

        # The brain.fetch_papers result is already structured per mode
        # (batch/topic/lookup) — return it verbatim as JSON.
        return json.dumps(result, indent=2, default=str)

    @mcp.tool
    async def ingest_directory(
        dirpath: str,
        doc_type: str = "paper",
        force: bool = False,
    ) -> str:
        """
        Ingest all PDF and text files in a directory.

        Args:
            dirpath:  Path to the directory.
            doc_type: Type label applied to all files — paper | report | documentation | other.
            force:    Re-ingest duplicates.
        """
        path = Path(dirpath).expanduser().resolve()
        if not path.is_dir():
            return f"Not a directory: {dirpath}"

        files = sorted(
            list(path.glob("**/*.pdf"))
            + list(path.glob("**/*.txt"))
            + list(path.glob("**/*.md"))
        )
        if not files:
            return "No PDF or text files found."

        ok, skipped, failed = [], [], []
        for f in files:
            try:
                result = await brain.ingest_document_pipeline(str(f), doc_type, force)
                ok.append(result["title"])
            except ValueError as e:
                skipped.append(f"{f.name}: {e}")
            except Exception as e:
                failed.append(f"{f.name}: {e}")

        lines = [f"Processed {len(files)} files:"]
        if ok:
            lines.append(f"  Ingested ({len(ok)}): " + ", ".join(ok))
        if skipped:
            lines.append(f"  Skipped ({len(skipped)}):")
            lines.extend(f"    {s}" for s in skipped)
        if failed:
            lines.append(f"  Failed ({len(failed)}):")
            lines.extend(f"    {s}" for s in failed)
        return "\n".join(lines)

    @mcp.tool
    async def find_papers(
        text: str | None = None,
        ids: str | None = None,
        doi: str | None = None,
        title: str | None = None,
        journal: str | None = None,
        authors: str | None = None,
        year: int | None = None,
        limit: int = 10,
        year_min: int | None = None,
        doc_type: str | None = None,
        threshold: float = 0.4,
    ) -> str:
        """
        Topical/semantic search over the library — "papers related to X".

        Anchor it one of three ways (precedence: ids > reference > text):
          - text: a natural-language topic ("water economics").
          - ids: comma-separated library paper ids — returns papers similar to
            those (their centroid), excluding the anchors themselves.
          - reference: doi/title/journal/authors/year — resolves the known
            paper(s) (via get_papers' resolver) and anchors on them.

        Use get_papers when you know *which* paper you want (deterministic).

        Returns JSON: `anchors` (resolved anchor papers, [] for text mode),
        `ids` (distinct library ids in rank order), and `results` (matching
        records with title, authors, year, similarity, and an excerpt).

        Args:
            text:      Natural-language topic query.
            ids:       Comma-separated library paper ids to anchor on.
            doi/title/journal/authors/year: Bibliographic reference to anchor on.
            limit:     Max distinct papers. Default 10.
            year_min:  Only documents from this year or later.
            doc_type:  Filter by type (paper | report | documentation | …).
            threshold: Minimum similarity (0–1). Default 0.4.
        """
        anchors: list[dict] = []
        anchor_ids: list[str] | None = None

        if ids:
            anchor_ids = [s.strip() for s in ids.split(",") if s.strip()]
        elif any(v is not None for v in (doi, title, journal, authors, year)):
            author_list = (
                [a.strip() for a in authors.split(",") if a.strip()]
                if authors
                else None
            )
            anchors = await brain.resolve_reference(
                doi=doi, title=title, journal=journal, authors=author_list, year=year
            )
            if not anchors:
                return "No library record matched the reference; nothing to anchor on."
            anchor_ids = [str(a["id"]) for a in anchors]

        if anchor_ids:
            try:
                rows = await brain.search_library_by_anchors(
                    anchor_ids, threshold, limit, year_min, doc_type
                )
            except ValueError as exc:
                return f"Cannot find related papers: {exc}"
        elif text:
            rows = await brain.search_library_vec(
                text, threshold, limit, year_min, doc_type
            )
        else:
            return (
                "Provide text, ids, or a bibliographic reference "
                "(doi/title/journal/authors/year)."
            )

        result_ids: list[str] = []
        results = []
        for r in rows:
            lid = str(r["library_id"])
            if lid not in result_ids:
                result_ids.append(lid)
            results.append(
                {
                    "id": lid,
                    "title": r["title"],
                    "authors": r["authors"] or [],
                    "year": r["year"],
                    "source": r["source"],
                    "similarity": r["similarity"],
                    "section": r.get("section"),
                    "excerpt": r["chunk_text"][:300].replace("\n", " "),
                }
            )
            if len(result_ids) >= limit:
                break

        payload = {
            "anchors": [
                {"id": str(a["id"]), "title": a["title"], "year": a["year"]}
                for a in anchors
            ],
            "ids": result_ids,
            "results": results,
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool
    async def get_papers(
        id: str | None = None,
        doi: str | None = None,
        title: str | None = None,
        journal: str | None = None,
        authors: str | None = None,
        year: int | None = None,
    ) -> str:
        """
        Deterministically retrieve library record(s) by uuid or bibliographic
        reference — not by free-text search. Supply `id` (the uuid returned by
        get_papers/find_papers/store_papers) or any subset of doi/title/journal/
        authors/year; uuid and DOI match exactly, the rest on normalized fields.

        Use this when you know *which* paper(s) you want (you have a citation).
        Use find_papers for "papers about X" (semantic), or search by topic.

        Args:
            id: Library uuid (exact). Preferred when you have it.
            doi: DOI (exact, authoritative when present).
            title: Paper title (fuzzy match — tolerates case/punctuation/apostrophe and slight misspellings).
            journal: Journal/venue name or canonical abbrev.
            authors: One author or a comma-separated list (surname is enough).
            year: Publication year (exact).
        """
        if not any((id, doi, title, journal, authors, year)):
            return "Supply id (uuid) or a reference (doi/title/journal/authors/year)."
        author_list = (
            [a.strip() for a in authors.split(",") if a.strip()] if authors else None
        )
        rows = await brain.resolve_reference(
            paper_id=id, doi=doi, title=title, journal=journal,
            authors=author_list, year=year,
        )
        if not rows:
            return "No library record matched the reference."
        if len(rows) > 1:
            lines = [f"{len(rows)} matches — disambiguate (pass a DOI or add fields):"]
            for r in rows:
                year_s = f" ({r['year']})" if r.get("year") else ""
                doi_s = f" doi:{r['doi']}" if r.get("doi") else ""
                lines.append(f"- {r['title']}{year_s}{doi_s} [{r['id']}]")
            return "\n".join(lines)

        record = rows[0]
        lines = [f"## {record['title']}"]
        authors_out = record["authors"] or []
        if authors_out:
            lines.append("Authors: " + ", ".join(authors_out))
        if record["year"]:
            lines.append(f"Year: {record['year']}")
        if record["source"]:
            lines.append(f"Source: {record['source']}")
        if record["doi"]:
            lines.append(f"DOI: {record['doi']}")
        if record["doc_type"]:
            lines.append(f"Type: {record['doc_type']}")
        if record["filepath"]:
            lines.append(f"Path: {record['filepath']}")
        if record.get("abstract"):
            abstract = record["abstract"][:500].replace("\n", " ")
            lines.append(f"Abstract: {abstract}")
        return "\n".join(lines)

    @mcp.tool
    async def update_paper(
        id: str | None = None,
        match: str | None = None,
        bibtex: str | None = None,
        title: str | None = None,
        authors: str | None = None,
        year: int | None = None,
        source: str | None = None,
        abstract: str | None = None,
        doc_type: str | None = None,
        doi: str | None = None,
    ) -> str:
        """
        Update fields on one library record. Identify it by `id` (uuid) or
        `match` (a doi/title, resolved deterministically like get_papers).

        Use this to correct catalog metadata after ingestion. Do not re-ingest a
        document just to fix title, authors, DOI, source, or type.

        Supply bibtex to populate multiple fields at once from a BibTeX entry.
        Any explicitly provided fields (title, authors, etc.) override the BibTeX values.

        The source field is automatically abbreviated (e.g. "Journal of Economic
        Perspectives" → "jour-econ-pers").

        Args:
            id:        Library uuid (exact). Preferred when you have it.
            match:     A doi or title to locate the record when you lack the uuid.
            bibtex:    BibTeX entry string — populates title, authors, year, source,
                       abstract, doi, and bibtex fields in one go.
            title:     New title (overrides bibtex).
            authors:   New authors as comma-separated string (overrides bibtex).
            year:      New publication year (overrides bibtex).
            source:    New source/journal, auto-abbreviated (overrides bibtex).
            abstract:  New abstract (overrides bibtex).
            doc_type:  New document type.
            doi:       New DOI (overrides bibtex).
        """
        record = await _locate_library_record(id, match)
        if isinstance(record, str):
            return record  # error / ambiguity message

        updates: dict = {}

        if bibtex is not None:
            meta = brain._parse_bibtex(bibtex)
            if meta.get("title"):
                updates["title"] = meta["title"]
            if meta.get("authors"):
                updates["authors"] = meta["authors"]
            if meta.get("year"):
                updates["year"] = meta["year"]
            if meta.get("source"):
                updates["source"] = meta["source"]
            if meta.get("abstract"):
                updates["abstract"] = meta["abstract"]
            if meta.get("doi"):
                updates["doi"] = meta["doi"]
            updates["bibtex"] = bibtex

        if title is not None:
            updates["title"] = title
        if authors is not None:
            updates["authors"] = [a.strip() for a in authors.split(",")]
        if year is not None:
            updates["year"] = year
        if source is not None:
            updates["source"] = source
        if abstract is not None:
            updates["abstract"] = abstract
        if doc_type is not None:
            updates["doc_type"] = doc_type
        if doi is not None:
            updates["doi"] = doi
        if not updates:
            return "No fields to update."

        updated = await brain.update_library_record(str(record["id"]), updates)
        if not updated:
            return "Update failed."
        return f"Updated: {updated['title']} [{updated['doc_type']}]"
