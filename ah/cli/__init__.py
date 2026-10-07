"""AgentHarness CLI — the ``ah`` command.

``ah`` with no subcommand (also ``ah repl`` and ``ah chat -i``) launches the
TypeScript terminal UI in ``ui/``, which drives the agent through the Python
gateway (``python -m ah.gateway``). Every other subcommand is a one-shot admin
command rendered with Rich.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

import ah.tools  # noqa: F401 — registers all built-in tools
from ah import __version__, services
from ah.cli import output
from ah.cli.launcher import launch_ui
from ah.cli.output import console
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.db.connection import db

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="ah",
    help="AgentHarness — self-hosted AI agent framework. Run `ah` to open the interactive UI.",
    invoke_without_command=True,
)


@app.callback()
def main(
    ctx: typer.Context,
    mode: str = typer.Option(None, "--mode", help="Execution mode: ask|workspace|sandbox|full"),
) -> None:
    """Open the interactive UI when no subcommand is given."""
    if mode is not None:
        normalized = mode.strip().lower()
        if normalized not in ("ask", "workspace", "sandbox", "full"):
            output.error("Invalid --mode. Use ask|workspace|sandbox|full.")
            raise typer.Exit(1)
        config.set("execution_mode", normalized)
    if ctx.invoked_subcommand is None:
        raise typer.Exit(launch_ui())


def _run(coro):
    """Run an async coroutine from a sync Typer command."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _parse_uuid(s: str) -> uuid.UUID | None:
    """Parse a UUID string, returning None if invalid.

    Accepts canonical, braced ({...}), and URN (urn:uuid:...) forms via
    stdlib uuid.UUID; surrounding whitespace is ignored.
    """
    try:
        return uuid.UUID(s.strip() if isinstance(s, str) else s)
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
    model: str = typer.Option(None, "--model", "-m", help="Model to use (e.g., openrouter/free)"),
    provider: str = typer.Option(
        "openrouter",
        "--provider",
        "-p",
        help="LLM provider (openrouter, openai, anthropic, google, mistral, groq, together, deepseek, xai, ollama)",
    ),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show tool calls"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Open the interactive UI"),
    mode: str = typer.Option(None, "--mode", help="Execution mode: ask|workspace|sandbox|full"),
):
    """Send one message to the agent (or open the UI with -i)."""
    if mode is not None:
        normalized = mode.strip().lower()
        if normalized not in ("ask", "workspace", "sandbox", "full"):
            output.error("Invalid --mode. Use ask|workspace|sandbox|full.")
            raise typer.Exit(1)
        config.set("execution_mode", normalized)
    if interactive:
        raise typer.Exit(launch_ui(model=model, provider=provider, session_id=session_id))

    # Guard before any I/O: message is required, and slicing it for the
    # session title (message[:50]) would raise TypeError on None.
    if not message:
        console.print('[yellow]No message provided. Use: ah chat "your message"[/yellow]')
        raise typer.Exit(1)

    async def _chat():
        await db.connect()
        agent = None
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

            # Same shared factory as gateway/REST/jobs (definition, shared
            # memory/RAG, authority). Headless: no approver → broker returns
            # structured needs_approval instead of hanging.
            try:
                from ah.core.agent_factory import build_agent_for_session

                agent = await build_agent_for_session(session)
                llm = agent.provider
                _provider_name = provider or session.provider or config.get("provider")
            except ValueError as e:
                output.error(
                    f"Provider error: {e}", "Check your provider configuration with `ah config`."
                )
                raise typer.Exit(1) from None

            if verbose:
                output.muted(f"Model: {llm.model} ({_provider_name})")
                try:
                    output.muted(f"Mode: {config.get('execution_mode')} (backend: host)")
                except Exception:
                    pass
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
            try:
                if agent is not None:
                    from ah.core.agent_factory import close_agent_provider

                    await close_agent_provider(agent)
            except Exception:
                pass
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
            # Real capability reporting (never healthy-by-flag alone).
            try:
                summary = await services.status_summary()
                output.status_line(
                    "Mode", f"{summary.get('mode')} | Backend: {summary.get('backend')}", "success"
                )
                if summary.get("workspace"):
                    output.status_line("Workspace", str(summary.get("workspace")))
                output.status_line("Chat provider", str(summary.get("chatProvider", "unknown")))
                output.status_line(
                    "Memory retrieval",
                    f"{summary.get('memoryRetrieval')}"
                    + (
                        f" ({summary.get('memoryRetrievalReason')})"
                        if summary.get("memoryRetrievalReason")
                        else ""
                    ),
                )
                output.status_line("Memory extraction", str(summary.get("memoryExtraction", "")))
                output.status_line("Document RAG", str(summary.get("documentRag", "")))
                output.status_line("Reranker", str(summary.get("reranker", "")))
                output.status_line("Automatic compaction", str(summary.get("autoCompaction", "")))
                output.status_line("Sandbox", str(summary.get("sandbox", "")))
                output.status_line(
                    "Jobs awaiting approval", str(summary.get("jobsAwaitingApproval", 0))
                )
                output.status_line("Research training", str(summary.get("researchTraining", "")))
            except Exception as e:
                output.status_line("Capabilities", f"unavailable ({e})", "warning")
            console.print()
            output.muted("Run `ah` to open the interactive UI.")
        finally:
            await db.close()

    _run(_status())


