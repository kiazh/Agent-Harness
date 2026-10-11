"""Session-scoped CLI management of persistent personality and memory learning."""

import json
from pathlib import Path

import typer

from ah.cli.output import console

app = typer.Typer(
    help="Inspect personality, teach notes, and train memory decisions with feedback."
)


def _execute(session_id, operation):
    from ah.cli import _resolve_session, _run
    from ah.db.connection import db
    from ah.memory.growth import growth_store

    async def run():
        await db.connect()
        try:
            await db.initialize_growth_schema()
            session = await _resolve_session(session_id)
            result = await operation(growth_store, session)
            console.print_json(json.dumps(result))
        finally:
            await db.close()

    try:
        _run(run())
    except ValueError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None


@app.command()
def show(session: str = typer.Option(None, "--session", "-s")):
    """Show learned notes, policy weights and recent decision IDs."""

    async def inspect(store, current):
        return {
            "growth": await store.get(current.agent_id),
            "episodes": await store.episodes(current.agent_id, current.id),
            "rsi": await store.rsi_status(current.agent_id),
        }

    _execute(session, inspect)


@app.command()
def teach(kind: str, key: str, content: str, session: str = typer.Option(None, "--session", "-s")):
    """Teach a preference, style request or verified lesson."""

    async def update(store, current):
        from ah.memory.growth import require_growth_enabled

        require_growth_enabled()
        return {"notes": await store.teach(current.agent_id, kind, key, content)}

    _execute(session, update)


@app.command()
def feedback(episode: str, reward: float, session: str = typer.Option(None, "--session", "-s")):
    """Rate a completed memory decision (-1 to +1), once per turn."""
    import uuid

    episode_id = uuid.UUID(episode)

    async def rate(store, current):
        from ah.memory.growth import require_growth_enabled

        require_growth_enabled()
        return {"accepted": await store.feedback(current.agent_id, episode_id, reward, current.id)}

    _execute(session, rate)


@app.command("import-policy")
def import_policy(path: Path, session: str = typer.Option(None, "--session", "-s")):
    """Install validated weights produced by the existing offline RL trainer."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    async def install(store, current):
        from ah.memory.growth import require_growth_enabled

        require_growth_enabled()
        await store.install_policy(current.agent_id, payload)
        return {"installed": True}

    _execute(session, install)


@app.command()
def forget(key: str, session: str = typer.Option(None, "--session", "-s")):
    """Remove one learned note using its kind:key name."""

    async def remove(store, current):
        return {"removed": await store.forget(current.agent_id, key)}

    _execute(session, remove)


@app.command()
def rollback(revision: int, session: str = typer.Option(None, "--session", "-s")):
    """Restore an owned persona/policy revision from the retained history."""

    async def restore(store, current):
        return {"restored": await store.rollback(current.agent_id, revision)}

    _execute(session, restore)


@app.command("export-policy")
def export_policy(path: Path, session: str = typer.Option(None, "--session", "-s")):
    """Export live weights in the offline trainer's JSON format."""

    async def export(store, current):
        payload = (await store.get(current.agent_id))["policy"]
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {"exported": str(path)}

    _execute(session, export)
