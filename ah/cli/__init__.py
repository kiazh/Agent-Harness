"""AgentHarness CLI — `ah` command."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

import typer

logger = logging.getLogger(__name__)
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.text import Text

from ah import __version__
from ah.core.agent import ReActAgent
from ah.core.context import context_manager
from ah.core.provider import get_provider
from ah.core.session import session_manager
from ah.core.config import config
from ah.db.connection import db
from ah.tools import builtins  # noqa: F401 — registers built-in tools

app = typer.Typer(
    name="ah",
    help="AgentHarness — self-hosted multi-agent AI orchestration framework",
    no_args_is_help=True,
)
console = Console()


def _run(coro):
    """Run async coroutine from sync Typer command."""
    return asyncio.run(coro)


@app.command()
def chat(
    message: str = typer.Argument(None, help="Message to send to the agent"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="Continue last session"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
    model: str = typer.Option(None, "--model", "-m", help="Model to use (e.g., anthropic/claude-3.5-sonnet)"),
    provider: str = typer.Option("openrouter", "--provider", "-p", help="LLM provider (openrouter, ollama)"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Launch interactive REPL mode"),
):
    """Chat with the agent. Creates a new session or continues an existing one."""

    if interactive:
        from ah.cli.interactive import run_repl
        _run(run_repl(model=model, provider=provider, verbose=verbose, session_id=session_id))
        return

    async def _chat():
        await db.connect()
        try:
            # Determine session
            if continue_:
                session = await session_manager.get_last_active()
                if not session:
                    console.print("[red]No active session to continue.[/red]")
                    raise typer.Exit(1)
                console.print(f"[dim]Continuing session: {session.id}[/dim]")
            elif session_id:
                sid = uuid.UUID(session_id)
                session = await session_manager.get(sid)
                if not session:
                    console.print(f"[red]Session {session_id} not found[/red]")
                    raise typer.Exit(1)
                console.print(f"[dim]Resuming session: {session.id}[/dim]")
            else:
                session = await session_manager.create(
                    title=message[:50] if message else None,
                    goal=message[:100] if message else None,
                )
                console.print(f"[dim]New session: {session.id}[/dim]")

            if not message:
                console.print("[yellow]No message provided. Use: ah chat \"your message\"[/yellow]")
                raise typer.Exit(0)

            # Create LLM provider
            try:
                llm = get_provider(provider=provider, model=model)
            except ValueError as e:
                console.print(f"[red]Provider error:[/red] {e}")
                raise typer.Exit(1)

            # Create agent and run
            agent = ReActAgent(provider=llm)
            if verbose:
                console.print(f"[dim]Model: {llm.model} ({provider})[/dim]")
                console.print()

            # Stream response with Rich Live display
            response_text = ""
            tool_calls_count = 0
            tokens_used = 0

            with Live(console=console, refresh_per_second=10, transient=False) as live:
                async for event in agent.run_stream(session.id, message, verbose=verbose):
                    if event.type == "text":
                        response_text += event.content
                        live.update(Text(response_text, style="green"))
                    elif event.type == "tool_call":
                        tool_calls_count += 1
                        if verbose:
                            live.update(Text(response_text + f"\n\n[yellow]→ {event.tool_name}({event.tool_args})[/yellow]", style="green"))
                    elif event.type == "tool_result":
                        if verbose:
                            preview = str(event.tool_result)[:100].replace("\n", " ")
                            live.update(Text(response_text + f"\n\n[green]← {preview}[/green]", style="green"))
                    elif event.type == "token_usage":
                        tokens_used = event.tokens_used
                    elif event.type == "done":
                        response_text = event.response.content
                        tool_calls_count = len(event.response.tool_calls)
                        tokens_used = event.response.tokens_used
                        live.update(Text(response_text, style="green"))

            console.print()
            console.print(Panel(response_text, title="Agent", border_style="green"))
            console.print()
            if verbose:
                console.print(f"[dim]Iterations: {event.response.iterations} | Tool calls: {tool_calls_count} | Tokens: {tokens_used}[/dim]")
            console.print(f"[dim]Session ID: {session.id}[/dim]")

        finally:
            await db.close()

    _run(_chat())


@app.command()
def repl(
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(None, "--provider", "-p", help="LLM provider"),
    verbose: bool = typer.Option(None, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
):
    """Launch interactive REPL mode."""
    from ah.cli.interactive import run_repl
    _run(run_repl(model=model, provider=provider, verbose=verbose, session_id=session_id))


@app.command()
def status():
    """Show AgentHarness status and recent sessions."""

    async def _status():
        await db.connect()
        try:
            # Check DB
            try:
                version = await db.fetchval("SELECT version()")
                console.print(f"  PostgreSQL: [green]connected[/green] ({version.split(',')[0]})")
            except Exception as e:
                console.print(f"  PostgreSQL: [red]connection failed[/red] ({e})")
                return

            # Count sessions
            count = await db.fetchval("SELECT COUNT(*) FROM sessions")
            console.print(f"  Sessions: {count}")

            # Count context chunks
            chunks = await db.fetchval("SELECT COUNT(*) FROM context_chunks")
            console.print(f"  Context chunks: {chunks}")

            # Check tools
            from ah.tools.base import registry
            tools = registry.list_tools()
            console.print(f"  Tools: {len(tools)} registered")
            for t in tools:
                console.print(f"    - {t}")

            # Check env
            import os
            if os.environ.get("OPENROUTER_API_KEY"):
                console.print("  OpenRouter API key: [green]set[/green]")
            else:
                console.print("  OpenRouter API key: [yellow]not set[/yellow]")

            console.print()
            console.print("[dim]Run `ah chat \"your message\"` to start.[/dim]")

        finally:
            await db.close()

    _run(_status())


@app.command(name="sessions")
def list_sessions(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of sessions to show"),
    status_filter: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status (active, idle, archived)"),
):
    """List recent sessions."""

    async def _sessions():
        await db.connect()
        try:
            sessions = await session_manager.list_sessions(status=status_filter, limit=limit)

            if not sessions:
                console.print("[yellow]No sessions found.[/yellow]")
                return

            table = Table(title="Sessions")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Title", style="white")
            table.add_column("Status", style="green")
            table.add_column("Agent", style="dim")
            table.add_column("Goal", style="dim")
            table.add_column("Last Activity", style="dim")

            for s in sessions:
                table.add_row(
                    str(s.id)[:8],
                    s.title or "(untitled)",
                    s.status,
                    s.agent_id,
                    (s.goal or "")[:40],
                    s.last_activity.strftime("%Y-%m-%d %H:%M"),
                )

            console.print(table)
        finally:
            await db.close()

    _run(_sessions())


@app.command()
def context(
    session_id: str = typer.Argument(None, help="Session ID to inspect"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of chunks to show"),
):
    """View context chunks for a session."""

    async def _context():
        await db.connect()
        try:
            if session_id:
                sid = uuid.UUID(session_id)
            else:
                session = await session_manager.get_last_active()
                if not session:
                    console.print("[yellow]No active sessions.[/yellow]")
                    return
                sid = session.id

            session = await session_manager.get(sid)
            if session:
                console.print(f"[bold]Session:[/bold] {session.id}")
                console.print(f"[bold]Title:[/bold] {session.title or '(untitled)'}")
                console.print(f"[bold]Status:[/bold] {session.status}")
                console.print(f"[bold]Goal:[/bold] {session.goal or '(none)'}")
                console.print(f"[bold]Budget:[/bold] {session.context_budget} tokens")
                console.print()

            chunks = await context_manager.get_chunks(sid, limit=limit)
            if not chunks:
                console.print("[yellow]No context chunks found.[/yellow]")
                return

            table = Table(title=f"Context Chunks (session {str(sid)[:8]})")
            table.add_column("Type", style="cyan")
            table.add_column("Agent", style="green")
            table.add_column("Tokens", style="yellow")
            table.add_column("Created", style="dim")

            for c in chunks:
                table.add_row(
                    c.chunk_type,
                    c.agent_id,
                    str(c.token_count),
                    c.created_at.strftime("%H:%M:%S"),
                )

            console.print(table)

            total_tokens = await context_manager.get_token_usage(sid)
            console.print(f"\n[dim]Total tokens: {total_tokens}[/dim]")
        finally:
            await db.close()

    _run(_context())


@app.command(name="skills")
def skills_list():
    """List all loaded skills."""
    from ah.skills.registry import skill_registry
    skill_registry.load_all()
    all_skills = skill_registry.list_skills()
    if not all_skills:
        console.print("[yellow]No skills found.[/yellow]")
        return
    table = Table(title=f"Skills ({len(all_skills)} loaded)")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Triggers", style="dim")
    for s in all_skills:
        table.add_row(s.name, s.description[:60], ", ".join(s.triggers[:3]))
    console.print(table)


@app.command()
def doctor():
    """Check AgentHarness dependencies and configuration."""
    console.print("[bold]AgentHarness Doctor[/bold]\n")

    # Python version
    import sys
    console.print(f"  Python: {sys.version.split()[0]} {'✓' if sys.version_info >= (3, 11) else '✗ (need 3.11+)'}")

    # Dependencies
    deps = ["typer", "rich", "asyncpg", "httpx", "msgpack", "prompt_toolkit"]
    for dep in deps:
        try:
            __import__(dep)
            console.print(f"  {dep}: [green]ok[/green]")
        except ImportError:
            console.print(f"  {dep}: [red]missing[/red]")

    # Database
    async def _check_db():
        try:
            await db.connect()
            version = await db.fetchval("SELECT version()")
            console.print(f"  PostgreSQL: [green]connected[/green] ({version.split(',')[0]})")
        except Exception as e:
            console.print(f"  PostgreSQL: [red]failed[/red] ({e})")
        finally:
            await db.close()

    _run(_check_db())

    # Environment
    import os
    if os.environ.get("OPENROUTER_API_KEY"):
        console.print("  OPENROUTER_API_KEY: [green]set[/green]")
    else:
        console.print("  OPENROUTER_API_KEY: [yellow]not set[/yellow]")

    if os.environ.get("DATABASE_URL"):
        console.print("  DATABASE_URL: [green]set[/green]")
    else:
        console.print("  DATABASE_URL: [yellow]not set (using default)[/yellow]")

    console.print()
    console.print("[dim]Run `ah chat \"hello\"` to test the agent.[/dim]")


@app.command()
def init(
    db_url: Optional[str] = typer.Option(None, "--db-url", help="PostgreSQL connection URL"),
):
    """Initialize AgentHarness — set up database schema."""

    async def _init():
        if db_url:
            db.dsn = db_url

        console.print("[bold]Initializing AgentHarness...[/bold]")
        console.print(f"  Database: {db.dsn.split('@')[-1]}")

        try:
            await db.connect()
            await db.initialize_schema()
            console.print("  Schema: [green]created[/green]")
        except Exception as e:
            console.print(f"  Schema: [red]failed[/red] ({e})")
            raise typer.Exit(1)
        finally:
            await db.close()

        console.print()
        console.print("[green]AgentHarness initialized![/green]")
        console.print("[dim]Run `ah chat \"hello\"` to start.[/dim]")

    _run(_init())


@app.command()
def version():
    """Show AgentHarness version."""
    console.print(f"AgentHarness v{__version__}")


# ─── Config commands ─────────────────────────────────────────────────────────

@app.command(name="config")
def config_show():
    """Show current configuration."""
    table = Table(title="Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="white")

    config_dict = config.to_dict()
    for key, value in config_dict.items():
        table.add_row(key, str(value))

    console.print(table)


@app.command(name="config-set")
def config_set(
    key: str = typer.Argument(..., help="Config key to set"),
    value: str = typer.Argument(..., help="New value"),
    persist: bool = typer.Option(False, "--persist", "-p", help="Save to config file"),
):
    """Set a configuration value."""
    if key not in config.to_dict():
        console.print(f"[red]Unknown config key: {key}[/red]")
        console.print(f"[dim]Available keys: {', '.join(config.to_dict().keys())}[/dim]")
        raise typer.Exit(1)

    config.set(key, value, persist=persist)
    console.print(f"[green]Set {key} = {value}[/green]")
    if persist:
        console.print("[dim]Config saved to file.[/dim]")


# ─── Memory commands ────────────────────────────────────────────────────────

@app.command(name="memory-list")
def memory_list(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of memories to show"),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by category"),
):
    """List all memories."""

    async def _memory_list():
        await db.connect()
        try:
            from ah.memory.store import memory_store
            memories = await memory_store.search(category=category, limit=limit)

            if not memories:
                console.print("[yellow]No memories found.[/yellow]")
                return

            table = Table(title=f"Memories ({len(memories)})")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Category", style="green")
            table.add_column("Importance", style="yellow")
            table.add_column("Content", style="white")

            for m in memories:
                table.add_row(
                    str(m.id)[:8],
                    m.category,
                    f"{m.importance:.2f}",
                    m.content[:60],
                )

            console.print(table)
        finally:
            await db.close()

    _run(_memory_list())


@app.command(name="memory-search")
def memory_search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(5, "--limit", "-n", help="Number of results"),
):
    """Search memories by relevance."""

    async def _memory_search():
        await db.connect()
        try:
            from ah.memory.retriever import MemoryRetriever
            retriever = MemoryRetriever(top_k=limit)
            results = await retriever.retrieve(query=query)

            if not results:
                console.print(f"[yellow]No memories found for '{query}'[/yellow]")
                return

            console.print(f"[bold]Search results for '{query}'[/bold]")
            for i, rm in enumerate(results, 1):
                m = rm.memory
                console.print(f"\n  [{i}] [cyan]({m.category}, importance={m.importance:.2f}, score={rm.score:.3f})[/cyan]")
                console.print(f"      {m.content[:200]}")
            console.print()
        finally:
            await db.close()

    _run(_memory_search())


@app.command(name="memory-forget")
def memory_forget(
    memory_id: str = typer.Argument(..., help="Memory ID to delete"),
):
    """Delete a memory by ID."""

    async def _memory_forget():
        await db.connect()
        try:
            from ah.memory.store import memory_store
            mid = uuid.UUID(memory_id)
            deleted = await memory_store.delete(mid)
            if deleted:
                console.print(f"[green]Memory {memory_id} deleted.[/green]")
            else:
                console.print(f"[yellow]Memory {memory_id} not found.[/yellow]")
        finally:
            await db.close()

    _run(_memory_forget())


if __name__ == "__main__":
    app()
