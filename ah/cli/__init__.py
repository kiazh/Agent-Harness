"""AgentHarness CLI — `ah` command."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
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

            # Check config
            from ah.core.config import config
            if config.get("openrouter_api_key"):
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


@app.command(name="sessions-search")
def sessions_search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results"),
):
    """Full-text search over session titles."""

    async def _search():
        await db.connect()
        try:
            sessions = await session_manager.search(query, limit=limit)

            if not sessions:
                console.print(f"[yellow]No sessions found for '{query}'[/yellow]")
                return

            table = Table(title=f"Search results for '{query}'")
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

    _run(_search())


@app.command()
def export(
    filename: str = typer.Argument(..., help="Output markdown filename"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Session ID to export (default: last active)"),
):
    """Export a conversation as markdown."""

    async def _export():
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
            if not session:
                console.print(f"[red]Session {session_id} not found[/red]")
                raise typer.Exit(1)

            chunks = await context_manager.get_chunks(sid, limit=1000)

            lines = [
                f"# Session: {session.title or '(untitled)'}",
                "",
                f"**ID:** {session.id}",
                f"**Status:** {session.status}",
                f"**Agent:** {session.agent_id}",
                f"**Goal:** {session.goal or '(none)'}",
                f"**Created:** {session.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"**Last Activity:** {session.last_activity.strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "## Conversation",
                "",
            ]

            for chunk in reversed(chunks):
                if chunk.chunk_type == "user_message":
                    content = chunk.payload.get("content", "")
                    lines.append(f"### User")
                    lines.append("")
                    lines.append(content)
                    lines.append("")
                elif chunk.chunk_type == "assistant_message":
                    content = chunk.payload.get("content", "")
                    lines.append(f"### Assistant")
                    lines.append("")
                    lines.append(content)
                    lines.append("")
                elif chunk.chunk_type == "tool_call":
                    tool = chunk.payload.get("tool", "unknown")
                    args = chunk.payload.get("args", {})
                    result_preview = chunk.payload.get("result_preview", "")
                    lines.append(f"**Tool:** `{tool}({args})`")
                    if result_preview:
                        lines.append(f"**Result:** {result_preview[:200]}")
                    lines.append("")

            output_path = Path(filename)
            output_path.write_text("\n".join(lines), encoding="utf-8")
            console.print(f"[green]Exported session {session.id} to {filename}[/green]")
        finally:
            await db.close()

    _run(_export())


@app.command()
def fork(
    session_id: str = typer.Argument(..., help="Session ID to fork"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Title for the forked session"),
):
    """Fork a session — create a new session with copied state and context."""

    async def _fork():
        await db.connect()
        try:
            sid = uuid.UUID(session_id)
            new_session = await session_manager.fork(sid, title=title)
            console.print(f"[green]Forked session {session_id} → {new_session.id}[/green]")
            console.print(f"[dim]Title: {new_session.title or '(untitled)'}[/dim]")
        finally:
            await db.close()

    _run(_fork())


@app.command()
def delete(
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Permanently delete a session and all its context chunks."""

    async def _delete():
        await db.connect()
        try:
            sid = uuid.UUID(session_id)
            session = await session_manager.get(sid)
            if not session:
                console.print(f"[red]Session {session_id} not found[/red]")
                raise typer.Exit(1)

            if not force:
                console.print(f"[yellow]About to delete session {session_id} ({session.title or 'untitled'})[/yellow]")
                console.print("[yellow]This will permanently remove all context chunks.[/yellow]")
                confirm = typer.confirm("Are you sure?")
                if not confirm:
                    console.print("[dim]Cancelled.[/dim]")
                    return

            deleted = await session_manager.delete(sid)
            if deleted:
                console.print(f"[green]Session {session_id} deleted.[/green]")
            else:
                console.print(f"[red]Failed to delete session {session_id}.[/red]")
                raise typer.Exit(1)
        finally:
            await db.close()

    _run(_delete())


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