@app.command(name="mode")
def set_mode(
    mode: str = typer.Argument(None, help="ask|workspace|sandbox|full (empty shows current)"),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke grants and return to ask mode"),
):
    """Show or set the execution mode."""

    async def _mode():
        await db.connect()
        try:
            if revoke:
                from ah.permissions.broker import permission_broker

                count = await permission_broker.revoke("*")
                config.set("execution_mode", "ask")
                output.status_line("Mode", f"ask ({count} grants revoked)", "success")
                return
            if not mode:
                output.status_line("Mode", str(config.get("execution_mode")), "success")
                console.print("ask: approval-based (default) | workspace: project operation")
                console.print("sandbox: isolated container | full: explicit FULL HOST grant")
                return
            normalized = mode.strip().lower()
            if normalized not in ("ask", "workspace", "sandbox", "full"):
                output.error("Invalid mode. Use ask|workspace|sandbox|full.")
                raise typer.Exit(1)
            if normalized == "full":
                confirm = typer.confirm(
                    "Grant FULL HOST access (your OS account, this session)? "
                    "Credentials/elevation/destructive ops still ask.",
                    default=False,
                )
                if not confirm:
                    output.muted("Full mode not activated.")
                    return
            config.set("execution_mode", normalized)
            output.status_line("Mode", normalized, "success")
            if normalized == "full":
                console.print(
                    "[yellow]FULL HOST active. Sensitive actions still ask. "
                    "Revoke with `ah mode --revoke`.[/yellow]"
                )
        finally:
            await db.close()

    _run(_mode())


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


