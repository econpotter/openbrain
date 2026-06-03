"""
tools/library.py — ingest_document, ingest_directory, search_library,
                   update_library, update_library_status tools.
"""

from pathlib import Path

from openbrain import brain


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
            f"ingest_staged_document(filepath=\"{result['filepath']}\")"
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
            result = await brain.ingest_document_pipeline(filepath, doc_type, force, bibtex, title)
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
                f"update_library(query=\"{result['title']}\", bibtex=...) to fix it."
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

        Use this when the user asks about papers, reports, documentation, or
        document text. Use get_library_record for metadata about a known record.

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
    async def get_library_record(query: str) -> str:
        """
        Get metadata for one library record without searching document chunks.

        Use this when the user asks for a document's filepath, citation metadata,
        DOI, status, or catalog details.

        Args:
            query: Title fragment, author name, filename slug, filepath, or DOI.
        """
        record = await brain.get_library_record(query)
        if record is None:
            return f"No library record found matching '{query}'."
        if "multiple" in record:
            return f"Multiple matches: {', '.join(record['multiple'])}. Please be more specific."

        lines = [f"## {record['title']}"]
        authors = record["authors"] or []
        if authors:
            lines.append("Authors: " + ", ".join(authors))
        if record["year"]:
            lines.append(f"Year: {record['year']}")
        if record["source"]:
            lines.append(f"Source: {record['source']}")
        if record["doi"]:
            lines.append(f"DOI: {record['doi']}")
        if record["doc_type"]:
            lines.append(f"Type: {record['doc_type']}")
        if record["status"]:
            lines.append(f"Status: {record['status']}")
        if record["filepath"]:
            lines.append(f"Path: {record['filepath']}")
        if record.get("abstract"):
            abstract = record["abstract"][:500].replace("\n", " ")
            lines.append(f"Abstract: {abstract}")
        return "\n".join(lines)

    @mcp.tool
    async def update_library(
        query: str,
        bibtex: str | None = None,
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

        Use this to correct catalog metadata after ingestion. Do not re-ingest a
        document just to fix title, authors, DOI, source, type, or status.

        Supply bibtex to populate multiple fields at once from a BibTeX entry.
        Any explicitly provided fields (title, authors, etc.) override the BibTeX values.

        The source field is automatically abbreviated (e.g. "Journal of Economic
        Perspectives" → "jour-econ-pers").

        Args:
            query:     Search term to identify the record (title fragment, author name,
                       filename slug like "Smith2020", or DOI).
            bibtex:    BibTeX entry string — populates title, authors, year, source,
                       abstract, doi, and bibtex fields in one go.
            title:     New title (overrides bibtex).
            authors:   New authors as comma-separated string (overrides bibtex).
            year:      New publication year (overrides bibtex).
            source:    New source/journal, auto-abbreviated (overrides bibtex).
            abstract:  New abstract (overrides bibtex).
            doc_type:  New document type.
            status:    New status — ingested | reading | read | archived.
            doi:       New DOI (overrides bibtex).
        """
        matches = await brain.find_library_records(query)
        if not matches:
            return f"No library record found matching '{query}'."
        if len(matches) > 1:
            titles = ", ".join(r["title"] for r in matches)
            return f"Multiple matches: {titles}. Please be more specific."

        record = matches[0]
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
    async def update_library_status(query: str, status: str) -> str:
        """
        Update the status of a library record.

        Use this when the user says a paper/document is now reading, read,
        archived, or should return to ingested.

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

    @mcp.tool
    async def update_status(query: str, status: str) -> str:
        """
        Compatibility alias for update_library_status.

        Prefer update_library_status because the clearer name helps models
        choose the right tool when multiple MCP servers are available.

        Args:
            query:  Title fragment, author name, filename slug, or DOI.
            status: ingested | reading | read | archived
        """
        return await update_library_status(query, status)
