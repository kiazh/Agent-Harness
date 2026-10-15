"""R4-COMPLY: the removed subscription flows cannot silently return.

Owner-directed removals (2026-10-14):
* Gemini Code Assist OAuth (the gemini-cli Cloud Code Assist flow): Google's
  Gemini CLI FAQ declares third-party OAuth reuse a direct ToS violation
  subject to account suspension, and account/project suspensions for this
  pattern are documented upstream.
* Claude Pro/Max OAuth (the Claude Code flow): Anthropic's
  legal-and-compliance page (2026-02-19) declares subscription OAuth tokens
  for Claude Code and native Anthropic apps only — "not permitted" in any
  other product, tool, or service.

The honest API-key paths stay: ``provider=google`` (``GOOGLE_API_KEY``) and
``provider=anthropic`` (``ANTHROPIC_API_KEY``) are untouched by this wave.

Proof shape (E2E-first, no seam mocks):
* ``get_provider`` answers the generic typed unknown-provider error — no
  special-case error machinery for the removed names.
* The CLI no longer exposes ``auth claude`` / ``auth gemini`` subcommands.
* The deleted modules cannot be imported (subprocess probes) and no
  ``machina`` source references them (text census), so a reintroduction
  fails here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from machina.cli import auth as auth_cli
from machina.core.exceptions import ValidationError
from machina.core.provider import get_provider
from machina.core.provider.specs import provider_names

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "machina"

REMOVED_PROVIDERS = ("claude", "gemini")

REMOVED_MODULES = (
    "machina.security.claude_auth",
    "machina.security.gemini_auth",
    "machina.core.provider.claude",
    "machina.core.provider.gemini",
)

#: Source tokens whose presence anywhere in ``machina/`` means a removed flow
#: is reachable again (import, docstring hint, or CLI help wiring).
FORBIDDEN_SOURCE_TOKENS = (
    "claude_auth",
    "gemini_auth",
    "ClaudeProvider",
    "GeminiProvider",
    "auth claude",
    "auth gemini",
)


@pytest.mark.parametrize("provider", REMOVED_PROVIDERS)
def test_removed_providers_answer_the_generic_unknown_provider_error(provider: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        get_provider(provider)
    message = str(excinfo.value)
    assert message.startswith(f"Unknown provider: {provider}")
    # The generic message lists the selectable providers; the removed name is
    # not among them and no removal-specific hint machinery exists.
    assert provider not in provider_names()
    assert "Choose one of:" in message


@pytest.mark.parametrize("provider", REMOVED_PROVIDERS)
def test_removed_auth_subcommands_are_gone_from_the_cli(provider: str) -> None:
    runner = CliRunner()
    result = runner.invoke(auth_cli.app, [provider])
    assert result.exit_code == 2, result.output
    assert f"No such command '{provider}'" in result.output, result.output
    assert f"Usage: auth {provider}" not in result.output, result.output


def test_auth_help_keeps_only_the_remaining_subscription_providers() -> None:
    result = CliRunner().invoke(auth_cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "chatgpt" in result.output
    assert "copilot" in result.output
    for removed in REMOVED_PROVIDERS:
        assert removed not in result.output, result.output


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_modules_are_not_importable(module: str) -> None:
    probe = subprocess.run(
        [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert probe.returncode != 0, f"{module} is still importable"
    assert "ModuleNotFoundError" in probe.stderr, probe.stderr


@pytest.mark.parametrize("attribute", ["ClaudeProvider", "GeminiProvider"])
def test_provider_facade_no_longer_exports_removed_transports(attribute: str) -> None:
    import machina.core.provider as provider_package

    assert not hasattr(provider_package, attribute)


def test_no_machina_source_mentions_a_removed_flow() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_SOURCE_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert not offenders, offenders


@pytest.mark.parametrize("provider", REMOVED_PROVIDERS)
def test_removed_providers_report_unknown_in_runtime_readiness(provider: str, monkeypatch) -> None:
    from machina.core import runtime
    from machina.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"provider": provider})
    report = runtime._detect_providers()
    assert report["chat_provider"] == "unavailable", report
    assert f"unknown provider {provider!r}" in report["chat_provider_reason"], report