@app.command(name="usage")
def usage_command(
    session_id: str | None = typer.Option(
        None, "--session", "-s", help="Session ID (default: last active)"
    ),
    agent: str | None = typer.Option(None, "--agent", help="Agent ID (default: session agent)"),
):
    """Show durable LLM usage and remaining budgets."""

    async def _usage():
        from ah.core.usage import usage_store

        await db.connect()
        try:
            session = await _resolve_session(session_id)
            result = await usage_store.summary(session.id, agent or session.agent_id)
            table = Table(title=f"Usage for {session.id}")
            table.add_column("Scope")
            table.add_column("Calls", justify="right")
            table.add_column("Accounted tokens", justify="right")
            table.add_column("Unknown calls", justify="right")
            table.add_column("Calls remaining", justify="right")
            table.add_column("Tokens remaining", justify="right")
            for scope in ("session", "agent"):
                item = result[scope]
                table.add_row(
                    scope,
                    str(item["requests"]),
                    str(item["accountedTokens"]),
                    str(item["unknownCalls"]),
                    str(item["requestsRemaining"])
                    if item["requestsRemaining"] is not None
                    else "unlimited",
                    str(item["tokensRemaining"])
                    if item["tokensRemaining"] is not None
                    else "unlimited",
                )
            console.print(table)
        finally:
            await db.close()

    _run(_usage())


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
            from ah.tools.file import resolve_path

            try:
                output_path = resolve_path(filename)
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

    check(
        "OPENROUTER_API_KEY",
        bool(config.get("openrouter_api_key")),
        "",
        "Run `ah setup` or set it in .env.",
    )
    check("DATABASE_URL", bool(config.get("database_url")), "", "Run `ah setup` or set it in .env.")

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
def setup(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing .env file"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Create a blank .env without prompting"
    ),
):
    """First-launch setup — create the .env file and configure API keys.

    Creates the git-ignored ``.env`` next to the project (or at
    ``$AH_ENV_FILE``) with one entry per model family you use. Existing
    values are kept when you press Enter; ``--force`` starts over.
    """
    import secrets as _secrets

    from ah.security.env_file import PROVIDER_KEYS, find_env_file, read_env_values, set_env_values

    target = find_env_file()
    if target.is_file() and force:
        console.print(f"[bold]Overwriting existing .env:[/bold] {target}")
        existing: dict[str, str] = {}
    elif target.is_file():
        console.print(f"[bold]Found existing .env:[/bold] {target}")
        console.print("Press Enter to keep each current value (use --force to start over).")
        existing = read_env_values(target)
    else:
        console.print(f"[bold]Creating new .env:[/bold] {target}")
        existing = {}

    prompts: list[tuple[str, str, str, bool]] = [
        (
            "DATABASE_URL",
            "PostgreSQL connection string",
            "postgresql://postgres:postgres@localhost:5432/agentharness",
            False,
        ),
        *[(key, desc, "", True) for key, desc in PROVIDER_KEYS],
        ("AGENT_HARNESS_API_KEY", "HTTP API key (blank = auto-generate)", "", True),
        ("AGENT_HARNESS_PROVENANCE_KEY", "Memory provenance key (blank = auto-generate)", "", True),
    ]

    updates: dict[str, str] = {}
    if not non_interactive:
        for key, desc, default, hidden in prompts:
            current = existing.get(key, "")
            shown_default = default or ("" if hidden else current)
            if hidden and current.strip():
                prompt_text = f"{key} ({desc}) [currently set — Enter to keep]"
            elif hidden:
                prompt_text = f"{key} ({desc}) [Enter to skip]"
            elif current.strip():
                prompt_text = f"{key} [Enter to keep]"
                shown_default = current
            else:
                prompt_text = f"{key} ({desc})"
            try:
                answer = typer.prompt(
                    prompt_text,
                    default=shown_default,
                    hide_input=hidden,
                    show_default=bool(shown_default) and not hidden,
                )
            except (typer.Abort, KeyboardInterrupt):
                console.print("\n[dim]Setup cancelled.[/dim]")
                raise typer.Exit(1) from None
            answer = (answer or "").strip()
            if hidden and not answer and current.strip():
                continue  # keep existing secret
            if not hidden and not answer and current.strip():
                continue
            if key in ("AGENT_HARNESS_API_KEY", "AGENT_HARNESS_PROVENANCE_KEY") and not answer:
                answer = _secrets.token_hex(32)
                console.print(f"  Generated random {key}.")
            if answer != current:
                updates[key] = answer
    else:
        # Blank file: seed connection defaults, leave provider keys for the /keys menu.
        updates = {
            "DATABASE_URL": existing.get(
                "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/agentharness"
            )
        }
        if "DATABASE_URL" in existing:
            updates = {}

    if not target.is_file():
        # Seed from .env.example so comments/docs carry over, then apply values.
        example = target.parent / ".env.example"
        if example.is_file():
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            target.write_text("# AgentHarness environment (git-ignored)\n", encoding="utf-8")
    if updates:
        set_env_values(updates, target)
        changed = ", ".join(sorted(updates))
        console.print(f"\n[green]✓ Wrote {len(updates)} value(s) to .env:[/green] {changed}")
    else:
        console.print("\n[dim]No changes written.[/dim]")
    console.print()
    output.muted("Next: run `ah doctor`, then `ah init` to create the database schema.")

    # Show key status without ever printing values.
    stored = read_env_values(target)
    for key, _desc in PROVIDER_KEYS:
        mark = "[green]set[/green]" if stored.get(key, "").strip() else "[dim]not set[/dim]"
        console.print(f"  {key}: {mark}")


