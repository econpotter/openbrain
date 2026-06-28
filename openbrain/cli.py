"""
cli.py — CLI for ingesting documents into the openbrain library.

Usage:
    python -m openbrain.cli paper.pdf
    python -m openbrain.cli papers/
    python -m openbrain.cli paper.pdf --doc-type report
    python -m openbrain.cli paper.pdf --force
"""

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from openbrain import acquire as acquire_mod
from openbrain import brain

app = typer.Typer(help="openbrain library: ingest and deterministic acquisition.")
console = Console()


async def _ingest_one(path: Path, doc_type: str, force: bool) -> dict:
    return await brain.ingest_document_pipeline(str(path), doc_type, force)


async def _ingest_many(files: list[Path], doc_type: str, force: bool) -> list[dict]:
    results = []
    for f in files:
        try:
            r = await brain.ingest_document_pipeline(str(f), doc_type, force)
            r["status"] = "ok"
            results.append(r)
            console.print(f"[green]✓[/green] {r['title']}")
        except ValueError as e:
            results.append({"title": f.name, "status": "skipped", "error": str(e)})
            console.print(f"[yellow]–[/yellow] {f.name}: {e}")
        except Exception as e:
            results.append({"title": f.name, "status": "failed", "error": str(e)})
            console.print(f"[red]✗[/red] {f.name}: {e}")
    return results


@app.command()
def ingest(
    path: str = typer.Argument(..., help="File or directory to ingest"),
    doc_type: str = typer.Option("paper", "--doc-type", "-t", help="Document type"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-ingest duplicates"),
):
    """Ingest a PDF/text file or directory of files into the openbrain library."""
    target = Path(path).expanduser()

    if target.is_dir():
        files = sorted(
            list(target.glob("**/*.pdf"))
            + list(target.glob("**/*.txt"))
            + list(target.glob("**/*.md"))
        )
        if not files:
            console.print("[yellow]No PDF or text files found.[/yellow]")
            raise typer.Exit(1)

        console.print(f"Found [bold]{len(files)}[/bold] files in {target}\n")
        results = asyncio.run(_ingest_many(files, doc_type, force))

        ok = [r for r in results if r["status"] == "ok"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        console.print(
            f"\n[green]{len(ok)} ingested[/green]  "
            f"[yellow]{len(skipped)} skipped[/yellow]  "
            f"[red]{len(failed)} failed[/red]"
        )

    elif target.is_file():
        console.print(f"Ingesting [bold]{target.name}[/bold]...")
        try:
            result = asyncio.run(_ingest_one(target, doc_type, force))
        except FileNotFoundError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        except ValueError as e:
            console.print(f"[yellow]Skipped:[/yellow] {e}")
            raise typer.Exit(0)

        table = Table(show_header=False, box=None)
        table.add_row("Title", result["title"])
        table.add_row("Saved to", result["filepath"])
        table.add_row("Chunks", str(result["chunks"]))
        console.print(table)

    else:
        console.print(f"[red]Error:[/red] Path not found: {path}")
        raise typer.Exit(1)


def _emit(data) -> None:
    console.print_json(json.dumps(data, default=str))


@app.command()
def acquire(
    query: str = typer.Argument(..., help="Topic/keyword query"),
    venue: list[str] = typer.Option(
        None, "--venue", "-v", help="Restrict to venue abbrev(s); repeatable"
    ),
    since: int = typer.Option(None, "--since", help="Minimum publication year"),
    limit: int = typer.Option(50, "--limit", help="Max survivors to store"),
    min_overlap: int = typer.Option(
        2, "--min-overlap", help="Lexical recall-gate threshold"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview only; store nothing"
    ),
):
    """Search journal sources, prefilter deterministically, and store survivors."""
    report = asyncio.run(
        acquire_mod.acquire(
            query,
            venues_filter=set(venue) if venue else None,
            since=since,
            limit=limit,
            min_overlap=min_overlap,
            dry_run=dry_run,
        )
    )
    _emit(report)


@app.command(name="query-library")
def query_library(
    venue: str = typer.Option(None, "--venue", help="Canonical venue abbrev"),
    kind: str = typer.Option(None, "--kind", help="Venue kind"),
    year_min: int = typer.Option(None, "--year-min"),
    year_max: int = typer.Option(None, "--year-max"),
    status: str = typer.Option(None, "--status"),
    concept: str = typer.Option(None, "--concept"),
    sort: str = typer.Option("year", "--sort"),
    limit: int = typer.Option(50, "--limit"),
):
    """Deterministic structured query over the library."""
    rows = asyncio.run(
        brain.query_library_struct(
            venue=venue,
            kind=kind,
            year_min=year_min,
            year_max=year_max,
            status=status,
            concept=concept,
            sort=sort,
            limit=limit,
        )
    )
    _emit(rows)


@app.command()
def coverage(
    venue: str = typer.Option(None, "--venue"),
    concept: str = typer.Option(None, "--concept"),
):
    """Counts of library papers by year (optionally filtered)."""
    rows = asyncio.run(brain.coverage_stats(venue=venue, concept=concept))
    _emit(rows)


@app.command(name="list-venues")
def list_venues(
    field: str = typer.Option(None, "--field"),
    kind: str = typer.Option(None, "--kind"),
    unknown: bool = typer.Option(False, "--unknown", help="List uncategorized venues"),
):
    """List the venue registry, or discovered-but-uncategorized venues."""
    if unknown:
        rows = asyncio.run(brain.list_unknown_venues())
    else:
        rows = asyncio.run(brain.list_registry_venues(field=field, kind=kind))
    _emit(rows)


@app.command(name="export-bibtex")
def export_bibtex(
    venue: str = typer.Option(None, "--venue"),
    year_min: int = typer.Option(None, "--year-min"),
    status: str = typer.Option(None, "--status"),
    path: str = typer.Option(None, "--path", help="Write to file instead of stdout"),
):
    """Export matching library records as bibtex."""
    bibtex = asyncio.run(
        brain.export_bibtex_records(venue=venue, year_min=year_min, status=status)
    )
    if path:
        Path(path).write_text(bibtex)
        console.print(f"Wrote {bibtex.count('@')} record(s) to {path}")
    else:
        console.print(bibtex)


if __name__ == "__main__":
    app()
