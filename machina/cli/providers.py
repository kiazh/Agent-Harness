"""``machina providers`` — manage user-added OpenAI-compatible providers.

Commands:

* ``machina providers list`` — built-in + user-added providers with key and
  model-cache state (``--json`` for machine use).
* ``machina providers add NAME BASE_URL`` — register a provider, store its
  key through the same ``.env`` idiom ``/keys`` uses (0600, never echoed;
  ``--no-key`` for keyless local endpoints), then pull and print its
  ``/models`` catalog.
* ``machina providers remove NAME`` — remove the entry and clear its ``.env``
  key line (``--yes`` skips the confirm).
* ``machina providers models [PROVIDER]`` — print the catalog for a provider:
  the live ChatGPT/Codex catalog for ``chatgpt`` (per-model efforts + context
  windows) or a named provider's pulled ``/models`` list.

The API key is resolved at call time through the shared secret resolver
(environment -> ``*_FILE`` mount -> Vault/AWS backend), exactly like every
other Machina credential.
"""

from __future__ import annotations

import json as json_lib

import typer
from rich.markup import escape
from rich.table import Table

from machina.cli.output import console
from machina.core.config import config
from machina.core.provider.catalog import (
    cached_catalog,
    fetch_catalog_for_account,
    visible_models,
)
from machina.security import providers as store

app = typer.Typer(
    name="providers",
    help="Manage user-added OpenAI-compatible providers (name + base URL + optional key).",
    no_args_is_help=True,
)

EXIT_OK = 0
EXIT_FAILED = 1

#: Cached catalogs older than this are refreshed automatically on `models`.
MODELS_TTL_SECONDS = 3600.0


def _fmt_context(tokens: int | None) -> str:
    if not tokens:
        return "—"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1_000:
        return f"{round(tokens / 1_000)}K"
    return str(tokens)


def _provider_rows() -> list[dict[str, object]]:
    selected = str(config.get("provider") or "").lower()
    rows: list[dict[str, object]] = [
        {
            "name": name,
            "kind": "builtin",
            "base_url": None,
            "key": None,
            "models": None,
            "selected": name == selected,
        }
        for name in (
            "openrouter",
            "openai",
            "anthropic",
            "google",
            "mistral",
            "groq",
            "together",
            "deepseek",
            "xai",
            "opencode",
            "chatgpt",
            "custom",
            "ollama",
        )
    ]
    for name in store.provider_names():
        entry = store.get_provider_entry(name) or {}
        key = store.resolve_provider_key(entry)
        rows.append(
            {
                "name": name,
                "kind": "user",
                "base_url": entry.get("base_url"),
                "key": "set"
                if key
                else (
                    "keyless local"
                    if store.is_loopback_base_url(str(entry.get("base_url") or ""))
                    else "missing"
                ),
                "models": len(entry.get("models") or []),
                "selected": name == selected,
            }
        )
    return rows


