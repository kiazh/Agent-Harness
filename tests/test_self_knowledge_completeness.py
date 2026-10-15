"""Structural completeness pins for the always-on capability brief.

``machina/core/self_knowledge.py`` is prompt text, so these tests stay
structural rather than asserting prose snapshots:

1. every snake_case token in FEATURES is a real configuration key
   (``DEFAULTS``/``SECRET_KEYS``) or a registered tool name — the brief never
   invents an identifier;
2. every ``machina <command>`` reference written in backticks names a real
   CLI command or command group (the word directly after ``machina``; piped
   subcommand lists such as ``status|enable|run`` are not validated here);
3. every TUI slash command registered in ``ui/src/features/*.ts`` is either
   named in the brief (``/<name>``) or explicitly allowlisted below with a
   reason, so the brief cannot silently fall behind the UI surface; and the
   reverse — every ``/command`` named in the brief must be registered.

The TUI allowlist is deliberately narrow: only terminal housekeeping or
read-only transcript viewers may appear there.
"""

from __future__ import annotations

import re
from pathlib import Path

from machina.core.config import DEFAULTS, SECRET_KEYS
from machina.core.self_knowledge import FEATURES

REPO_ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = REPO_ROOT / "ui" / "src" / "features"

#: TUI commands intentionally not named in the brief, with the reason they do
#: not need to be (terminal housekeeping and read-only viewers only).
TUI_ALLOWLIST = {
    "clear": "terminal housekeeping: clears the screen and starts a new chat",
    "exit": "terminal housekeeping: quits the UI",
    "output": "read-only viewer for tool output already retained in the transcript",
}

_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_MACHINA_REF = re.compile(r"`[^`]*?machina ([a-z][a-z0-9-]*)")
_TUI_COMMAND = re.compile(r'name:\s*"([a-z][a-z0-9-]*)"\s*,\s*\n\s*description:')

#: Commands the extraction must always see — guards the regex above against
#: silently matching nothing after a UI refactor.
_TUI_CANARIES = {
    "theme",
    "harnesses",
    "skills",
    "jobs",
    "sessions",
    "models",
    "keys",
    "mcp",
    "mode",
    "usage",
    "persona",
    "growth",
    "profile",
    "interop",
}


def _facts_text() -> str:
    return "\n".join(FEATURES.values())


def _registered_tui_commands() -> set[str]:
    sources = sorted(FEATURES_DIR.glob("*.ts"))
    assert sources, f"TUI feature sources not found under {FEATURES_DIR}"
    registered: set[str] = set()
    for source in sources:
        registered |= set(_TUI_COMMAND.findall(source.read_text(encoding="utf-8")))
    assert _TUI_CANARIES <= registered, (
        f"TUI command extraction regressed; missing canaries: {sorted(_TUI_CANARIES - registered)}"
    )
    return registered


def _registered_cli_names() -> set[str]:
    import machina.cli as cli

    commands = set()
    for command in cli.app.registered_commands:
        name = command.name or (
            command.callback.__name__.replace("_", "-") if command.callback else None
        )
        if name:
            commands.add(name)
    commands |= {group.name for group in cli.app.registered_groups if group.name}
    return commands


def test_every_snake_case_token_is_a_config_key_or_registered_tool():
    from machina.tools.base import registry

    known = set(DEFAULTS) | set(SECRET_KEYS) | set(registry.list_tools())
    unknown = sorted({token for token in _SNAKE.findall(_facts_text()) if token not in known})
    assert not unknown, (
        "FEATURES names identifiers that are neither config keys nor tools; "
        f"fix the entry text or the identifier: {unknown}"
    )


def test_every_backticked_machina_cli_reference_exists():
    referenced = sorted(set(_MACHINA_REF.findall(_facts_text())))
    assert {"config-set", "harnesses", "mode", "sessions", "usage"} <= set(referenced), (
        "CLI reference extraction regressed; expected the core command "
        f"surfaces to be named in the brief, saw {referenced}"
    )
    missing = sorted(name for name in referenced if name not in _registered_cli_names())
    assert not missing, f"FEATURES names CLI commands that do not exist: {missing}"


def test_every_tui_feature_command_is_covered_or_allowlisted():
    registered = _registered_tui_commands()

    text = _facts_text()
    uncovered = []
    for name in sorted(registered - set(TUI_ALLOWLIST)):
        if not re.search(r"(?<![\w/])/" + re.escape(name) + r"(?![\w-])", text):
            uncovered.append(name)
    assert not uncovered, (
        f"TUI commands are neither named in FEATURES nor allowlisted with a reason: {uncovered}"
    )


def test_every_slash_command_named_in_the_brief_is_registered():
    registered = _registered_tui_commands()
    mentioned = set(re.findall(r"(?<![\w/])/([a-z][a-z0-9-]*)", _facts_text()))
    assert {"theme", "harnesses", "skills", "jobs", "sessions"} <= mentioned, (
        "slash-command extraction regressed; expected the campaign surfaces in "
        f"the brief, saw {sorted(mentioned)}"
    )
    unknown = sorted(mentioned - registered)
    assert not unknown, f"FEATURES names TUI commands the UI does not register: {unknown}"


def test_self_change_entry_states_the_self_modification_stance():
    entry = FEATURES.get("self_change", "")
    assert "may change this installation" in entry, "the brief must grant the capability"
    assert "permissions" in entry, "self-change stays inside the permission model"
    assert "asked, not guessed" in entry, "user-owned choices must be requested"


def test_facts_are_single_line_entries():
    for name, description in FEATURES.items():
        assert isinstance(description, str) and description.strip()
        assert "\n" not in description, f"FEATURES[{name!r}] must stay one terse line"
