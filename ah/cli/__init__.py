"""AgentHarness CLI — `ah` command."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table
from rich.text import Text

from ah import __version__
from ah.core.agent import ReActAgent
from ah.core.context import context_manager
from ah.core.provider import get_provider
from ah.core.session import session_manager
from ah.core.config import config
from ah.db.connection import db
from ah.tools import builtins  # noqa: F401 — registers built-in tools
from ah.cli.visual import get_default_visual
from ah.cli.animations import (
    SquareLoader,
    get_spinner,
    get_thinking_animation,
    get_streaming_animation,
    get_tool_animation,
    get_animation_runner,
    SUCCESS,
    WARNING,
    ERROR,
    INFO,
    MUTED,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="ah",
    help="AgentHarness — self-hosted multi-agent AI orchestration framework",
    no_args_is_help=True,
)
viz = get_default_visual()
console = viz.console


def _gradient_color(idx: int, total: int, start_hex: str, end_hex: str) -> str:
    """Interpolate between two hex colors for gradient effect."""
    def hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    def rgb_to_hex(r: int, g: int, b: int) -> str:
        return f"#{r:02X}{g:02X}{b:02X}"
    r1, g1, b1 = hex_to_rgb(start_hex)
    r2, g2, b2 = hex_to_rgb(end_hex)
    t = idx / max(total - 1, 1)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return rgb_to_hex(r, g, b)


def _print_banner():
    """Print the ASCII art banner with gradient colors."""
    # Agent line — gradient from cyan to blue
    agent_lines = [
        "  █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
        " ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
        " ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║",
        " ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║",
        " ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║",
        " ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝",
    ]
    # Harness line — gradient from purple to magenta
    harness_lines = [
        "  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗",
        "  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝",
        "  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗",
        "  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║",
        "  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║",
        "  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝",
    ]

    banner_text = Text()
    # Agent gradient: #00D4FF → #3B82F6
    for i, line in enumerate(agent_lines):
        color = _gradient_color(i, len(agent_lines), "#00D4FF", "#3B82F6")
        banner_text.append(line + "\n", style=f"bold {color}")
    banner_text.append("\n")
    # Harness gradient: #7C3AED → #EC4899
    for i, line in enumerate(harness_lines):
        color = _gradient_color(i, len(harness_lines), "#7C3AED", "#EC4899")
        banner_text.append(line + "\n", style=f"bold {color}")
    banner_text.append("\n")
    banner_text.append("  Self-hosted Multi-Agent AI Orchestration\n", style="dim")
    banner_text.append(f"  v{__version__}", style="dim")

    console.print(banner_text)


def _run(coro):
    """Run async coroutine from sync Typer command."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _parse_uuid(s: str) -> "uuid.UUID | None":
    """Parse a UUID string, returning None if invalid."""
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        return None


def _print_colored(message: str, color: str = "white", bold: bool = False, icon: str = ""):
    """Print a color-coded message with optional icon."""
    style = color
    if bold:
        style = f"bold {color}"
    if icon:
        console.print(f"{icon} {message}", style=style)
    else:
        console.print(message, style=style)


def _print_success(message: str, icon: str = "✓"):
    """Print a success message in green."""
    _print_colored(message, color=SUCCESS, bold=True, icon=icon)


def _print_error(message: str, icon: str = "✗"):
    """Print an error message in red."""
    _print_colored(message, color=ERROR, bold=True, icon=icon)


def _print_warning(message: str, icon: str = "⚠"):
    """Print a warning message in yellow."""
    _print_colored(message, color=WARNING, bold=True, icon=icon)


def _print_info(message: str, icon: str = "ℹ"):
    """Print an info message in blue."""
    _print_colored(message, color=INFO, icon=icon)


def _print_muted(message: str):
    """Print a muted/dimmed message."""
    console.print(message, style=MUTED)