@app.command("list")
def list_providers(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List built-in and user-added providers (keys are never shown)."""
    rows = _provider_rows()
    if json_output:
        typer.echo(json_lib.dumps(rows, indent=2))
        return
    table = Table(title="Providers")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Base URL")
    table.add_column("Key")
    table.add_column("Models", justify="right")
    for row in rows:
        name = str(row["name"]) + (" (current)" if row["selected"] else "")
        table.add_row(
            escape(name),
            str(row["kind"]),
            escape(str(row["base_url"] or "—")),
            escape(str(row["key"] or "—")),
            str(row["models"] if row["models"] is not None else "—"),
        )
    console.print(table)
    console.print(
        "[dim]User-added keys live in the git-ignored .env (0600) and are resolved like"
        " every other credential (environment -> *_FILE -> Vault/AWS).[/dim]"
    )


@app.command("add")
def add_provider(
    name: str = typer.Argument(..., help="Provider name (lowercase letters, digits, dashes)."),
    base_url: str = typer.Argument(
        ..., help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1."
    ),
    key: str = typer.Option(
        "", "--key", help="API key (prefer the hidden prompt; this lands in shell history)."
    ),
    no_key: bool = typer.Option(
        False, "--no-key", help="Keyless endpoint (e.g. local vLLM/LM Studio)."
    ),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Register without pulling /models."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register a provider, store its key, and pull its model catalog."""
    if no_key and key:
        console.print("[red]--no-key and --key are mutually exclusive.[/red]")
        raise typer.Exit(EXIT_FAILED)
    api_key = key.strip()
    if not api_key and not no_key:
        api_key = typer.prompt(
            "API key (leave empty for keyless local endpoints)", default="", hide_input=True
        ).strip()
    try:
        entry = store.add_provider(name, base_url, api_key=api_key or None)
    except store.ProviderStoreError as error:
        console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(EXIT_FAILED) from None
    except (OSError, ValueError) as error:
        console.print(f"[red]Could not save provider ({type(error).__name__}).[/red]")
        raise typer.Exit(EXIT_FAILED) from None
    summary: dict[str, object] = {
        "provider": name.lower(),
        "baseUrl": entry["base_url"],
        "apiKeyEnv": entry["api_key_env"],
        "keySet": bool(store.resolve_provider_key(entry)),
        "models": [],
        "fetchError": None,
    }
    if not no_fetch:
        resolved = store.resolve_provider_key(entry)
        try:
            pulled = store.fetch_models(str(entry["base_url"]), api_key=resolved)
            models = store.save_models(name, pulled) if pulled else []
            summary["models"] = [row["id"] for row in models]
        except Exception as error:  # ProviderStoreError + transport, reported honestly
            summary["fetchError"] = str(error) or type(error).__name__
    if json_output:
        typer.echo(json_lib.dumps(summary, indent=2))
        raise typer.Exit(EXIT_FAILED if summary["fetchError"] else EXIT_OK)
    console.print(
        f"Provider [bold]{escape(str(summary['provider']))}[/bold] saved at {escape(str(entry['base_url']))}."
    )
    if summary["keySet"]:
        console.print(
            f"[dim]Key stored in .env as {entry['api_key_env']} (0600, never shown).[/dim]"
        )
    if summary["fetchError"]:
        console.print(f"[yellow]Model fetch failed: {escape(str(summary['fetchError']))}[/yellow]")
        console.print(
            f"Fix the URL/key, then retry: [bold]machina providers models {escape(str(summary['provider']))} --refresh[/bold]"
        )
        raise typer.Exit(EXIT_FAILED)
    if not no_fetch:
        ids = summary["models"]
        count = len(ids) if isinstance(ids, list) else 0
        console.print(f"{count} model(s) pulled.")
        if isinstance(ids, list) and ids:
            preview = ", ".join(str(identifier) for identifier in ids[:8])
            console.print(f"[dim]{escape(preview)}{'…' if count > 8 else ''}[/dim]")
    console.print(
        f"Use it with [bold]machina --provider {escape(str(summary['provider']))} --model <id>[/bold]"
        " or save the default in /provider once the UI is open."
    )


@app.command("remove")
def remove_provider(
    name: str = typer.Argument(..., help="Provider name to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove a user-added provider; its .env key line is cleared."""
    normalized = name.strip().lower()
    if normalized not in store.provider_names():
        console.print(f"[red]Unknown user-added provider: {escape(normalized)}[/red]")
        raise typer.Exit(EXIT_FAILED)
    if not yes and not typer.confirm(f"Remove provider {normalized!r} and clear its key entry?"):
        raise typer.Exit(EXIT_OK)
    removed = store.remove_provider(normalized)
    if not removed:
        console.print(f"[red]Could not remove {escape(normalized)}.[/red]")
        raise typer.Exit(EXIT_FAILED)
    console.print(f"Provider {escape(normalized)} removed.")


@app.command("models")
def models(
    provider: str = typer.Argument(None, help="Provider name (default: the configured provider)."),
    refresh: bool = typer.Option(False, "--refresh", help="Force a live fetch."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print a provider's model catalog (chatgpt: live, with efforts)."""
    selected = (provider or str(config.get("provider") or "openrouter")).strip().lower()
    if selected == "chatgpt":
        _chatgpt_models_cmd(refresh=refresh, json_output=json_output)
        return
    if store.is_named_provider(selected):
        _named_models_cmd(selected, refresh=refresh, json_output=json_output)
        return
    console.print(
        f"[yellow]Provider '{escape(selected)}' has no catalog surface here.[/yellow] "
        "Use it with an explicit model id, or list chatgpt/user-added providers: "
        "[bold]machina providers models chatgpt[/bold] · [bold]machina providers list[/bold]"
    )


def _chatgpt_models_cmd(*, refresh: bool, json_output: bool) -> None:
    try:
        fetched = fetch_catalog_for_account(force=refresh)
        source = "live"
        fetch_error = None
    except Exception as error:
        fetched = cached_catalog()
        source = fetched[0].source if fetched else "none"
        fetch_error = str(error) or type(error).__name__
    visible = visible_models(fetched)
    if json_output:
        typer.echo(
            json_lib.dumps(
                {
                    "provider": "chatgpt",
                    "source": source,
                    "fetchError": fetch_error,
                    "models": [model.to_dict() for model in visible],
                },
                indent=2,
            )
        )
        return
    if not visible:
        console.print(
            f"[yellow]No ChatGPT catalog available ({escape(str(fetch_error))}).[/yellow]"
        )
        raise typer.Exit(EXIT_FAILED)
    table = Table(title=f"ChatGPT models ({source})")
    table.add_column("Model")
    table.add_column("Efforts")
    table.add_column("Default")
    table.add_column("Context", justify="right")
    table.add_column("Max", justify="right")
    for model in visible:
        table.add_row(
            model.slug,
            "/".join(model.supported_efforts) or "—",
            model.default_effort or "—",
            _fmt_context(model.context_window),
            _fmt_context(model.max_context_window),
        )
    console.print(table)
    if fetch_error:
        console.print(
            f"[yellow]Live fetch failed, showing {source}: {escape(fetch_error)}[/yellow]"
        )
    console.print(
        "[dim]Per-model efforts come from each model's catalog entry; ultra is a Codex-client"
        " tier the Responses transport rejects, so it is not offered.[/dim]"
    )


def _named_models_cmd(provider: str, *, refresh: bool, json_output: bool) -> None:
    entry = store.get_provider_entry(provider)
    if entry is None:
        console.print(f"[red]Unknown provider: {escape(provider)}[/red]")
        raise typer.Exit(EXIT_FAILED)
    age = store.pulled_at_age_seconds(entry)
    stale = age is None or age >= MODELS_TTL_SECONDS
    fetch_error = None
    models = list(entry.get("models") or [])
    if refresh or stale:
        try:
            key = store.resolve_provider_key(entry)
            pulled = store.fetch_models(str(entry.get("base_url") or ""), api_key=key)
            models = store.save_models(provider, pulled) if pulled else []
        except Exception as error:
            fetch_error = str(error) or type(error).__name__
    if json_output:
        typer.echo(
            json_lib.dumps(
                {
                    "provider": provider,
                    "baseUrl": entry.get("base_url"),
                    "fetchError": fetch_error,
                    "models": models,
                },
                indent=2,
            )
        )
        return
    if not models:
        console.print(
            f"[yellow]No models for {escape(provider)}"
            f"{f' ({escape(str(fetch_error))})' if fetch_error else ''}.[/yellow]"
        )
        raise typer.Exit(EXIT_FAILED)
    table = Table(title=f"{provider} models")
    table.add_column("Model")
    table.add_column("Context", justify="right")
    for row in models:
        table.add_row(str(row.get("id")), _fmt_context(row.get("contextWindow")))
    console.print(table)
    if fetch_error:
        console.print(
            f"[yellow]Live refresh failed, showing the cached list: {escape(fetch_error)}[/yellow]"
        )
    console.print(f"[dim]Base URL: {escape(str(entry.get('base_url')))}[/dim]")
