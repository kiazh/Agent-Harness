"""``machina interop`` — detect external agent harnesses and set up Machina.

Surfaces the ``machina.interop`` module: which of OpenCode / Codex / Hermes
are installed, running, and registered to reach Machina's MCP server
(``python -m machina.mcp_server``), plus idempotent enable/disable.

Status-surface decision (interop wave, 2026-10-14): this feature is
deliberately NOT folded into ``services.format_status_lines`` or the
``machina status`` rendering. The shared (label, value) subset is pinned by
``tests/test_mcp_server.py`` as a stability contract for two consumers (CLI +
MCP shim), and detection spawns platform CLIs — a cost that does not belong in
the general status path. ``machina interop status`` is the interop surface.

Exit codes: 0 ok, 1 action failed, 2 usage / unknown platform.
"""

from __future__ import annotations

import shlex

import typer
from rich.table import Table

from machina import interop
from machina.cli.output import console

app = typer.Typer(
    help="Detect external agent harnesses (OpenCode, Codex, Hermes) and "
    "register or remove Machina's MCP server for them.",
    no_args_is_help=True,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

_PLATFORM_HELP = "Platform: opencode | codex | hermes"


def _version_cell(version: str | None) -> str:
    if not version:
        return "—"
    return version if len(version) <= 44 else version[:41] + "..."


def _registered_cell(result: interop.DetectResult) -> str:
    if result.scopes:
        return "+".join(result.scopes)
    return "listed" if result.registered else "no"


def run_status() -> int:
    """Render the interop detection table and hints; return an exit code."""
    try:
        results = interop.detect()
    except Exception as error:  # detection must never hard-fail the command
        console.print(f"[red]Interop detection failed:[/red] {error}")
        return EXIT_FAILED

    table = Table(title=f"External agent harnesses ({len(results)})")
    table.add_column("Platform", style="cyan", no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    table.add_column("Version", no_wrap=True)
    table.add_column("Running", no_wrap=True)
    table.add_column("Registered", no_wrap=True)
    table.add_column("Enabled", no_wrap=True)

    def _flag(value: bool, ok_style: str = "green") -> str:
        return f"[{ok_style}]yes[/{ok_style}]" if value else "[dim]no[/dim]"

    hints: list[str] = []
    for key, result in results.items():
        enabled = result.enabled_cell
        enabled_style = {"yes": "green", "manual": "yellow"}.get(enabled, "dim")
        table.add_row(
            result.label,
            _flag(result.installed),
            _version_cell(result.version),
            _flag(result.running),
            _registered_cell(result),
            f"[{enabled_style}]{enabled}[/{enabled_style}]",
        )
        hints.append(_hint_for(key, result))

    console.print(table)
    console.print("[dim]Machina MCP server:[/dim] " + shlex.join(interop.server_command()))
    actionable = [hint for hint in hints if hint]
    for hint in actionable:
        console.print(f"[dim]· {hint}[/dim]")
    if actionable:
        console.print(
            "[dim]Enable other harnesses with `machina interop enable <platform>`; "
            "see `machina interop --help`.[/dim]"
        )
    return EXIT_OK


def _hint_for(key: str, result: interop.DetectResult) -> str:
    if not result.installed:
        return f"{result.label}: not installed — install `{result.platform}` to enable Machina tools there."
    if result.enabled is None and result.registered:
        return (
            f"{result.label}: registered outside Machina's state — "
            f"`machina interop enable {key}` records it without changing anything."
        )
    if result.registered and result.enabled:
        scope = f" ({result.state_scope} scope)" if result.state_scope else ""
        return f"{result.label}: registered{scope}."
    if not result.registered:
        scope_hint = (
            "(project or --scope global)"
            if "project" in interop.PLATFORMS[key].supports_scopes
            else "(user config)"
        )
        return f"{result.label}: not registered — `machina interop enable {key}` {scope_hint}."
    return ""


def run_enable(platform: str, *, scope: str = "project", dry_run: bool = False) -> int:
    """Run enable and print the actions; return 0/1/2."""
    normalized = (scope or "project").strip().lower()
    if normalized not in ("project", "global"):
        console.print("[red]Invalid --scope. Use project|global.[/red]")
        return EXIT_USAGE
    try:
        result = interop.enable(platform, scope=normalized, dry_run=dry_run)
    except interop.UnknownPlatform as error:
        console.print(f"[red]{error}[/red]")
        return EXIT_USAGE
    for step in result.steps:
        console.print(f"  [dim]{step}[/dim]")
    style = "green" if result.ok else "red"
    console.print(f"[{style}]{result.message}[/{style}]")
    if result.verify:
        mark = "[green]✓[/green]" if result.verify.get("ok") else "[red]✗[/red]"
        console.print(f"  {mark} verify: {result.verify.get('detail', '')}")
    return EXIT_OK if result.ok else EXIT_FAILED


def run_disable(platform: str) -> int:
    """Run disable and print the actions; return 0/1/2."""
    try:
        result = interop.disable(platform)
    except interop.UnknownPlatform as error:
        console.print(f"[red]{error}[/red]")
        return EXIT_USAGE
    for step in result.steps:
        console.print(f"  [dim]{step}[/dim]")
    style = "green" if result.ok else "yellow"
    console.print(f"[{style}]{result.message}[/{style}]")
    if result.verify:
        mark = "[green]✓[/green]" if result.verify.get("ok") else "[red]✗[/red]"
        console.print(f"  {mark} verify: {result.verify.get('detail', '')}")
    return EXIT_OK if result.ok else EXIT_FAILED


@app.command()
def status() -> None:
    """Show which harnesses are installed, running, registered, and enabled."""
    raise typer.Exit(run_status())


@app.command()
def enable(
    platform: str = typer.Argument(..., help=_PLATFORM_HELP),
    scope: str = typer.Option(
        "project", "--scope", help="Registration scope: project|global (Codex/Hermes: global only)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the exact command without running it"
    ),
) -> None:
    """Register Machina's MCP server for a harness (idempotent)."""
    raise typer.Exit(run_enable(platform, scope=scope, dry_run=dry_run))


@app.command()
def disable(
    platform: str = typer.Argument(..., help=_PLATFORM_HELP),
) -> None:
    """Remove Machina's MCP server registration (manual where unsupported)."""
    raise typer.Exit(run_disable(platform))
