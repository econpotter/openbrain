"""
cli.py — CLI for ingesting documents into the openbrain library.

Usage:
    python -m openbrain.cli paper.pdf
    python -m openbrain.cli papers/
    python -m openbrain.cli paper.pdf --doc-type report
    python -m openbrain.cli paper.pdf --force
"""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from openbrain import brain

app = typer.Typer(help="Ingest documents into the openbrain library.")
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


if __name__ == "__main__":
    app()
