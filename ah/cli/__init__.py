"""AgentHarness CLI — the ``ah`` command.

``ah`` with no subcommand (also ``ah repl`` and ``ah chat -i``) launches the
TypeScript terminal UI in ``ui/``, which drives the agent through the Python
gateway (``python -m ah.gateway``). Every other subcommand is a one-shot admin
command rendered with Rich.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import typer
from rich.markup import escape
from rich.table import Table

import ah.tools  # noqa: F401 — registers all built-in tools
from ah import __version__, services
from ah.cli import output
from ah.cli.launcher import launch_ui
from ah.cli.output import console
from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.provider import get_provider
from ah.core.session import session_manager
from ah.db.connection import db

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="ah",
    help="AgentHarness — self-hosted AI agent framework. Run `ah` to open the interactive UI.",
    invoke_without_command=True,
)


@app.callback()
def main(ctx: typer.Context) -> None:
    """Open the interactive UI when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        raise typer.Exit(launch_ui())


def _run(coro):
    """Run an async coroutine from a sync Typer command."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _parse_uuid(s: str) -> uuid.UUID | None:
    """Parse a UUID string, returning None if invalid."""
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        return None


async def _resolve_session(session_id: str | None):
    """Return the named session, or the last active one; exit with a message otherwise."""
    if session_id:
        sid = _parse_uuid(session_id)
        if sid is None:
            output.error(f"Invalid session ID: {session_id}")
            raise typer.Exit(1)
        session = await session_manager.get(sid)
        if session is None:
            output.error(f"Session {session_id} not found", "Use `ah sessions` to list sessions.")
            raise typer.Exit(1)
        return session
    session = await session_manager.get_last_active()
    if session is None:
        output.error("No active sessions.", "Start one with `ah`.")
        raise typer.Exit(1)
    return session


@app.command()
def chat(
    message: str = typer.Argument(None, help="Message to send to the agent"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="Continue last session"),
    session_id: str | None = typer.Option(None, "--session", "-s", help="Resume specific session"),
    model: str = typer.Option(
        None, "--model", "-m", help="Model to use (e.g., anthropic/claude-3.5-sonnet)"
    ),
    provider: str = typer.Option(
        "openrouter", "--provider", "-p", help="LLM provider (openrouter, ollama)"
    ),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show tool calls"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Open the interactive UI"),
):
    """Send one message to the agent (or open the UI with -i)."""
    if interactive:
        raise typer.Exit(launch_ui(model=model, provider=provider, session_id=session_id))

    # Guard before any I/O: message is required, and slicing it for the
    # session title (message[:50]) would raise TypeError on None.
    if not message:
        console.print('[yellow]No message provided. Use: ah chat "your message"[/yellow]')
        raise typer.Exit(1)

    async def _chat():
        await db.connect()
        try:
            if continue_:
                session = await session_manager.get_last_active()
                if not session:
                    output.error(
                        "No active session to continue.", "Use `ah sessions` to list sessions."
                    )
                    raise typer.Exit(1)
                output.muted(f"Continuing session: {session.id}")
            elif session_id:
                sid = _parse_uuid(session_id)
                if sid is None:
                    output.error(f"Invalid session ID: {session_id}")
                    raise typer.Exit(1)
                session = await session_manager.get(sid)
                if not session:
                    output.error(
                        f"Session {session_id} not found", "Use `ah sessions` to list sessions."
                    )
                    raise typer.Exit(1)
                output.muted(f"Resuming session: {session.id}")
            else:
                session = await session_manager.create(title=message[:50], goal=message[:100])
                output.muted(f"New session: {session.id}")

            try:
                llm = get_provider(provider=provider, model=model)
            except ValueError as e:
                output.error(
                    f"Provider error: {e}", "Check your provider configuration with `ah config`."
                )
                raise typer.Exit(1) from None

            agent = ReActAgent(provider=llm)
            if verbose:
                output.muted(f"Model: {llm.model} ({provider})")
            console.print()

            final = None
            async for event in agent.run_stream(session.id, message, verbose=False):
                if event.type == "text":
                    console.out(event.content, end="", highlight=False)
                elif event.type == "tool_call" and verbose:
                    console.print(f"\n[yellow]→ {escape(event.tool_name)}[/yellow]")
                elif event.type == "tool_result" and verbose:
                    preview = str(event.tool_result)[:120].replace("\n", " ")
                    console.print(f"[dim]  ← {escape(preview)}[/dim]")
                elif event.type == "done":
                    final = event.response
            console.print()

            if final is not None and verbose:
                output.muted(
                    f"Iterations: {final.iterations} | Tool calls: {len(final.tool_calls)} "
                    f"| Tokens: {final.tokens_used}"
                )
            output.muted(f"Session ID: {session.id}")
        finally:
            await db.close()

    _run(_chat())


@app.command()
def repl(
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(None, "--provider", "-p", help="LLM provider"),
    session_id: str | None = typer.Option(None, "--session", "-s", help="Resume specific session"),
):
    """Open the interactive UI (same as running `ah` with no arguments)."""
    raise typer.Exit(launch_ui(model=model, provider=provider, session_id=session_id))


@app.command()
def status():
    """Show AgentHarness status: database, sessions, tools and API key."""

    async def _status():
        try:
            try:
                await db.connect()
                version = await db.fetchval("SELECT version()")
            except Exception as e:
                output.status_line("PostgreSQL", f"connection failed ({e})", "error")
                raise typer.Exit(1) from None
            output.status_line("PostgreSQL", f"connected ({version.split(',')[0]})", "success")
            output.status_line("Sessions", str(await db.fetchval("SELECT COUNT(*) FROM sessions")))
            output.status_line(
                "Context chunks", str(await db.fetchval("SELECT COUNT(*) FROM context_chunks"))
            )

            from ah.tools.base import registry

            tools = registry.list_tools()
            output.status_line("Tools", f"{len(tools)} registered")
            for t in tools:
                console.print(f"    [dim]•[/dim] {t}")

            if config.get("openrouter_api_key"):
                output.status_line("OpenRouter API key", "set", "success")
            else:
                output.status_line("OpenRouter API key", "not set", "warning")
            console.print()
            output.muted("Run `ah` to open the interactive UI.")
        finally:
            await db.close()

    _run(_status())


@app.command(name="sessions")
def list_sessions(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of sessions to show"),
    status_filter: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status (active, idle, archived)"
    ),
):
    """List recent sessions."""

    async def _sessions():
        await db.connect()
        try:
            sessions = await session_manager.list_sessions(status=status_filter, limit=limit)

            if not sessions:
                console.print("[yellow]No sessions found.[/yellow]")
                return

            output.sessions_table(sessions)
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

            output.sessions_table(sessions)
        finally:
            await db.close()

    _run(_search())


@app.command()
def export(
    filename: str = typer.Argument(..., help="Output markdown filename"),
    session_id: str | None = typer.Option(
        None, "--session", "-s", help="Session ID to export (default: last active)"
    ),
):
    """Export a conversation as markdown."""

    async def _export():
        await db.connect()
        try:
            session = await _resolve_session(session_id)
            text = await services.export_markdown(session)
            from ah.tools.file import _resolve_path

            try:
                output_path = _resolve_path(filename)
            except ValueError as e:
                output.error(f"Path traversal blocked: {e}")
                raise typer.Exit(1) from None
            output_path.write_text(text, encoding="utf-8")
            output.success(f"Exported session {session.id} to {filename}")
        finally:
            await db.close()

    _run(_export())


@app.command()
def fork(
    session_id: str = typer.Argument(..., help="Session ID to fork"),
    title: str | None = typer.Option(None, "--title", "-t", help="Title for the forked session"),
):
    """Fork a session — create a new session with copied state and context."""

    async def _fork():
        await db.connect()
        try:
            sid = _parse_uuid(session_id)
            if sid is None:
                console.print(f"[red]Invalid session ID: {session_id}[/red]")
                raise typer.Exit(1)
            new_session = await session_manager.fork(sid, title=title)
            output.success(f"Forked session {session_id} → {new_session.id}")
            output.muted(f"Title: {new_session.title or '(untitled)'}")
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
            sid = _parse_uuid(session_id)
            if sid is None:
                console.print(f"[red]Invalid session ID: {session_id}[/red]")
                raise typer.Exit(1)
            session = await session_manager.get(sid)
            if not session:
                console.print(f"[red]Session {session_id} not found[/red]")
                raise typer.Exit(1)

            if not force:
                console.print(
                    f"[yellow]About to delete session {session_id} ({session.title or 'untitled'})[/yellow]"
                )
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
                sid = _parse_uuid(session_id)
                if sid is None:
                    console.print(f"[red]Invalid session ID: {session_id}[/red]")
                    raise typer.Exit(1)
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

            output.context_table(chunks, sid)

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
            session = await _resolve_session(session_id)
            total_tokens = await context_manager.get_token_usage(session.id)
            output.muted(f"Session: {session.id} ({total_tokens} tokens)")
            with output.spinner("Compressing context..."):
                result = await services.compress_session(session)
            if result is None:
                console.print("[yellow]Nothing to compress (not enough chunks).[/yellow]")
                return
            output.success(
                f"Context compressed: "
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
    output.skills_table(all_skills)


@app.command(name="learn")
def learn(
    source: str = typer.Argument(..., help="Source to learn from (file path, URL, or skill name)"),
    name: str | None = typer.Option(None, "--name", "-n", help="Name for the new skill"),
    description: str | None = typer.Option(
        None, "--description", "-d", help="Description for the new skill"
    ),
    triggers: str | None = typer.Option(
        None, "--triggers", "-t", help="Comma-separated trigger words"
    ),
):
    """Learn a new skill from a source (file, URL, or existing skill)."""
    trigger_list = [t.strip() for t in triggers.split(",") if t.strip()] if triggers else None
    try:
        skill = services.learn_skill(
            source, name=name, description=description, triggers=trigger_list
        )
    except services.ServiceError as e:
        output.error(str(e))
        raise typer.Exit(1) from None
    console.print(f"[green]Skill '{skill.name}' created successfully![/green]")
    console.print(f"  Description: {skill.description}")
    console.print(f"  Triggers: {', '.join(skill.triggers)}")
    console.print(f"  Source: {skill.source}")


@app.command(name="curator")
def curator(
    action: str = typer.Argument(
        "report", help="Action: report, archive, cleanup, stale, unused, top"
    ),
    days: int = typer.Option(30, "--days", "-d", help="Days threshold for stale skills"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results to show"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without doing it"
    ),
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
    from ah.skills.registry import SkillCurator, skill_registry

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
        if report["top_skills"]:
            console.print("\n[bold]Top skills:[/bold]")
            for s in report["top_skills"]:
                console.print(f"  {s['name']}: {s['usage_count']} uses")

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
                console.print(f"  {i}. {s.name} — {s.usage_count} uses, {s.view_count} views")
        else:
            console.print("[yellow]No skill usage data.[/yellow]")

    else:
        console.print(f"[red]Unknown action:[/red] {action}")
        console.print("[dim]Available: report, archive, cleanup, stale, unused, top[/dim]")
        raise typer.Exit(1)


@app.command(name="hub")
def hub(
    action: str = typer.Argument("list", help="Action: list, search, publish, install, telemetry"),
    query: str | None = typer.Option(None, "--query", "-q", help="Search query"),
    name: str | None = typer.Option(None, "--name", "-n", help="Skill name"),
    author: str | None = typer.Option(None, "--author", "-a", help="Author name for publish"),
    tags: str | None = typer.Option(None, "--tags", "-t", help="Comma-separated tags for publish"),
):
    """Skill hub — community-curated skill sharing.

    Actions:
        list      — List all skills in the hub
        search    — Search hub skills
        publish   — Publish a local skill to the hub
        install   — Install a skill from the hub
        telemetry — Show hub skill telemetry
    """
    from ah.skills.registry import SkillHub, skill_registry

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
            console.print(
                f"    Author: {s.get('author', 'unknown')} | Tags: {', '.join(s.get('tags', []))}"
            )

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
            raise typer.Exit(1) from None

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
            raise typer.Exit(1) from None

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
                str(t.get("usage_count", 0)),
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
    import importlib.util
    import shutil
    import sys

    from ah.cli.launcher import MIN_NODE_VERSION, _node_version, ui_dir

    console.print("[bold]AgentHarness Doctor[/bold]\n")

    def check(label: str, ok: bool, detail: str = "", hint: str = "") -> None:
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"  {label}: {mark} {detail}".rstrip())
        if not ok and hint:
            console.print(f"      {hint}", style="dim")

    check(
        "Python", sys.version_info >= (3, 11), sys.version.split()[0], "Python 3.11+ is required."
    )
    for dep in ("typer", "rich", "asyncpg", "httpx", "msgpack", "yaml", "tiktoken", "dotenv"):
        check(dep, importlib.util.find_spec(dep) is not None, "", "Run: pip install -e .")

    node = shutil.which("node")
    node_version = _node_version(node) if node else None
    wanted = ".".join(map(str, MIN_NODE_VERSION))
    check(
        "Node.js",
        node_version is not None and node_version >= MIN_NODE_VERSION,
        ".".join(map(str, node_version)) if node_version else "not found",
        f"Node.js {wanted}+ is required for the interactive UI.",
    )
    ui_deps = ui_dir() / "node_modules" / "@earendil-works" / "pi-tui"
    check(
        "UI dependencies",
        ui_deps.exists(),
        "",
        f'Run: npm install --ignore-scripts --prefix "{ui_dir()}"',
    )

    check("OPENROUTER_API_KEY", bool(config.get("openrouter_api_key")), "", "Set it in .env.")
    check("DATABASE_URL", bool(config.get("database_url")), "", "Set it in .env.")

    async def _check_db():
        try:
            await db.connect()
            version = await db.fetchval("SELECT version()")
            check("PostgreSQL", True, f"({version.split(',')[0]})")
        except Exception as e:
            check("PostgreSQL", False, f"({e})", "Is PostgreSQL running and DATABASE_URL correct?")
        finally:
            await db.close()

    _run(_check_db())
    console.print()
    output.muted("Run `ah` to open the interactive UI.")


@app.command()
def init(
    db_url: str | None = typer.Option(None, "--db-url", help="PostgreSQL connection URL"),
):
    """Initialize AgentHarness — create the database schema."""

    async def _init():
        if db_url:
            db.dsn = db_url
        console.print("[bold]Initializing AgentHarness...[/bold]")
        console.print(f"  Database: {db.dsn.split('@')[-1]}")
        try:
            with output.spinner("Creating schema..."):
                await db.connect()
                await db.initialize_schema()
            console.print("  Schema: [green]✓ created[/green]")
        except Exception as e:
            console.print(f"  Schema: [red]✗ failed[/red] ({e})")
            raise typer.Exit(1) from None
        finally:
            await db.close()
        console.print()
        output.success("AgentHarness initialized!")
        output.muted("Run `ah` to open the interactive UI.")

    _run(_init())


@app.command()
def version():
    """Show AgentHarness version."""
    console.print(f"[bold cyan]AgentHarness[/bold cyan] v{__version__}")


# ─── Config commands ─────────────────────────────────────────────────────────


@app.command(name="config")
def config_show():
    """Show current configuration."""
    output.config_table(config.to_dict())


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
    category: str | None = typer.Option(None, "--category", "-c", help="Filter by category"),
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

            output.memory_table(memories)
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
                console.print(
                    f"\n  [{i}] [cyan]({m.category}, importance={m.importance:.2f}, score={rm.score:.3f})[/cyan]"
                )
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

            mid = _parse_uuid(memory_id)
            if mid is None:
                console.print(f"[red]Invalid memory ID: {memory_id}[/red]")
                raise typer.Exit(1)
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
            from ah.memory.approval import ApprovalStatus, memory_approval_gate

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

            pid = _parse_uuid(pending_id)
            if pid is None:
                console.print(f"[red]Invalid pending memory ID: {pending_id}[/red]")
                raise typer.Exit(1)
            memory = await memory_approval_gate.approve(pid, review_note=note)
            if memory:
                console.print(
                    f"[green]Pending memory {pending_id} approved → memory {memory.id}[/green]"
                )
            else:
                console.print(
                    f"[yellow]Pending memory {pending_id} not found or already reviewed.[/yellow]"
                )
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

            pid = _parse_uuid(pending_id)
            if pid is None:
                console.print(f"[red]Invalid pending memory ID: {pending_id}[/red]")
                raise typer.Exit(1)
            rejected = await memory_approval_gate.reject(pid, review_note=note)
            if rejected:
                console.print(f"[green]Pending memory {pending_id} rejected.[/green]")
            else:
                console.print(
                    f"[yellow]Pending memory {pending_id} not found or already reviewed.[/yellow]"
                )
        finally:
            await db.close()

    _run(_memory_reject())


@app.command(name="memory-approve-all")
def memory_approve_all(
    agent_id: str | None = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
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
    agent_id: str | None = typer.Option(None, "--agent", "-a", help="Filter by agent ID"),
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

            console.print("[bold]User Profile[/bold]")
            console.print(f"  ID: {profile.id}")
            console.print(f"  User ID: {profile.user_id}")
            console.print(f"  Display Name: {profile.display_name or '(none)'}")
            console.print(f"  Interactions: {profile.interaction_count}")
            console.print(f"  Created: {profile.created_at.strftime('%Y-%m-%d %H:%M')}")

            if profile.preferences:
                console.print("\n  [bold]Preferences:[/bold]")
                for key, value in profile.preferences.items():
                    console.print(f"    {key}: {value}")

            if profile.topics:
                console.print("\n  [bold]Top Topics:[/bold]")
                for topic, count in profile.get_top_topics():
                    console.print(f"    {topic}: {count}")

            if profile.last_topics:
                console.print("\n  [bold]Recent Topics:[/bold]")
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
            updated = await user_profile_store.update_preferences(profile.id, profile.preferences)
            if updated:
                console.print(f"[green]Updated {key} = {value} for {user_id}[/green]")
            else:
                console.print("[red]Failed to update profile[/red]")
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