@app.command()
def version():
    """Show AgentHarness version."""
    console.print(f"[bold cyan]AgentHarness[/bold cyan] v{__version__}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind host"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload"),
):
    """Start the AgentHarness HTTP API server."""
    import uvicorn

    console.print("[bold]AgentHarness API[/bold]")
    console.print(f"  Host: {host}")
    console.print(f"  Port: {port}")
    console.print(f"  Reload: {'enabled' if reload else 'disabled'}")
    console.print()

    url = f"http://{host}:{port}"

    try:
        with output.spinner("Starting server..."):
            from ah.api.app import create_app

            create_app()
    except Exception as e:
        output.error(f"Failed to start server: {e}")
        raise typer.Exit(1) from None

    console.print(f"[green]Server ready at {url}[/green]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")
    console.print()

    try:
        uvicorn.run(
            "ah.api.app:create_app",
            host=host,
            port=port,
            reload=reload,
            factory=True,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Server stopped.[/dim]")


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
                if rm.persona_interpretation:
                    console.print(f"      [dim][persona] {rm.persona_interpretation[:200]}[/dim]")
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


def _uninstall_app_root() -> Path:
    """Checkout root this ``ah`` binary was installed from."""
    return Path(__file__).resolve().parent.parent.parent


def _filter_path_entries(path_value: str, remove: list[str]) -> str:
    """Return PATH with every entry in ``remove`` dropped.

    Comparison is case-insensitive with trailing separators stripped, so
    ``C:\\x\\.venv\\Scripts`` matches ``c:\\x\\.venv\\Scripts\\``.
    """
    gone = {r.rstrip("/\\").lower() for r in remove}
    return ";".join(p for p in path_value.split(";") if p.rstrip("/\\").lower() not in gone)


def _shim_points_at(shim_text: str, app_root: str) -> bool:
    """True when a ``~/.local/bin/ah`` shim execs an ``ah`` inside ``app_root``."""
    return app_root.rstrip("/\\").lower() in shim_text.lower()


def _docker_container_exists(name: str = "agentharness-db") -> bool:
    """True when a docker container called *name* exists (running or stopped)."""
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess

        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return any(line.strip() == name for line in out.stdout.splitlines())


def _remove_docker_container(name: str = "agentharness-db") -> tuple[bool, str]:
    """Stop and remove the docker container (its data goes with it)."""
    import subprocess

    for action in (["docker", "stop", name], ["docker", "rm", name]):
        try:
            out = subprocess.run(action, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"docker {' '.join(action[1:])} failed ({e})"
        if out.returncode != 0 and "No such container" not in (out.stderr or ""):
            return False, f"docker {' '.join(action[1:])} failed: {(out.stderr or '').strip()}"
    return True, f"container {name} stopped and removed (its data is gone)"


def _brew_postgres_present() -> bool:
    """True when Homebrew's postgresql@16 is installed (macOS installer path)."""
    import subprocess
    import sys

    if not sys.platform.startswith("darwin") or shutil.which("brew") is None:
        return False
    try:
        out = subprocess.run(
            ["brew", "--prefix", "postgresql@16"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def _remove_brew_postgres() -> tuple[bool, str]:
    """Stop the brew service and uninstall postgresql@16 + pgvector."""
    import subprocess

    for action in (
        ["brew", "services", "stop", "postgresql@16"],
        ["brew", "uninstall", "postgresql@16", "pgvector"],
    ):
        try:
            out = subprocess.run(action, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{' '.join(action)} failed ({e})"
        if out.returncode != 0:
            return False, f"{' '.join(action)} failed: {(out.stderr or out.stdout or '').strip()}"
    return True, "brew postgresql@16 + pgvector uninstalled (its data is gone)"


def _valid_db_identifier(name: str) -> bool:
    """Conservative identifier check so DROP DATABASE can't escape its quoting."""
    import re

    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", name or ""))


def _split_database_dsn(dsn: str) -> tuple[str, str] | None:
    """Split a Postgres DSN into (maintenance_dsn, dbname) for DROP DATABASE.

    Returns None when the URL doesn't parse or names a protected database.
    """
    from urllib.parse import urlparse

    try:
        parts = urlparse(dsn)
    except ValueError:
        return None
    dbname = (parts.path or "").lstrip("/")
    if not _valid_db_identifier(dbname) or dbname in ("postgres", "template0", "template1"):
        return None
    maintenance = parts._replace(path="/postgres").geturl()
    return maintenance, dbname


async def _drop_database(maintenance_dsn: str, dbname: str) -> None:
    """DROP DATABASE on the maintenance connection. Raises on failure."""
    import asyncpg

    conn = await asyncpg.connect(maintenance_dsn, timeout=15)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    finally:
        await conn.close()


def _remove_windows_user_path_entries(remove: list[str]) -> tuple[bool, str]:
    """Drop entries from the HKCU ``Path`` value. Returns (changed, detail)."""
    if os.name != "nt":
        return False, "not Windows, skipping registry edit"
    try:
        import winreg
    except ImportError as e:
        return False, f"winreg unavailable ({e})"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return False, "no user Path value found"
            updated = _filter_path_entries(current, remove)
            if updated == current:
                return False, "no matching entries in user Path"
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)
    except OSError as e:
        return False, f"registry edit failed ({e})"
    # Best-effort: tell Explorer/new processes the environment changed.
    try:
        import ctypes

        HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x1A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x0002, 5000, None
        )
    except Exception:  # noqa: BLE001 — broadcast is cosmetic
        pass
    return True, "removed from user Path (takes effect in new terminals)"


@app.command()
def uninstall():
    """Uninstall AgentHarness — answers yes to everything and nothing is left.

    Asks one yes/no question per item (PATH registration, virtualenv, .env,
    local config, docker container, Postgres database, Homebrew postgres,
    checkout) and removes everything you confirm.
    """
    app_root = _uninstall_app_root()
    venv_dir = app_root / ".venv"
    from ah.security.env_file import find_env_file, read_env_values

    try:
        env_file = find_env_file()
    except Exception:  # noqa: BLE001 — fall back to the checkout .env
        env_file = app_root / ".env"
    shim = Path.home() / ".local" / "bin" / "ah"
    local_config = Path.home() / ".agent-harness"

    # Discover what's actually there so we only ask about real things.
    has_docker = _docker_container_exists()
    has_brew_pg = _brew_postgres_present()
    maintenance_dsn: str | None = None
    dbname = ""
    if env_file.is_file():
        for key in ("DATABASE_URL",):
            dsn = read_env_values(env_file).get(key, "")
            if dsn:
                split = _split_database_dsn(dsn)
                if split is not None:
                    maintenance_dsn, dbname = split
                break

    console.print("[bold]What should go?[/bold] (yes/no)")
    remove_path = typer.confirm("Remove PATH registration (user Path + shim)?", default=True)
    remove_venv = typer.confirm(f"Delete the virtualenv ({venv_dir})?", default=False)
    remove_env_file = typer.confirm(f"Delete the env file ({env_file})?", default=False)
    remove_config = typer.confirm(f"Delete local config ({local_config})?", default=False)
    remove_docker = (
        typer.confirm(
            "Stop and remove the agentharness-db docker container (its data too)?",
            default=False,
        )
        if has_docker
        else False
    )
    drop_db = (
        typer.confirm(
            f'DROP the Postgres database "{dbname}" (all sessions/memories/jobs)?',
            default=False,
        )
        if maintenance_dsn
        else False
    )
    remove_postgres = (
        typer.confirm("Uninstall Homebrew postgresql@16 + pgvector entirely?", default=False)
        if has_brew_pg
        else False
    )
    delete_checkout = typer.confirm(f"Delete the whole checkout ({app_root})?", default=False)
    if not remove_path and not any(
        (
            remove_venv,
            remove_env_file,
            remove_config,
            delete_checkout,
            remove_docker,
            drop_db,
            remove_postgres,
        )
    ):
        console.print("[dim]Nothing selected.[/dim]")
        raise typer.Exit(0)

    path_targets = [str(venv_dir / "Scripts"), str(venv_dir / "bin")]
    plan: list[str] = []
    if remove_path:
        plan.append("Remove PATH registration (user Path entry + ~/.local/bin/ah shim)")
    if remove_venv and venv_dir.is_dir():
        plan.append(f"Delete {venv_dir}")
    if remove_env_file and env_file.is_file():
        plan.append(f"Delete {env_file}")
    if remove_config and local_config.exists():
        plan.append(f"Delete {local_config}")
    if remove_docker and has_docker:
        plan.append("Stop + remove docker container agentharness-db (with its data)")
    if drop_db and maintenance_dsn:
        plan.append(f'DROP DATABASE "{dbname}"')
    if remove_postgres and has_brew_pg:
        plan.append("Uninstall Homebrew postgresql@16 + pgvector")
    if delete_checkout:
        plan.append(f"Delete checkout {app_root}")

    console.print("[bold]Uninstall plan:[/bold]")
    for item in plan:
        console.print(f"  • {item}")

    # 1. PATH: Windows registry + shim file (created by install.sh).
    if remove_path:
        changed, detail = _remove_windows_user_path_entries(path_targets)
        output.status_line("PATH", detail, "success" if changed else "warning")
        if shim.is_file():
            try:
                points_here = _shim_points_at(
                    shim.read_text(encoding="utf-8", errors="replace"), str(app_root)
                )
            except OSError:
                points_here = False
            if points_here:
                try:
                    shim.unlink()
                    output.status_line("Shim", f"deleted {shim}", "success")
                except OSError as e:
                    output.error(f"Could not delete shim {shim}: {e}")
            else:
                output.muted(f"Shim {shim} points elsewhere, leaving it.")
        else:
            output.muted("No ~/.local/bin/ah shim found.")
    else:
        output.muted("Keeping PATH registration.")

    # 2. Docker + database before files (drop-db needs the DSN from .env).
    if remove_docker:
        if has_docker:
            ok, detail = _remove_docker_container()
            output.status_line("Docker", detail, "success" if ok else "error")
        else:
            output.muted("No agentharness-db container found.")
    if drop_db:
        if maintenance_dsn:
            try:
                _run(_drop_database(maintenance_dsn, dbname))
                output.status_line("Database", f'dropped "{dbname}"', "success")
            except Exception as e:
                output.error(f'Could not drop database "{dbname}": {e}')
        else:
            output.muted("No droppable DATABASE_URL found in .env.")
    if remove_postgres:
        if has_brew_pg:
            ok, detail = _remove_brew_postgres()
            output.status_line("Postgres", detail, "success" if ok else "error")
        else:
            output.muted("No Homebrew postgresql@16 found.")

    # 3. Files.
    def _rmtree(path: Path, label: str) -> None:
        try:
            shutil.rmtree(path, ignore_errors=False)
            output.status_line(label, f"deleted {path}", "success")
        except Exception as e:
            output.error(f"Could not delete {path}: {e}")

    if remove_venv and venv_dir.is_dir():
        _rmtree(venv_dir, "Venv")
    if remove_env_file and env_file.is_file():
        try:
            env_file.unlink()
            output.status_line("Env file", f"deleted {env_file}", "success")
        except OSError as e:
            output.error(f"Could not delete {env_file}: {e}")
    if remove_config and local_config.exists():
        if local_config.is_dir():
            _rmtree(local_config, "Config")
        else:
            try:
                local_config.unlink()
                output.status_line("Config", f"deleted {local_config}", "success")
            except OSError as e:
                output.error(f"Could not delete {local_config}: {e}")

    # 4. Checkout last — the running `ah` binary lives inside it, so on
    # Windows the delete can fail with a file lock. Report leftovers + manual cmd.
    if delete_checkout:
        try:
            shutil.rmtree(app_root, ignore_errors=False)
            output.status_line("Checkout", f"deleted {app_root}", "success")
        except Exception as e:  # noqa: BLE001 — locked running exe on Windows
            output.error(f"Could not delete checkout {app_root}: {e}")
            if os.name == "nt":
                console.print(
                    f'[dim]Close this terminal, then run: Remove-Item -Recurse -Force "{app_root}"[/dim]'
                )
            else:
                console.print(f'[dim]Then run: rm -rf "{app_root}"[/dim]')

    console.print()
    output.success("Uninstall complete. Open a new terminal for the PATH change to take effect.")


if __name__ == "__main__":
    app()
