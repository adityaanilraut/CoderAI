"""CLI index and search subcommands."""

import asyncio
import sys
from pathlib import Path

import click


@click.command("index")
@click.option("--force", is_flag=True, help="Re-index all files, ignoring the manifest cache")
@click.option(
    "--paths",
    "-p",
    multiple=True,
    help="Specific files or directories to index (can be repeated). If omitted, indexes the whole project.",
)
def index_cmd(force, paths):
    """Build or update the semantic code search index."""
    from coderAI.system.config import config_manager
    from coderAI.embeddings.factory import create_embedding_provider
    from coderAI.context.code_indexer import CodeIndexer
    from coderAI.ui.display import display

    config = config_manager.load()
    provider = create_embedding_provider(config)
    if provider is None:
        display.print_error(
            "No embedding provider available. Set openai_api_key via "
            "`coderAI config set openai_api_key <key>` or OPENAI_API_KEY env var."
        )
        sys.exit(1)

    project_root = str(Path(config.project_root).resolve())
    indexer = CodeIndexer(project_root, provider)

    display.print_info(f"Project root: {project_root}")
    display.print_info("Indexing project (this may take a while on first run)...")

    try:
        result = asyncio.run(
            indexer.index(
                skip_if_unchanged=not force,
                paths=list(paths) if paths else None,
            )
        )
    except Exception as e:
        display.print_error(f"Indexing failed: {e}")
        sys.exit(1)

    stats = indexer.stats()
    display.print_success(
        f"Index updated: {result['added']} added, {result['updated']} updated, "
        f"{result['removed']} removed, {result['unchanged']} unchanged. "
        f"Total: {stats['chunks']} chunks from {stats['indexed_files']} files."
    )


@click.command("search")
@click.argument("query")
@click.option("--top-k", "-n", default=10, help="Number of results (default: 10)")
@click.option("--file-filter", "-f", default=None, help="Glob to filter results, e.g. '*.py'")
def search_cmd(query, top_k, file_filter):
    """Search the codebase with a natural-language query."""
    from coderAI.system.config import config_manager
    from coderAI.embeddings.factory import create_embedding_provider
    from coderAI.context.code_indexer import CodeIndexer
    from coderAI.ui.display import display

    config = config_manager.load()
    provider = create_embedding_provider(config)
    if provider is None:
        display.print_error("No embedding provider available. Set openai_api_key.")
        sys.exit(1)

    project_root = str(Path(config.project_root).resolve())
    indexer = CodeIndexer(project_root, provider)

    try:
        results = asyncio.run(indexer.search(query=query, top_k=top_k, file_filter=file_filter))
    except Exception as e:
        display.print_error(f"Search failed: {e}")
        sys.exit(1)

    if not results:
        display.print_warning("No results found. Is the index built? Run `coderAI index`.")
        return

    display.print_header(f'Semantic search results for: "{query}"')
    for i, r in enumerate(results, 1):
        display.print(
            f"\n[bold]{i}.[/bold] [cyan]{r['file_path']}[/cyan] "
            f"lines {r['start_line']}-{r['end_line']} "
            f"[dim]({r['language']}, score: {r['score']:.3f})[/dim]"
        )
        snippet = r["text"][:300]
        if len(r["text"]) > 300:
            snippet += "..."
        for line in snippet.split("\n")[:5]:
            display.print(f"    {line}")
