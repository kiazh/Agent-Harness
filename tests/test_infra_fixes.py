"""INFRA audit fixes: sandbox image, installer pinning, placeholder guard, CI.

Regression pins for the infra/cluster audit (INFRA-B1/M1/M2/M4, m1/m2/m3/m5/n5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_dockerfile_matches_current_package_layout():
    """INFRA-B1: stale `ah/` path made the sandbox image unbuildable."""
    text = (ROOT / "Dockerfile.sandbox").read_text(encoding="utf-8")
    assert "COPY ah" not in text
    assert "COPY machina ./machina" in text
    assert "COPY skills ./skills" in text
    # Isolation image: runtime install only, no dev extras.
    assert "'.[dev]'" not in text


def test_installer_uses_locked_ui_install():
    """INFRA-M1: lockfile must be enforced (npm ci), not re-resolved."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "npm ci --ignore-scripts --prefix ui" in text
    assert "npm install --ignore-scripts --prefix ui" not in text


def test_installer_dir_flag_requires_a_value():
    """INFRA-m2: --dir with no value must fail loudly, not silently exit 1."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--dir requires a value" in text


def test_installer_chmod_failures_warn():
    """INFRA-m3: a failed chmod must warn, never be swallowed."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert text.count('chmod 600 "$ENV_FILE" 2>/dev/null || warn') == 2


def test_pgvector_fallback_is_pinned_and_verified():
    """INFRA-M2: the clone+make fallback must pin tag AND commit SHA."""
    text = (ROOT / "tools" / "install-helpers.sh").read_text(encoding="utf-8")
    assert 'pgvector_tag="v0.8.7"' in text
    assert 'pgvector_sha="f37c13f68b57d2c3472b2214fbcff699d6d34876"' in text
    assert '--branch "$pgvector_tag"' in text
    assert "rev-parse HEAD" in text


def test_placeholder_api_key_refuses_startup(monkeypatch):
    """INFRA-M4: the public .env.example placeholder must fail closed."""
    monkeypatch.setenv("MACHINA_API_KEY", "replace-with-a-long-random-key")
    from machina.api.app import create_app

    with pytest.raises(RuntimeError, match="placeholder"):
        create_app()


def test_real_or_absent_key_still_constructs(monkeypatch):
    monkeypatch.setenv("MACHINA_API_KEY", "a-real-generated-key-0123456789abcdef")
    from machina.api.app import create_app

    app = create_app()
    assert app is not None


def test_ci_builds_sandbox_image_and_has_timeouts():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker build --tag machina-tool-sandbox:ci -f Dockerfile.sandbox ." in text
    assert text.count("timeout-minutes:") >= 3


def test_dependabot_covers_all_surfaces():
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    for ecosystem in ("github-actions", "pip", "npm", "docker"):
        assert f"package-ecosystem: {ecosystem}" in text


def test_compose_scripts_mount_target_exists_in_repo():
    """INFRA-m1: the bind-mount target must exist (root-owned auto-create)."""
    assert (ROOT / "scripts" / "README.md").is_file()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./scripts:/app/scripts:ro" in compose
