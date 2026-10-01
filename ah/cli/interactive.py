"""Interactive REPL — persistent prompt with streaming, slash commands, and session management."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax

from ah import __version__
from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import StreamEvent
from ah.core.provider import get_provider
from ah.core.session import Session, session_manager
from ah.db.connection import db
from ah.tools import builtins  # noqa: F401 — registers built-in tools

logger = logging.getLogger(__name__)

# Prompt style
PROMPT_STYLE = Style.from_dict({
    "prompt": "ansicyan bold",
    "path": "ansigreen",
})

# Slash commands registry
SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show available slash commands",
    "/status": "Show current session and agent status",
    "/sessions": "List recent sessions",
    "/new": "Create a new session",
    "/switch": "Switch to a different session",
    "/context": "Show context for current session",
    "/model": "Show or set the model (e.g., /model anthropic/claude-3.5-sonnet)",
    "/provider": "Show or set the provider (e.g., /provider openrouter)",
    "/budget": "Show or set context budget (e.g., /budget 8000)",
    "/verbose": "Toggle verbose mode on/off",
    "/config": "Show current configuration",
    "/clear": "Clear the screen",
    "/exit": "Exit the REPL",
}


class InteractiveREPL:
    """Interactive REPL for AgentHarness.

    Usage:
        repl = InteractiveREPL()
        await repl.run()
    """

    def __init__(
        self,
        model: str | None = None,
        provider: str | None = None,
        verbose: bool | None = None,
        session_id: str | None = None,
    ) -> None:
        self.console = Console()
        self.model = model or config.get("model")
        self.provider = provider or config.get("provider")
        self.verbose = config.get("verbose") if verbose is None else verbose
        self.context_budget = config.get("context_budget")
        self.agent_id = config.get("agent_id")

        # Session state
        self.session: Session | None = None
        if session_id:
            try:
                sid = uuid.UUID(session_id)
                # Will be loaded in run() since we need async
                self._initial_session_id = sid
            except ValueError:
                console.print(f"[red]Invalid session ID: {session_id}[/red]")
                self._initial_session_id = None
        else:
            self._initial_session_id = None

        # Prompt session
        history_path = config.get("history_size")
        self._prompt_session: PromptSession = PromptSession(
            history=FileHistory(str(self._get_history_file())),
            auto_suggest=AutoSuggestFromHistory(),
            style=PROMPT_STYLE,
        )

        # Agent
        self._agent: ReActAgent | None = None

        # Running flag
        self._running = False

    def _get_history_file(self):
        """Get the path to the history file."""
        from pathlib import Path
        history_dir = Path.home() / ".agent-harness"
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir / "repl_history"

    async def run(self) -> None:
        """Run the interactive REPL."""
        self._running = True

        # Connect to database
        await db.connect()
        try:
            # Load or create initial session
            if self._initial_session_id:
                self.session = await session_manager.get(self._initial_session_id)
                if not self.session:
                    self.console.print(f"[red]Session {self._initial_session_id} not found[/red]")
                    self.session = None

            if not self.session:
                self.session = await session_manager.create(
                    title="Interactive REPL",
                    goal=None,
                    model=self.model,
                    provider=self.provider,
                    context_budget=self.context_budget,
                )

            self.console.print(Panel(
                f"[bold]AgentHarness Interactive REPL[/bold] v{__version__}\n"
                f"Session: [cyan]{self.session.id}[/cyan]\n"
                f"Model: [green]{self.model}[/green] | Provider: [green]{self.provider}[/green]\n"
                f"Type [yellow]/help[/yellow] for commands, [yellow]/exit[/yellow] to quit.",
                title="Welcome",
                border_style="cyan",
            ))
            self.console.print()

            # Main REPL loop
            while self._running:
                try:
                    # Get user input
                    user_input = await self._prompt_session.prompt_async(
                        self._get_prompt_text(),
                    )
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\n[dim]Use /exit to quit.[/dim]")
                    continue

                if not user_input.strip():
                    continue

                # Handle slash commands
                if user_input.strip().startswith("/"):
                    should_continue = await self._handle_slash_command(user_input.strip())
                    if not should_continue:
                        break
                    continue

                # Send message to agent
                await self._handle_message(user_input.strip())

        finally:
            self._running = False
            await db.close()

    def _get_prompt_text(self) -> str:
        """Get the prompt text with session info."""
        if self.session:
            short_id = str(self.session.id)[:8]
            return f"ah ({short_id}) > "
        return "ah > "

    async def _handle_slash_command(self, command: str) -> bool:
        """Handle a slash command. Returns False if the REPL should exit."""
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "/exit" or cmd == "/quit":
            self.console.print("[dim]Goodbye![/dim]")
            return False

        elif cmd == "/help":
            self._show_help()

        elif cmd == "/status":
            await self._show_status()

        elif cmd == "/sessions":
            await self._list_sessions()

        elif cmd == "/new":
            await self._new_session(args)

        elif cmd == "/switch":
            await self._switch_session(args)

        elif cmd == "/context":
            await self._show_context()

        elif cmd == "/model":
            self._set_model(args)

        elif cmd == "/provider":
            self._set_provider(args)

        elif cmd == "/budget":
            self._set_budget(args)

        elif cmd == "/verbose":
            self._toggle_verbose()

        elif cmd == "/config":
            self._show_config()

        elif cmd == "/clear":
            self.console.clear()

        else:
            self.console.print(f"[red]Unknown command: {cmd}[/red]")
            self.console.print("[dim]Type /help for available commands.[/dim]")

        return True

    def _show_help(self) -> None:
        """Show help for slash commands."""
        from rich.table import Table

        table = Table(title="Slash Commands", show_header=True, header_style="bold cyan")
        table.add_column("Command", style="yellow", no_wrap=True)
        table.add_column("Description", style="white")

        for cmd, desc in SLASH_COMMANDS.items():
            table.add_row(cmd, desc)

        self.console.print(table)
        self.console.print()

    async def _show_status(self) -> None:
        """Show current session and agent status."""
        from rich.table import Table

        if not self.session:
            self.console.print("[yellow]No active session.[/yellow]")
            return

        table = Table(title="Status", show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Session ID", str(self.session.id))
        table.add_row("Title", self.session.title or "(untitled)")
        table.add_row("Status", self.session.status)
        table.add_row("Model", self.model)
        table.add_row("Provider", self.provider)
        table.add_row("Context Budget", str(self.context_budget))
        table.add_row("Verbose", "on" if self.verbose else "off")
        table.add_row("Agent ID", self.agent_id)

        self.console.print(table)
        self.console.print()

    async def _list_sessions(self) -> None:
        """List recent sessions."""
        from rich.table import Table

        sessions = await session_manager.list_sessions(limit=10)
        if not sessions:
            self.console.print("[yellow]No sessions found.[/yellow]")
            return

        table = Table(title="Recent Sessions")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="white")
        table.add_column("Status", style="green")
        table.add_column("Model", style="dim")
        table.add_column("Last Activity", style="dim")

        for s in sessions:
            table.add_row(
                str(s.id)[:8],
                s.title or "(untitled)",
                s.status,
                s.model or self.model,
                s.last_activity.strftime("%Y-%m-%d %H:%M"),
            )

        self.console.print(table)
        self.console.print()

    async def _new_session(self, title: str = "") -> None:
        """Create a new session."""
        self.session = await session_manager.create(
            title=title or "Interactive REPL",
            goal=None,
            model=self.model,
            provider=self.provider,
            context_budget=self.context_budget,
        )
        self.console.print(f"[green]New session created:[/green] {self.session.id}")
        self.console.print()

    async def _switch_session(self, session_id: str) -> None:
        """Switch to a different session."""
        if not session_id:
            self.console.print("[yellow]Usage: /switch <session_id>[/yellow]")
            return

        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            self.console.print(f"[red]Invalid session ID: {session_id}[/red]")
            return

        session = await session_manager.get(sid)
        if not session:
            self.console.print(f"[red]Session {session_id} not found[/red]")
            return

        self.session = session
        self.console.print(f"[green]Switched to session:[/green] {session.id}")
        self.console.print()

    async def _show_context(self) -> None:
        """Show context for current session."""
        from rich.table import Table

        if not self.session:
            self.console.print("[yellow]No active session.[/yellow]")
            return

        chunks = await context_manager.get_chunks(self.session.id, limit=10)
        if not chunks:
            self.console.print("[yellow]No context chunks found.[/yellow]")
            return

        table = Table(title=f"Context (session {str(self.session.id)[:8]})")
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

        self.console.print(table)

        total_tokens = await context_manager.get_token_usage(self.session.id)
        self.console.print(f"\n[dim]Total tokens: {total_tokens}[/dim]")
        self.console.print()

    def _set_model(self, model: str) -> None:
        """Set the model."""
        if not model:
            self.console.print(f"Current model: [cyan]{self.model}[/cyan]")
            return
        self.model = model
        self.console.print(f"Model set to: [cyan]{model}[/cyan]")

    def _set_provider(self, provider: str) -> None:
        """Set the provider."""
        if not provider:
            self.console.print(f"Current provider: [cyan]{self.provider}[/cyan]")
            return
        if provider not in ("openrouter", "ollama"):
            self.console.print(f"[red]Unknown provider: {provider}. Use 'openrouter' or 'ollama'.[/red]")
            return
        self.provider = provider
        self.console.print(f"Provider set to: [cyan]{provider}[/cyan]")

    def _set_budget(self, budget: str) -> None:
        """Set the context budget."""
        if not budget:
            self.console.print(f"Current context budget: [cyan]{self.context_budget}[/cyan]")
            return
        try:
            self.context_budget = int(budget)
            self.console.print(f"Context budget set to: [cyan]{self.context_budget}[/cyan]")
        except ValueError:
            self.console.print(f"[red]Invalid budget: {budget}[/red]")

    def _toggle_verbose(self) -> None:
        """Toggle verbose mode."""
        self.verbose = not self.verbose
        self.console.print(f"Verbose mode: [cyan]{'on' if self.verbose else 'off'}[/cyan]")

    def _show_config(self) -> None:
        """Show current configuration."""
        from rich.table import Table

        table = Table(title="Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")

        config_dict = config.to_dict()
        for key, value in config_dict.items():
            table.add_row(key, str(value))

        self.console.print(table)
        self.console.print()

    async def _handle_message(self, message: str) -> None:
        """Send a message to the agent and display the streaming response."""
        if not self.session:
            self.console.print("[red]No active session. Use /new to create one.[/red]")
            return

        # Create provider and agent
        try:
            llm = get_provider(provider=self.provider, model=self.model)
        except ValueError as e:
            self.console.print(f"[red]Provider error:[/red] {e}")
            return

        agent = ReActAgent(
            provider=llm,
            max_iterations=config.get("max_iterations"),
            agent_id=self.agent_id,
        )

        # Stream response
        response_text = ""
        tool_calls_count = 0
        tokens_used = 0

        try:
            with Live(console=self.console, refresh_per_second=10, transient=False) as live:
                async for event in agent.run_stream(
                    self.session.id,
                    message,
                    verbose=self.verbose,
                ):
                    if event.type == "text":
                        response_text += event.content
                        live.update(Text(response_text, style="green"))
                    elif event.type == "tool_call":
                        tool_calls_count += 1
                        if self.verbose:
                            live.update(Text(
                                response_text + f"\n\n[yellow]→ {event.tool_name}({event.tool_args})[/yellow]",
                                style="green",
                            ))
                    elif event.type == "tool_result":
                        if self.verbose:
                            preview = str(event.tool_result)[:100].replace("\n", " ")
                            live.update(Text(
                                response_text + f"\n\n[green]← {preview}[/green]",
                                style="green",
                            ))
                    elif event.type == "token_usage":
                        tokens_used = event.tokens_used
                    elif event.type == "done":
                        response_text = event.response.content
                        tool_calls_count = len(event.response.tool_calls)
                        tokens_used = event.response.tokens_used
                        live.update(Text(response_text, style="green"))

            # Final output
            self.console.print()
            self.console.print(Panel(
                Markdown(response_text),
                title="Agent",
                border_style="green",
            ))
            self.console.print()
            if self.verbose:
                self.console.print(
                    f"[dim]Iterations: {event.response.iterations} | "
                    f"Tool calls: {tool_calls_count} | "
                    f"Tokens: {tokens_used}[/dim]"
                )
            self.console.print(f"[dim]Session ID: {self.session.id}[/dim]")
            self.console.print()

        except Exception as e:
            self.console.print(f"[red]Agent error:[/red] {e}")
            logger.exception("Agent run failed in REPL")


async def run_repl(
    model: str | None = None,
    provider: str | None = None,
    verbose: bool | None = None,
    session_id: str | None = None,
) -> None:
    """Convenience function to run the interactive REPL."""
    repl = InteractiveREPL(
        model=model,
        provider=provider,
        verbose=verbose,
        session_id=session_id,
    )
    await repl.run()
