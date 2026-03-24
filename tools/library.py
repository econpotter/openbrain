"""
tools/library.py — ingest_document, ingest_directory, search_library,
                   update_library, update_status tools (Phase 2).
"""

from pathlib import Path
import brain


def register(mcp):
    @mcp.tool
    async def ingest_document(filepath: str, doc_type: str = "paper", force: bool = False) -> str:
        """
        Ingest a single document into the library.

        Extracts text, looks up metadata via DOI → CrossRef, copies the file to
        LIBRARY_PATH with a standardized filename, chunks and embeds the content.

        Args:
            filepath:  Absolute or ~ path to the file (PDF or text).
            doc_type:  Type label — paper | report | documentation | data_dictionary | note | other.
            force:     Re-ingest even if the file has already been ingested.
        """
        try:
            result = await brain.ingest_document_pipeline(filepath, doc_type, force)
        except FileNotFoundError as e:
            return str(e)
        except ValueError as e:
            return str(e)
        return (
            f"Ingested: {result['title']}\n"
            f"Saved to: {result['filepath']}\n"
            f"Chunks:   {result['chunks']}"
        )

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
    async def search_library(
        query: str,
        year_min: int | None = None,
        doc_type: str | None = None,
        threshold: float = 0.4,
        count: int = 10,
    ) -> str:
        """
        Semantic search over the document library.

        Returns ranked results with title, authors, year, and the matching excerpt.

        Args:
            query:     Natural-language search query.
            year_min:  Only include documents from this year or later.
            doc_type:  Filter by type — paper | report | documentation | etc.
            threshold: Minimum similarity score (0–1). Default 0.4.
            count:     Maximum results to return. Default 10.
        """
        results = await brain.search_library_vec(query, threshold, count, year_min, doc_type)
        if not results:
            return "No results found."
        lines = []
        for r in results:
            score = f"{r['similarity']:.2f}"
            authors = r["authors"] or []
            author_str = " & ".join(authors[:2]) + (" et al." if len(authors) > 2 else "")
            header = f"[{score}] {r['title']}"
            if author_str:
                header += f" — {author_str}"
            if r["year"]:
                header += f" ({r['year']})"
            if r["source"]:
                header += f" [{r['source']}]"
            snippet = r["chunk_text"][:300].replace("\n", " ")
            lines.append(f"{header}\n  {snippet}")
        return "\n\n".join(lines)

    @mcp.tool
    async def update_library(
        query: str,
        title: str | None = None,
        authors: str | None = None,
        year: int | None = None,
        source: str | None = None,
        abstract: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        doi: str | None = None,
    ) -> str:
        """
        Update fields on a library record. Looks up by title, author, filepath, or DOI.

        The source field is automatically abbreviated (e.g. "Journal of Economic
        Perspectives" → "jour-econ-pers").

        Args:
            query:     Search term to identify the record (title fragment, author name,
                       filename slug like "Smith2020", or DOI).
            title:     New title.
            authors:   New authors as comma-separated string.
            year:      New publication year.
            source:    New source/journal (auto-abbreviated).
            abstract:  New abstract.
            doc_type:  New document type.
            status:    New status — ingested | reading | read | archived.
            doi:       New DOI.
        """
        matches = await brain.find_library_records(query)
        if not matches:
            return f"No library record found matching '{query}'."
        if len(matches) > 1:
            titles = ", ".join(r["title"] for r in matches)
            return f"Multiple matches: {titles}. Please be more specific."

        record = matches[0]
        updates: dict = {}
        if title is not None:
            updates["title"] = title
        if authors is not None:
            updates["authors"] = [a.strip() for a in authors.split(",")]
        if year is not None:
            updates["year"] = year
        if source is not None:
            updates["source"] = source  # brain.update_library_record will abbreviate
        if abstract is not None:
            updates["abstract"] = abstract
        if doc_type is not None:
            updates["doc_type"] = doc_type
        if status is not None:
            updates["status"] = status
        if doi is not None:
            updates["doi"] = doi
        if not updates:
            return "No fields to update."

        updated = await brain.update_library_record(str(record["id"]), updates)
        if not updated:
            return "Update failed."
        return f"Updated: {updated['title']} [{updated['doc_type']}] — {updated['status']}"

    @mcp.tool
    async def update_status(query: str, status: str) -> str:
        """
        Update the status of a library record.

        Args:
            query:  Title fragment, author name, filename slug, or DOI.
            status: ingested | reading | read | archived
        """
        matches = await brain.find_library_records(query)
        if not matches:
            return f"No library record found matching '{query}'."
        if len(matches) > 1:
            titles = ", ".join(r["title"] for r in matches)
            return f"Multiple matches: {titles}. Please be more specific."

        updated = await brain.update_library_record(str(matches[0]["id"]), {"status": status})
        if not updated:
            return "Update failed."
        return f"Updated: {updated['title']} → {updated['status']}"
