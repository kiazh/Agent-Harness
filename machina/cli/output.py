"""Plain Rich output helpers for the non-interactive ``machina`` commands.

The interactive UI lives in ``ui/`` (TypeScript). These helpers only cover the
one-shot admin commands: status lines, tables, errors and spinners.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from machina.core.config import SECRET_KEYS

__all__ = [
    "console",
    "success",
    "error",
    "muted",
    "status_line",
    "spinner",
    "sessions_table",
    "context_table",
    "skills_table",
    "config_table",
    "memory_table",
]

console = Console()

_LEVEL_STYLES = {"success": "green", "error": "red", "warning": "yellow", "info": "cyan"}


def success(message: str) -> None:
    """Print a success line; *message* is user text and rendered literally."""
    console.print(f"[bold green]✓[/bold green] {escape(str(message))}")


def muted(message: str) -> None:
    """Print a dim line; *message* is user text and rendered literally."""
    console.print(escape(str(message)), style="dim")


def error(message: str, suggestion: str = "") -> None:
    """Print an error, with an optional next-step suggestion.

    Both parts are user text and rendered literally (a ``[/b]`` in a
    session title must not raise Rich's MarkupError).
    """
    console.print(f"[bold red]✗[/bold red] {escape(str(message))}")
    if suggestion:
        console.print(f"  {escape(str(suggestion))}", style="dim")


def status_line(label: str, value: str, level: str = "info") -> None:
    """Print a labeled status line; label and value render literally."""
    style = _LEVEL_STYLES.get(level, "white")
    console.print(f"  {escape(str(label))}: [{style}]{escape(str(value))}[/{style}]")


@contextmanager
def spinner(label: str) -> Iterator[None]:
    """Show a transient spinner while the body runs (no-op when not a TTY)."""
    with console.status(label, spinner="dots"):
        yield


def _ts(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def sessions_table(sessions: list[Any]) -> None:
    table = Table(title=f"Sessions ({len(sessions)})", header_style="bold cyan")
    for col in ("ID", "Title", "Status", "Agent", "Goal", "Last Activity"):
        table.add_column(col, no_wrap=col == "ID")
    for s in sessions:
        table.add_row(
            escape(str(s.id))[:8],
            escape(s.title or "(untitled)"),
            escape(s.status),
            escape(s.agent_id),
            escape((s.goal or "")[:40]),
            _ts(s.last_activity),
        )
    console.print(table)


def context_table(chunks: list[Any], session_id: Any) -> None:
    table = Table(title=f"Context ({escape(str(session_id))[:8]})", header_style="bold cyan")
    for col in ("Type", "Agent", "Tokens", "Created"):
        table.add_column(col)
    for c in chunks:
        table.add_row(
            escape(c.chunk_type),
            escape(c.agent_id),
            escape(str(c.token_count)),
            _ts(c.created_at),
        )
    console.print(table)


def skills_table(skills: list[Any]) -> None:
    table = Table(title=f"Skills ({len(skills)})", header_style="bold cyan")
    for col in ("Name", "Description", "Triggers", "Uses", "Views", "Last Activity"):
        table.add_column(col)
    for s in skills:
        table.add_row(
            escape(s.name),
            escape((s.description or "")[:60]),
            escape(", ".join(s.triggers[:3])),
            escape(str(s.usage_count)),
            escape(str(s.view_count)),
            _ts(s.last_activity_at),
        )
    console.print(table)


def config_table(values: dict[str, Any]) -> None:
    """Print configuration, masking secret values."""
    table = Table(title="Configuration", header_style="bold cyan")
    table.add_column("Key")
    table.add_column("Value")
    for key, value in values.items():
        if key in SECRET_KEYS:
            shown = "[green]set[/green]" if value else "[dim]not set[/dim]"
        else:
            shown = escape(str(value))
        table.add_row(escape(str(key)), shown)
    console.print(table)


def memory_table(memories: list[Any]) -> None:
    table = Table(title=f"Memories ({len(memories)})", header_style="bold cyan")
    for col in ("ID", "Category", "Importance", "Content"):
        table.add_column(col, no_wrap=col == "ID")
    for m in memories:
        table.add_row(
            escape(str(m.id))[:8],
            escape(m.category),
            escape(f"{m.importance:.2f}"),
            escape(m.content[:80]),
        )
    console.print(table)