@app.command()
def compress(
    session_id: str = typer.Argument(None, help="Session ID to compress (default: last active)"),
):
    """Compress context for a session. Reduces token usage while preserving key information."""

    async def _compress():
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
            if not session:
                console.print(f"[red]Session {session_id} not found[/red]")
                raise typer.Exit(1)

            from ah.core.compression import ContextCompressor, CompressionConfig

            chunks = await context_manager.get_chunks(sid, limit=1000)
            if not chunks:
                console.print("[yellow]No context chunks to compress.[/yellow]")
                return

            total_tokens = await context_manager.get_token_usage(sid)
            console.print(f"[dim]Session: {session.id}[/dim]")
            console.print(f"[dim]Current context: {len(chunks)} chunks, {total_tokens} tokens[/dim]")

            comp_config = CompressionConfig(
                enabled=config.get("compression_enabled"),
                threshold=config.get("compression_threshold"),
                target_ratio=config.get("compression_target_ratio"),
                preserve_recent=config.get("compression_preserve_recent"),
                llm_summarize=config.get("compression_llm_summarize"),
            )

            compressor = ContextCompressor(config=comp_config)

            # Try to get LLM provider for summarization
            llm_provider = None
            if comp_config.llm_summarize:
                try:
                    llm_provider = get_provider(provider=config.get("provider"), model=config.get("model"))
                except Exception:
                    llm_provider = None

            result = compressor.compress(
                chunks=chunks,
                session_id=sid,
                agent_id=session.agent_id,
                llm_provider=llm_provider,
            )

            if result.original_count == 0:
                console.print("[yellow]Nothing to compress (not enough chunks).[/yellow]")
                return

            # Delete old chunks and store compressed ones
            await context_manager.delete_chunks(sid)

            for chunk in result.compressed_chunks:
                await context_manager.add_chunk(
                    session_id=chunk.session_id,
                    agent_id=chunk.agent_id,
                    chunk_type=chunk.chunk_type,
                    payload=chunk.payload,
                    token_count=chunk.token_count,
                )

            console.print(
                f"[green]Context compressed:[/green] "
                f"{result.original_count} chunks → {len(result.compressed_chunks)} chunks, "
                f"{result.original_tokens} → {result.compressed_tokens} tokens "
                f"({result.compression_ratio:.1%} ratio, method: {result.method})"
            )

        finally:
            await db.close()

    _run(_compress())


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
    table.add_column("Uses", style="yellow")
    table.add_column("Views", style="green")
    table.add_column("Last Activity", style="dim")
    for s in all_skills:
        last_act = s.last_activity_at.strftime("%Y-%m-%d %H:%M") if s.last_activity_at else "never"
        table.add_row(s.name, s.description[:60], ", ".join(s.triggers[:3]), str(s.use_count), str(s.view_count), last_act)
    console.print(table)


