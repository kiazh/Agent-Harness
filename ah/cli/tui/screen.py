"""Interactive screen — the pi-style coding-agent REPL.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Composes a scrolling transcript, a multiline editor, and a footer into a
``VStack`` driven by :class:`TuiMainScreen`, and streams agent responses from
``ReActAgent.run_stream`` into message/tool widgets. Mirrors the existing
``run_repl`` entry contract so it drops into ``ah chat -i`` / ``ah repl``.
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid

from ah.cli.tui.component import Container
from ah.cli.tui.components import Editor, Loader, Spacer
from ah.cli.tui.renderer import TuiMainScreen
from ah.cli.tui.terminal import ProcessTerminal
from ah.cli.tui.theme import get_theme
from ah.cli.tui.widgets import AssistantMessage, Footer, ToolExecution, UserMessage

from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.provider import get_provider
from ah.core.session import Session, session_manager
from ah.db.connection import db
from ah.tools import builtins  # noqa: F401 — registers built-in tools

__all__ = ["InteractiveScreen", "run_tui"]


def _git_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


class InteractiveScreen:
    """pi-style interactive REPL on top of the ported TUI."""

    def __init__(
        self,
        model: str | None = None,
        provider: str | None = None,
        verbose: bool | None = None,
        session_id: str | None = None,
        skin: str = "dark",
    ) -> None:
        self.model = model or config.get("model")
        self.provider = provider or config.get("provider")
        self.verbose = config.get("verbose") if verbose is None else verbose
        self.context_budget = config.get("context_budget")
        self.agent_id = config.get("agent_id")
        self._initial_session_id = session_id

        theme_name = skin if skin in ("dark", "light") else "dark"
        self.theme = get_theme(theme_name)

        self.session: Session | None = None
        self._running = False
        self._tokens = 0

        # Components
        self.terminal = ProcessTerminal(alt_screen=False)
        self.tui = TuiMainScreen(self.terminal)
        self.transcript = Container()
        self.editor = Editor(border_fn=lambda s: self.theme.style(s, "border"))
        self.footer = Footer(self.theme)
        self._loader = Loader(
            "Thinking...",
            spinner_fn=lambda s: self.theme.style(s, "accent"),
            message_fn=lambda s: self.theme.style(s, "muted"),
        )

    # ─── lifecycle ───────────────────────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        await db.connect()
        try:
            await self._init_session()
            self._compose()
            self._show_welcome()
            self.tui.set_focus(self.editor)
            self.terminal.start(lambda d: None, lambda: None)
            self.tui.start()
            await self._input_loop()
        finally:
            self._running = False
            self.tui.stop()
            await db.close()

    async def _init_session(self) -> None:
        if self._initial_session_id:
            try:
                sid = uuid.UUID(self._initial_session_id)
                self.session = await session_manager.get(sid)
            except ValueError:
                self.session = None
        if not self.session:
            self.session = await session_manager.create(
                title="Interactive (TUI)",
                goal=None,
                model=self.model,
                provider=self.provider,
                context_budget=self.context_budget,
            )
        self._refresh_footer()

    def _compose(self) -> None:
        self.tui.add_child(self.transcript)
        self.tui.add_child(Spacer(1))
        self.tui.add_child(self.editor)
        self.tui.add_child(self.footer)

    def _show_welcome(self) -> None:
        self.transcript.add_child(AssistantMessage(
            "**AgentHarness** — interactive mode. Type a message and press Enter. "
            "Use `/exit` to quit.", self.theme,
        ))
        self.transcript.add_child(Spacer(1))

    def _refresh_footer(self) -> None:
        self.footer.update(
            session_id=str(self.session.id) if self.session else "",
            model=self.model,
            tokens=self._tokens,
            branch=_git_branch(),
            mode="Auto",
        )

    # ─── input loop ──────────────────────────────────────────────────────
    async def _input_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while self._running:
            key = await loop.run_in_executor(None, self.terminal.read_key)
            if key is None:
                break
            if key in ("\r", "\n"):
                text = self.editor.get_value().strip()
                self.editor.set_value("")
                if not text:
                    self.tui.request_render()
                    continue
                if text.startswith("/"):
                    if not await self._handle_command(text):
                        break
                    continue
                await self._handle_message(text)
            elif key == "\x03":  # Ctrl+C
                break
            else:
                self.editor.handle_input(key)
                self.tui.request_render()

    async def _handle_command(self, text: str) -> bool:
        cmd = text[1:].split()[0].lower() if len(text) > 1 else ""
        if cmd in ("exit", "quit"):
            return False
        if cmd == "clear":
            self.transcript.clear()
            self.tui.request_render()
        else:
            self.transcript.add_child(AssistantMessage(f"Unknown command: `{text}`", self.theme))
            self.tui.request_render()
        return True

    async def _handle_message(self, text: str) -> None:
        if not self.session:
            return
        # User turn
        self.transcript.add_child(UserMessage(text, self.theme))
        self.transcript.add_child(Spacer(1))

        assistant = AssistantMessage("", self.theme)
        self.transcript.add_child(assistant)
        self._loader.start()
        self.transcript.add_child(self._loader)
        self.tui.request_render()

        try:
            llm = get_provider(provider=self.provider, model=self.model)
        except ValueError as e:
            assistant.set_text(self.theme.style(f"Provider error: {e}", "error"))
            self.transcript.remove_child(self._loader)
            self._loader.stop()
            self.tui.request_render()
            return

        agent = ReActAgent(provider=llm, max_iterations=config.get("max_iterations"), agent_id=self.agent_id)
        tool_cards: dict[str, ToolExecution] = {}
        try:
            async for event in agent.run_stream(self.session.id, text, verbose=self.verbose):
                if event.type == "text":
                    self._loader.advance()
                    assistant.append(event.content)
                elif event.type == "tool_call":
                    self._loader.stop()
                    self.transcript.remove_child(self._loader)
                    card = ToolExecution(event.tool_name, self.theme, "")
                    card.set_running()
                    tool_cards[event.tool_name] = card
                    self.transcript.add_child(card)
                elif event.type == "tool_result":
                    card = tool_cards.get(event.tool_name)
                    if card:
                        card.set_result(str(event.tool_result)[:400])
                elif event.type == "token_usage":
                    self._tokens = event.tokens_used
                    self._refresh_footer()
                elif event.type == "done":
                    assistant.set_text(event.response.content)
                    self._tokens = event.response.tokens_used
                    self._refresh_footer()
                self.tui.request_render()
        except Exception as e:
            assistant.set_text(self.theme.style(f"Agent error: {e}", "error"))
        finally:
            self._loader.stop()
            self.transcript.remove_child(self._loader)
            self.transcript.add_child(Spacer(1))
            self.tui.request_render()


async def run_tui(
    model: str | None = None,
    provider: str | None = None,
    verbose: bool | None = None,
    session_id: str | None = None,
    skin: str = "dark",
) -> None:
    """Entry point matching run_repl's signature."""
    screen = InteractiveScreen(
        model=model, provider=provider, verbose=verbose,
        session_id=session_id, skin=skin,
    )
    await screen.run()