@app.command()
def chat(
    message: str = typer.Argument(None, help="Message to send to the agent"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="Continue last session"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
    model: str = typer.Option(None, "--model", "-m", help="Model to use (e.g., anthropic/claude-3.5-sonnet)"),
    provider: str = typer.Option("openrouter", "--provider", "-p", help="LLM provider (openrouter, ollama)"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Launch interactive REPL mode"),
    tui: bool = typer.Option(False, "--tui", help="Launch the pi-style TUI (ported terminal UI)"),
):
    """Chat with the agent. Creates a new session or continues an existing one."""

    if tui:
        from ah.cli.tui.screen import run_tui
        _run(run_tui(model=model, provider=provider, verbose=verbose, session_id=session_id))
        return

    if interactive:
        from ah.cli.interactive_win import run_repl
        _run(run_repl(model=model, provider=provider, verbose=verbose, session_id=session_id))
        return

    # Guard before any I/O: message is required, and slicing it for the
    # session title (message[:50]) would raise TypeError on None.
    if not message:
        console.print("[yellow]No message provided. Use: ah chat \"your message\"[/yellow]")
        raise typer.Exit(1)

    async def _chat():
        await db.connect()
        try:
            # Determine session
            if continue_:
                session = await session_manager.get_last_active()
                if not session:
                    viz.error("No active session to continue.", title="Error", suggestion="Use `ah sessions` to list available sessions.")
                    raise typer.Exit(1)
                _print_muted(f"Continuing session: {session.id}")
            elif session_id:
                sid = _parse_uuid(session_id)
                if sid is None:
                    console.print(f"[red]Invalid session ID: {session_id}[/red]")
                    raise typer.Exit(1)
                session = await session_manager.get(sid)
                if not session:
                    viz.error(f"Session {session_id} not found", title="Error", suggestion="Use `ah sessions` to list available sessions.")
                    raise typer.Exit(1)
                _print_muted(f"Resuming session: {session.id}")
            else:
                session = await session_manager.create(
                    title=message[:50] if message else None,
                    goal=message[:100] if message else None,
                )
                _print_muted(f"New session: {session.id}")

            # Create LLM provider
            try:
                llm = get_provider(provider=provider, model=model)
            except ValueError as e:
                viz.error(f"Provider error: {e}", title="Error", suggestion="Check your provider configuration with `ah config`.")
                raise typer.Exit(1)

            # Create agent and run
            agent = ReActAgent(provider=llm)
            if verbose:
                _print_muted(f"Model: {llm.model} ({provider})")
                console.print()

            # Stream response with animations
            response_text = ""
            tool_calls_count = 0
            tokens_used = 0

            # Create animation runner for this turn
            runner = get_animation_runner(console)

            # Start thinking animation with braille spinner
            thinking = get_thinking_animation(console, spinner_type="dots")
            runner.add("thinking", thinking)

            # Start streaming display with green style
            stream = get_streaming_animation(console, style="green")
            runner.add_streaming("stream", stream)

            # Start the animations
            runner.start_all()

            async for event in agent.run_stream(session.id, message, verbose=verbose):
                if event.type == "text":
                    response_text += event.content
                    stream.add_token(event.content)
                elif event.type == "tool_call":
                    tool_calls_count += 1
                    if verbose:
                        tool_anim = get_tool_animation(console, event.tool_name)
                        tool_anim.start()
                        runner.add(f"tool_{tool_calls_count}", tool_anim)
                elif event.type == "tool_result":
                    if verbose:
                        tool_key = f"tool_{tool_calls_count}"
                        if tool_key in runner._animations:
                            runner.complete(tool_key)
                        preview = str(event.tool_result)[:100].replace("\n", " ")
                        stream.add_token(f"\n  ← {preview}\n")
                elif event.type == "token_usage":
                    tokens_used = event.tokens_used
                elif event.type == "done":
                    response_text = event.response.content
                    tool_calls_count = len(event.response.tool_calls)
                    tokens_used = event.response.tokens_used
                    stream.set_text(response_text)

            # Stop all animations
            runner.stop_all()

            console.print()
            viz.print_response_panel(response_text, title="Agent")
            console.print()
            if verbose:
                _print_muted(f"Iterations: {event.response.iterations} | Tool calls: {tool_calls_count} | Tokens: {tokens_used}")
            _print_muted(f"Session ID: {session.id}")

        finally:
            await db.close()

    _run(_chat())


@app.command()
def repl(
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(None, "--provider", "-p", help="LLM provider"),
    verbose: bool = typer.Option(None, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
    tui: bool = typer.Option(False, "--tui", help="Launch the pi-style TUI (ported terminal UI)"),
):
    """Launch interactive REPL mode."""
    if tui:
        from ah.cli.tui.screen import run_tui
        _run(run_tui(model=model, provider=provider, verbose=verbose, session_id=session_id))
        return
    from ah.cli.interactive_win import run_repl
    _run(run_repl(model=model, provider=provider, verbose=verbose, session_id=session_id))


@app.command(name="tui")
def tui_cmd(
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: str = typer.Option(None, "--provider", "-p", help="LLM provider"),
    verbose: bool = typer.Option(None, "--verbose/--quiet", "-v/-q", help="Show tool calls and reasoning"),
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="Resume specific session"),
    skin: str = typer.Option("dark", "--skin", help="Theme: dark or light"),
):
    """Launch the pi-style TUI (ported from earendil-works/pi, MIT)."""
    from ah.cli.tui.screen import run_tui
    _run(run_tui(model=model, provider=provider, verbose=verbose, session_id=session_id, skin=skin))


@app.command()
def status():
    """Show AgentHarness status and recent sessions."""

    async def _status():
        # Animated spinner while checking status
        spinner = get_spinner(console, label="Checking status...", spinner_type="dots")
        spinner.start()

        await db.connect()
        try:
            # Check DB
            try:
                version = await db.fetchval("SELECT version()")
                viz.print_status_line("PostgreSQL", f"connected ({version.split(',')[0]})", "success")
            except Exception as e:
                viz.print_status_line("PostgreSQL", f"connection failed ({e})", "error")
                spinner.stop()
                return

            # Count sessions
            count = await db.fetchval("SELECT COUNT(*) FROM sessions")
            viz.print_status_line("Sessions", str(count), "info")

            # Count context chunks
            chunks = await db.fetchval("SELECT COUNT(*) FROM context_chunks")
            viz.print_status_line("Context chunks", str(chunks), "info")

            # Check tools
            from ah.tools.base import registry
            tools = registry.list_tools()
            viz.print_status_line("Tools", f"{len(tools)} registered", "info")
            for t in tools:
                console.print(f"    [dim]•[/dim] {t}")

            # Check config
            from ah.core.config import config
            if config.get("openrouter_api_key"):
                viz.print_status_line("OpenRouter API key", "set", "success")
            else:
                viz.print_status_line("OpenRouter API key", "not set", "warning")

            spinner.stop()
            console.print()
            _print_muted("Run `ah chat \"your message\"` to start.")

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

            viz.print_sessions_table(sessions)
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

            viz.print_sessions_table(sessions)
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

            from ah.tools.file import _resolve_path
            try:
                output_path = _resolve_path(filename)
            except ValueError as e:
                console.print(f"[red]Path traversal blocked:[/red] {e}")
                raise typer.Exit(1)
            output_path.write_text("\n".join(lines), encoding="utf-8")
            _print_success(f"Exported session {session.id} to {filename}")
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
            sid = _parse_uuid(session_id)
            if sid is None:
                console.print(f"[red]Invalid session ID: {session_id}[/red]")
                raise typer.Exit(1)
            new_session = await session_manager.fork(sid, title=title)
            _print_success(f"Forked session {session_id} → {new_session.id}")
            _print_muted(f"Title: {new_session.title or '(untitled)'}")
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

            viz.print_context_table(chunks, sid)

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
            if not session:
                console.print(f"[red]Session {session_id} not found[/red]")
                raise typer.Exit(1)

            from ah.core.compression import ContextCompressor, CompressionConfig

            chunks = await context_manager.get_chunks(sid, limit=1000)
            if not chunks:
                console.print("[yellow]No context chunks to compress.[/yellow]")
                return

            total_tokens = await context_manager.get_token_usage(sid)
            _print_muted(f"Session: {session.id}")
            _print_muted(f"Current context: {len(chunks)} chunks, {total_tokens} tokens")

            # Animated loader during compression
            loader = SquareLoader(console, "Compressing context...", width=30)
            loader.start()

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

            loader.complete()

            if result.original_count == 0:
                console.print("[yellow]Nothing to compress (not enough chunks).[/yellow]")
                return

            # Atomically replace old chunks with compressed ones (order-preserving)
            await context_manager.replace_chunks(sid, result.compressed_chunks)

            _print_success(
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
    viz.print_skills_table(all_skills)


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
    from ah.tools.builtins import _is_safe_url
    skill_registry.load_all()

    # Determine source type
    source_path = Path(source)
    if source_path.exists() and source_path.is_file():
        # Local CLI run by the file's owner, so any readable file is allowed.
        # (The previous `".." in source` string check was not a real boundary:
        # it rejected legitimate relative paths but allowed absolute ones.)
        resolved = source_path.resolve()
        try:
            content = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            console.print(f"[red]Cannot learn from binary or non-UTF-8 file:[/red] {resolved.name}")
            raise typer.Exit(1) from None
        except OSError as e:
            console.print(f"[red]Cannot read file:[/red] {e}")
            raise typer.Exit(1) from None
        skill_name = name or resolved.stem
        skill_description = description or f"Skill learned from {resolved.name}"
        trigger_list = [t.strip() for t in triggers.split(",")] if triggers else []
    elif source.startswith("http://") or source.startswith("https://"):
        # Learn from URL — SSRF protection
        if not _is_safe_url(source):
            console.print(f"[red]URL rejected by security policy (private/internal address or invalid protocol):[/red] {source}")
            raise typer.Exit(1)
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
    try:
        skill = skill_registry.create_skill(
            name=skill_name,
            description=skill_description,
            content=content,
            triggers=trigger_list,
            source=source,
            source_type="learned",
        )
    except ValueError as e:
        console.print(f"[red]Skill rejected:[/red] {e}")
        raise typer.Exit(1) from None
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
    console.print("[bold]AgentHarness Doctor[/bold]\n")

    # Python version
    import sys
    py_ok = sys.version_info >= (3, 11)
    py_icon = "✓" if py_ok else "✗"
    py_color = "green" if py_ok else "red"
    console.print(f"  Python: {sys.version.split()[0]} [{py_color}]{py_icon}[/{py_color}]{'' if py_ok else ' (need 3.11+)'}")

    # Dependencies with spinner
    deps = ["typer", "rich", "asyncpg", "httpx", "msgpack", "prompt_toolkit"]
    spinner = get_spinner(console, label="Checking dependencies...", spinner_type="dots")
    spinner.start()
    for dep in deps:
        try:
            __import__(dep)
            console.print(f"  {dep}: [green]✓ ok[/green]")
        except ImportError:
            console.print(f"  {dep}: [red]✗ missing[/red]")
    spinner.stop()

    # Database with loader
    loader = SquareLoader(console, "Connecting to database...", width=25)
    loader.start()

    async def _check_db():
        try:
            await db.connect()
            loader.set_progress(0.5)
            version = await db.fetchval("SELECT version()")
            loader.complete()
            console.print(f"  PostgreSQL: [green]✓ connected[/green] ({version.split(',')[0]})")
        except Exception as e:
            loader.error()
            console.print(f"  PostgreSQL: [red]✗ failed[/red] ({e})")
        finally:
            await db.close()

    _run(_check_db())

    # Environment
    from ah.core.config import config
    if config.get("openrouter_api_key"):
        console.print("  OPENROUTER_API_KEY: [green]✓ set[/green]")
    else:
        console.print("  OPENROUTER_API_KEY: [yellow]⚠ not set[/yellow]")

    if config.get("database_url"):
        console.print("  DATABASE_URL: [green]✓ set[/green]")
    else:
        console.print("  DATABASE_URL: [yellow]⚠ not set (using default)[/yellow]")

    console.print()
    _print_muted("Run `ah chat \"hello\"` to test the agent.")


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

        # Animated loader during initialization
        loader = SquareLoader(console, "Creating schema...", width=30)
        loader.start()

        try:
            await db.connect()
            loader.set_progress(0.5)
            await db.initialize_schema()
            loader.complete()
            console.print("  Schema: [green]✓ created[/green]")
        except Exception as e:
            loader.error()
            console.print(f"  Schema: [red]✗ failed[/red] ({e})")
            raise typer.Exit(1)
        finally:
            await db.close()

        console.print()
        _print_success("AgentHarness initialized!")
        _print_muted("Run `ah chat \"hello\"` to start.")

    _run(_init())


@app.command()
def version():
    """Show AgentHarness version."""
    _print_banner()


# ─── Config commands ─────────────────────────────────────────────────────────

@app.command(name="config")
def config_show():
    """Show current configuration."""
    viz.print_config_table(config.to_dict())


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

            viz.print_memory_table(memories)
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
            pid = _parse_uuid(pending_id)
            if pid is None:
                console.print(f"[red]Invalid pending memory ID: {pending_id}[/red]")
                raise typer.Exit(1)
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
            pid = _parse_uuid(pending_id)
            if pid is None:
                console.print(f"[red]Invalid pending memory ID: {pending_id}[/red]")
                raise typer.Exit(1)
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