@app.command(name="learn")
def learn(
    source: str = typer.Argument(..., help="Source to learn from (file path, URL, or skill name)"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Name for the new skill"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Description for the new skill"),
    triggers: Optional[str] = typer.Option(None, "--triggers", "-t", help="Comma-separated trigger words"),
):
    """Learn a new skill from a source (file, URL, or existing skill).

    Extracts reusable knowledge from the source and creates a new skill.
    """
    from ah.skills.registry import skill_registry
    skill_registry.load_all()

    # Determine source type
    source_path = Path(source)
    if source_path.exists() and source_path.is_file():
        # Learn from file
        content = source_path.read_text(encoding="utf-8")
        skill_name = name or source_path.stem
        skill_description = description or f"Skill learned from {source_path.name}"
        trigger_list = [t.strip() for t in triggers.split(",")] if triggers else []
    elif source.startswith("http://") or source.startswith("https://"):
        # Learn from URL
        import httpx
        try:
            resp = httpx.get(source, timeout=30)
            resp.raise_for_status()
            content = resp.text
        except Exception as e:
            console.print(f"[red]Failed to fetch URL:[/red] {e}")
            raise typer.Exit(1)
        skill_name = name or source.split("/")[-1].split(".")[0]
        skill_description = description or f"Skill learned from {source}"
        trigger_list = [t.strip() for t in triggers.split(",")] if triggers else []
    else:
        # Try to find existing skill
        existing = skill_registry.get(source)
        if existing:
            content = existing.content
            skill_name = name or f"{existing.name}-learned"
            skill_description = description or f"Skill derived from {existing.name}"
            trigger_list = [t.strip() for t in triggers.split(",")] if triggers else existing.triggers[:]
        else:
            console.print(f"[red]Source not found:[/red] {source}")
            console.print("[dim]Provide a file path, URL, or existing skill name.[/dim]")
            raise typer.Exit(1)

    # Create the skill
    skill = skill_registry.create_skill(
        name=skill_name,
        description=skill_description,
        content=content,
        triggers=trigger_list,
        source=source,
        source_type="learned",
    )
    console.print(f"[green]Skill '{skill.name}' created successfully![/green]")
    console.print(f"  Description: {skill.description}")
    console.print(f"  Triggers: {', '.join(skill.triggers)}")
    console.print(f"  Source: {skill.source}")


@app.command(name="curator")
def curator(
    action: str = typer.Argument("report", help="Action: report, archive, cleanup, stale, unused, top"),
    days: int = typer.Option(30, "--days", "-d", help="Days threshold for stale skills"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results to show"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without doing it"),
):
    """Run skill curator — background maintenance for skills.

    Actions:
        report   — Show skill system health report
        archive  — Archive stale skills (not used in N days)
        cleanup  — Remove unused skills (never used)
        stale    — List stale skills
        unused   — List never-used skills
        top      — Show most frequently used skills
    """
    from ah.skills.registry import skill_registry, SkillCurator
    skill_registry.load_all()
    curator = SkillCurator(skill_registry)

    if action == "report":
        report = curator.get_health_report()
        console.print("[bold]Skill System Health Report[/bold]\n")
        console.print(f"  Total skills: {report['total_skills']}")
        console.print(f"  Enabled: {report['enabled']}")
        console.print(f"  Disabled: {report['disabled']}")
        console.print(f"  Never used: {report['never_used']}")
        console.print(f"  Total uses: {report['total_uses']}")
        console.print(f"  Total views: {report['total_views']}")
        console.print(f"  Stale skills: {report['stale_skills']}")
        if report['top_skills']:
            console.print("\n[bold]Top skills:[/bold]")
            for s in report['top_skills']:
                console.print(f"  {s['name']}: {s['use_count']} uses")

    elif action == "archive":
        archived = curator.archive_stale(days=days)
        if archived:
            console.print(f"[green]Archived {len(archived)} stale skills:[/green]")
            for name in archived:
                console.print(f"  - {name}")
        else:
            console.print("[yellow]No stale skills found.[/yellow]")

    elif action == "cleanup":
        removed = curator.cleanup_unused(dry_run=dry_run)
        if removed:
            prefix = "[yellow]Would remove[/yellow]" if dry_run else "[green]Removed[/green]"
            console.print(f"{prefix} {len(removed)} unused skills:")
            for name in removed:
                console.print(f"  - {name}")
        else:
            console.print("[yellow]No unused skills found.[/yellow]")

    elif action == "stale":
        stale = curator.get_stale_skills(days=days)
        if stale:
            console.print(f"[bold]Stale skills ({len(stale)}):[/bold]")
            for s in stale:
                last = s.last_activity_at.strftime("%Y-%m-%d") if s.last_activity_at else "never"
                console.print(f"  {s.name} (last activity: {last})")
        else:
            console.print("[yellow]No stale skills found.[/yellow]")

    elif action == "unused":
        unused = curator.get_unused_skills()
        if unused:
            console.print(f"[bold]Unused skills ({len(unused)}):[/bold]")
            for s in unused:
                console.print(f"  {s.name} (created: {s.created_at.strftime('%Y-%m-%d')})")
        else:
            console.print("[yellow]No unused skills found.[/yellow]")

    elif action == "top":
        top = curator.get_top_skills(limit=limit)
        if top:
            console.print(f"[bold]Top {len(top)} skills:[/bold]")
            for i, s in enumerate(top, 1):
                console.print(f"  {i}. {s.name} — {s.use_count} uses, {s.view_count} views")
        else:
            console.print("[yellow]No skill usage data.[/yellow]")

    else:
        console.print(f"[red]Unknown action:[/red] {action}")
        console.print("[dim]Available: report, archive, cleanup, stale, unused, top[/dim]")
        raise typer.Exit(1)


