"""Plain Rich output helpers for the non-interactive ``ah`` commands.

The interactive UI lives in ``ui/`` (TypeScript). These helpers only cover the
one-shot admin commands: status lines, tables, errors and spinners.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from ah.core.config import SECRET_KEYS

__all__ = [
    "console",
    "success",
    "error",
    "warning",
    "muted",
    "status_line",
    "spinner",
    "response_panel",
    "sessions_table",
    "context_table",
    "skills_table",
    "config_table",
    "memory_table",
]

console = Console()

_LEVEL_STYLES = {"success": "green", "error": "red", "warning": "yellow", "info": "cyan"}


def success(message: str) -> None:
    console.print(f"[bold green]✓[/bold green] {message}")


def warning(message: str) -> None:
    console.print(f"[bold yellow]![/bold yellow] {message}")


def muted(message: str) -> None:
    console.print(message, style="dim")


def error(message: str, suggestion: str = "") -> None:
    """Print an error, with an optional next-step suggestion."""
    console.print(f"[bold red]✗[/bold red] {message}")
    if suggestion:
        console.print(f"  {suggestion}", style="dim")


def status_line(label: str, value: str, level: str = "info") -> None:
    style = _LEVEL_STYLES.get(level, "white")
    console.print(f"  {label}: [{style}]{value}[/{style}]")


@contextmanager
def spinner(label: str) -> Iterator[None]:
    """Show a transient spinner while the body runs (no-op when not a TTY)."""
    with console.status(label, spinner="dots"):
        yield


def response_panel(text: str, title: str = "Agent") -> None:
    console.print(
        Panel(
            Markdown(text or "_(no response)_"),
            title=title,
            title_align="left",
            border_style="cyan",
        )
    )


def _ts(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def sessions_table(sessions: list[Any]) -> None:
    table = Table(title=f"Sessions ({len(sessions)})", header_style="bold cyan")
    for col in ("ID", "Title", "Status", "Agent", "Goal", "Last Activity"):
        table.add_column(col, no_wrap=col == "ID")
    for s in sessions:
        table.add_row(
            str(s.id)[:8],
            s.title or "(untitled)",
            s.status,
            s.agent_id,
            (s.goal or "")[:40],
            _ts(s.last_activity),
        )
    console.print(table)


def context_table(chunks: list[Any], session_id: Any) -> None:
    table = Table(title=f"Context ({str(session_id)[:8]})", header_style="bold cyan")
    for col in ("Type", "Agent", "Tokens", "Created"):
        table.add_column(col)
    for c in chunks:
        table.add_row(c.chunk_type, c.agent_id, str(c.token_count), _ts(c.created_at))
    console.print(table)


def skills_table(skills: list[Any]) -> None:
    table = Table(title=f"Skills ({len(skills)})", header_style="bold cyan")
    for col in ("Name", "Description", "Triggers", "Uses", "Views", "Last Activity"):
        table.add_column(col)
    for s in skills:
        table.add_row(
            s.name,
            (s.description or "")[:60],
            ", ".join(s.triggers[:3]),
            str(s.usage_count),
            str(s.view_count),
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
            shown = str(value)
        table.add_row(key, shown)
    console.print(table)


def memory_table(memories: list[Any]) -> None:
    table = Table(title=f"Memories ({len(memories)})", header_style="bold cyan")
    for col in ("ID", "Category", "Importance", "Content"):
        table.add_column(col, no_wrap=col == "ID")
    for m in memories:
        table.add_row(str(m.id)[:8], m.category, f"{m.importance:.2f}", m.content[:80])
    console.print(table)