@app.command(name="hub")
def hub(
    action: str = typer.Argument("list", help="Action: list, search, publish, install, telemetry"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Skill name"),
    author: Optional[str] = typer.Option(None, "--author", "-a", help="Author name for publish"),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated tags for publish"),
):
    """Skill hub — community-curated skill sharing.

    Actions:
        list      — List all skills in the hub
        search    — Search hub skills
        publish   — Publish a local skill to the hub
        install   — Install a skill from the hub
        telemetry — Show hub skill telemetry
    """
    from ah.skills.registry import skill_registry, SkillHub
    skill_registry.load_all()
    hub = SkillHub(skill_registry)

    if action == "list":
        skills = hub.list_hub()
        if not skills:
            console.print("[yellow]No skills in hub.[/yellow]")
            return
        table = Table(title=f"Skill Hub ({len(skills)} skills)")
        table.add_column("Name", style="cyan")
        table.add_column("Description", style="white")
        table.add_column("Author", style="dim")
        table.add_column("Tags", style="dim")
        table.add_column("Version", style="dim")
        for s in skills:
            table.add_row(
                s.get("name", ""),
                s.get("description", "")[:60],
                s.get("author", ""),
                ", ".join(s.get("tags", [])[:3]),
                s.get("version", "1.0.0"),
            )
        console.print(table)

    elif action == "search":
        if not query:
            console.print("[red]Query required for search.[/red]")
            raise typer.Exit(1)
        results = hub.search(query)
        if not results:
            console.print(f"[yellow]No skills found for '{query}'[/yellow]")
            return
        console.print(f"[bold]Search results for '{query}' ({len(results)}):[/bold]")
        for s in results:
            console.print(f"\n  [cyan]{s['name']}[/cyan] — {s.get('description', '')[:80]}")
            console.print(f"    Author: {s.get('author', 'unknown')} | Tags: {', '.join(s.get('tags', []))}")

    elif action == "publish":
        if not name:
            console.print("[red]Skill name required for publish.[/red]")
            raise typer.Exit(1)
        tag_list = [t.strip() for t in tags.split(",")] if tags else []
        try:
            entry = hub.publish(name, author=author or "", tags=tag_list)
            console.print(f"[green]Published '{entry['name']}' to hub.[/green]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    elif action == "install":
        if not name:
            console.print("[red]Skill name required for install.[/red]")
            raise typer.Exit(1)
        try:
            skill = hub.install(name)
            console.print(f"[green]Installed '{skill.name}' from hub.[/green]")
            console.print(f"  Description: {skill.description}")
            console.print(f"  Triggers: {', '.join(skill.triggers)}")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    elif action == "telemetry":
        telemetry = hub.get_hub_telemetry()
        if not telemetry:
            console.print("[yellow]No hub telemetry available.[/yellow]")
            return
        table = Table(title="Hub Telemetry")
        table.add_column("Name", style="cyan")
        table.add_column("Uses", style="yellow")
        table.add_column("Views", style="green")
        table.add_column("Published", style="dim")
        for t in telemetry:
            table.add_row(
                t.get("name", ""),
                str(t.get("use_count", 0)),
                str(t.get("view_count", 0)),
                t.get("published_at", "")[:10],
            )
        console.print(table)

    else:
        console.print(f"[red]Unknown action:[/red] {action}")
        console.print("[dim]Available: list, search, publish, install, telemetry[/dim]")
        raise typer.Exit(1)


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
    from ah.core.config import config
    if config.get("openrouter_api_key"):
        console.print("  OPENROUTER_API_KEY: [green]set[/green]")
    else:
        console.print("  OPENROUTER_API_KEY: [yellow]not set[/yellow]")

    if config.get("database_url"):
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


# ─── Memory Approval Commands ──────────────────────────────────────────────

@app.command(name="memory-pending")
def memory_pending(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of pending memories to show"),
):
    """List pending memories awaiting approval."""

    async def _memory_pending():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate, ApprovalStatus
            pending = await memory_approval_gate.list_pending(
                status=ApprovalStatus.PENDING,
                limit=limit,
            )

            if not pending:
                console.print("[yellow]No pending memories.[/yellow]")
                return

            table = Table(title=f"Pending Memories ({len(pending)})")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Category", style="green")
            table.add_column("Importance", style="yellow")
            table.add_column("Content", style="white")
            table.add_column("Redactions", style="red")

            for p in pending:
                table.add_row(
                    str(p.id)[:8],
                    p.category,
                    f"{p.importance:.2f}",
                    p.content[:60],
                    ", ".join(p.redactions) if p.redactions else "—",
                )

            console.print(table)
        finally:
            await db.close()

    _run(_memory_pending())


@app.command(name="memory-approve")
def memory_approve(
    pending_id: str = typer.Argument(..., help="Pending memory ID to approve"),
    note: str = typer.Option("", "--note", "-n", help="Review note"),
):
    """Approve a pending memory."""

    async def _memory_approve():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate
            pid = uuid.UUID(pending_id)
            memory = await memory_approval_gate.approve(pid, review_note=note)
            if memory:
                console.print(f"[green]Pending memory {pending_id} approved → memory {memory.id}[/green]")
            else:
                console.print(f"[yellow]Pending memory {pending_id} not found or already reviewed.[/yellow]")
        finally:
            await db.close()

    _run(_memory_approve())


@app.command(name="memory-reject")
def memory_reject(
    pending_id: str = typer.Argument(..., help="Pending memory ID to reject"),
    note: str = typer.Option("", "--note", "-n", help="Review note"),
):
    """Reject a pending memory."""

    async def _memory_reject():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate
            pid = uuid.UUID(pending_id)
            rejected = await memory_approval_gate.reject(pid, review_note=note)
            if rejected:
                console.print(f"[green]Pending memory {pending_id} rejected.[/green]")
            else:
                console.print(f"[yellow]Pending memory {pending_id} not found or already reviewed.[/yellow]")
        finally:
            await db.close()

    _run(_memory_reject())


@app.command(name="memory-approve-all")
def memory_approve_all(
    agent_id: Optional[str] = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
):
    """Approve all pending memories."""

    async def _memory_approve_all():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate
            count = await memory_approval_gate.approve_all(agent_id=agent_id)
            console.print(f"[green]Approved {count} pending memories.[/green]")
        finally:
            await db.close()

    _run(_memory_approve_all())


@app.command(name="memory-reject-all")
def memory_reject_all(
    agent_id: Optional[str] = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
    note: str = typer.Option("", "--note", "-n", help="Review note"),
):
    """Reject all pending memories."""

    async def _memory_reject_all():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate
            count = await memory_approval_gate.reject_all(agent_id=agent_id, review_note=note)
            console.print(f"[green]Rejected {count} pending memories.[/green]")
        finally:
            await db.close()

    _run(_memory_reject_all())


@app.command(name="memory-stats")
def memory_stats():
    """Show memory approval statistics."""

    async def _memory_stats():
        await db.connect()
        try:
            from ah.memory.approval import memory_approval_gate
            stats = await memory_approval_gate.get_stats()

            table = Table(title="Memory Approval Stats")
            table.add_column("Status", style="cyan")
            table.add_column("Count", style="white")

            for status, count in stats.items():
                table.add_row(status, str(count))

            console.print(table)
        finally:
            await db.close()

    _run(_memory_stats())


# ─── User Profile Commands ──────────────────────────────────────────────────

@app.command(name="user-profile")
def user_profile_cmd(
    user_id: str = typer.Argument(..., help="User ID to look up or create"),
    display_name: str = typer.Option("", "--name", "-n", help="Display name for new profiles"),
):
    """Show or create a user profile."""

    async def _user_profile():
        await db.connect()
        try:
            from ah.memory.user_profile import user_profile_store
            profile = await user_profile_store.get_or_create(
                user_id=user_id,
                display_name=display_name,
            )

            console.print(f"[bold]User Profile[/bold]")
            console.print(f"  ID: {profile.id}")
            console.print(f"  User ID: {profile.user_id}")
            console.print(f"  Display Name: {profile.display_name or '(none)'}")
            console.print(f"  Interactions: {profile.interaction_count}")
            console.print(f"  Created: {profile.created_at.strftime('%Y-%m-%d %H:%M')}")

            if profile.preferences:
                console.print(f"\n  [bold]Preferences:[/bold]")
                for key, value in profile.preferences.items():
                    console.print(f"    {key}: {value}")

            if profile.topics:
                console.print(f"\n  [bold]Top Topics:[/bold]")
                for topic, count in profile.get_top_topics():
                    console.print(f"    {topic}: {count}")

            if profile.last_topics:
                console.print(f"\n  [bold]Recent Topics:[/bold]")
                console.print(f"    {', '.join(profile.last_topics[:5])}")
        finally:
            await db.close()

    _run(_user_profile())


@app.command(name="user-profile-update")
def user_profile_update(
    user_id: str = typer.Argument(..., help="User ID"),
    key: str = typer.Argument(..., help="Preference key"),
    value: str = typer.Argument(..., help="Preference value"),
):
    """Update a user profile preference."""

    async def _user_profile_update():
        await db.connect()
        try:
            from ah.memory.user_profile import user_profile_store
            profile = await user_profile_store.get_by_user_id(user_id)
            if not profile:
                console.print(f"[red]User profile not found for '{user_id}'[/red]")
                raise typer.Exit(1)

            profile.set_preference(key, value)
            updated = await user_profile_store.update_preferences(
                profile.id, profile.preferences
            )
            if updated:
                console.print(f"[green]Updated {key} = {value} for {user_id}[/green]")
            else:
                console.print(f"[red]Failed to update profile[/red]")
                raise typer.Exit(1)
        finally:
            await db.close()

    _run(_user_profile_update())


@app.command(name="user-profile-list")
def user_profile_list(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of profiles to show"),
):
    """List all user profiles."""

    async def _user_profile_list():
        await db.connect()
        try:
            from ah.memory.user_profile import user_profile_store
            profiles = await user_profile_store.list_all(limit=limit)

            if not profiles:
                console.print("[yellow]No user profiles found.[/yellow]")
                return

            table = Table(title=f"User Profiles ({len(profiles)})")
            table.add_column("User ID", style="cyan")
            table.add_column("Display Name", style="white")
            table.add_column("Interactions", style="green")
            table.add_column("Topics", style="dim")

            for p in profiles:
                table.add_row(
                    p.user_id,
                    p.display_name or "—",
                    str(p.interaction_count),
                    str(len(p.topics)),
                )

            console.print(table)
        finally:
            await db.close()

    _run(_user_profile_list())


if __name__ == "__main__":
    app()
