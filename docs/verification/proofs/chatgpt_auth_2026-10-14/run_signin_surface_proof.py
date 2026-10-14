#!/usr/bin/env python
"""Live proof: the user-facing ChatGPT sign-in surface (``machina auth``).

The sub-app registration in ``machina/cli/__init__.py`` is deferred (another
lane holds that file), so this runner builds a temporary root Typer app and
adds the real ``machina.cli.auth`` sub-app exactly as the deferred line will,
then invokes it through Click's runner — the same command surface a user gets.

Captured (redacted; token values never reach an artifact):

1. ``auth status`` on the real machine (read-only) plus, in a sandbox HOME
   with only a copied Codex CLI ``auth.json``, the signed-in-via-codex state.
2. ``auth login --import-codex`` into a sandboxed Machina auth path, then the
   status after import (source: machina).
3. ``auth login --no-browser --timeout 0.3``: the real authorization URL
   (PKCE state/challenge only; the code verifier is never printed) followed by
   the clean timeout error. No browser exchange is attempted in automation.
4. ``auth logout`` in the sandbox: the Machina store is removed, the copied
   Codex file is byte-identical (sha256 + mtime), and status falls back to
   ``source: codex``.

Usage (from the repo root, with the repo venv):

    .venv/bin/python docs/verification/proofs/chatgpt_auth_2026-10-14/run_signin_surface_proof.py

Exit code 0 = every step produced its expected exit status; 1 = honest
failure (captured in the artifacts).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
for candidate in (PROOF_DIR, *PROOF_DIR.parents):
    if (candidate / "pyproject.toml").is_file() and (candidate / "machina").is_dir():
        REPO = candidate
        break
else:  # pragma: no cover - repo layout is fixed
    raise SystemExit("run this script from the Machina repo")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from machina.cli.auth import app as auth_app  # noqa: E402

# The deferred registration, verbatim:
root = typer.Typer(help="temporary CLI entry (real sub-app)")
root.add_typer(auth_app, name="auth")
runner = CliRunner()

_real_home = Path.home()
_real_codex = _real_home / ".codex" / "auth.json"
_real_store = _real_home / ".machina" / "auth" / "chatgpt.json"

_FAILURES: list[str] = []


def _capture(args: list[str], env: dict[str, str] | None = None) -> tuple[object, str]:
    result = runner.invoke(root, args, env=env or {})
    return result, result.output


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "(absent)"


def _live_tokens() -> list[str]:
    """Read the real token values ONLY to prove they never reach an artifact."""
    values: list[str] = []
    for path in (_real_codex, _real_store):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw
        if not isinstance(tokens, dict):
            continue
        for key in ("access_token", "refresh_token", "id_token"):
            value = tokens.get(key)
            if isinstance(value, str) and len(value) > 20:
                values.append(value)
    return values


def main() -> int:
    status_lines: list[str] = ["# Sign-in surface status proof (machina auth status)\n"]
    login_lines: list[str] = ["# Sign-in surface: login URL generation (no exchange)\n"]
    scope_lines: list[str] = ["# Sign-in surface: import + logout scope\n"]

    # 0. The command surface as registered.
    result, output = _capture(["--help"])
    status_lines.append("$ machina auth --help")
    status_lines.append(f"exit code: {result.exit_code}")
    status_lines.append(output)

    # 1a. Real machine: read-only status (current home).
    result, output = _capture(["auth", "status"])
    status_lines.append("$ machina auth status        # real home (read-only)")
    status_lines.append(f"exit code: {result.exit_code}")
    status_lines.append(output)
    result, output = _capture(["auth", "status", "--json"])
    status_lines.append("$ machina auth status --json # real home (read-only)")
    status_lines.append(f"exit code: {result.exit_code}")
    status_lines.append(output)
    real_json = json.loads(output)

    # 1b. Sandbox: no Machina store, only a copied Codex CLI auth.json.
    sandbox = Path(tempfile.mkdtemp(prefix="machina-signin-proof-"))
    sandbox_home = sandbox / "home"
    codex_home = sandbox_home / ".codex"
    codex_home.mkdir(parents=True)
    codex_copy = codex_home / "auth.json"
    shutil.copyfile(_real_codex, codex_copy)
    sandbox_store = sandbox / "machina-auth" / "chatgpt.json"
    env = {
        "HOME": str(sandbox_home),
        "CODEX_HOME": str(codex_home),
        "MACHINA_CHATGPT_AUTH_FILE": str(sandbox_store),
        "MACHINA_HOME": str(sandbox_home / ".machina"),
    }
    try:
        result, output = _capture(["auth", "status", "--json"], env)
        status_lines.append("$ machina auth status --json # sandbox: codex credentials only")
        status_lines.append(f"exit code: {result.exit_code}")
        status_lines.append(output)
        codex_json = json.loads(output)

        # 2. Import into the sandboxed Machina store.
        codex_before = _sha256(codex_copy)
        result, output = _capture(["auth", "login", "--import-codex"], env)
        scope_lines.append("$ machina auth login --import-codex  # sandboxed store")
        scope_lines.append(f"exit code: {result.exit_code}")
        scope_lines.append(output)
        result, output = _capture(["auth", "status", "--json"], env)
        status_lines.append("$ machina auth status --json # sandbox after import")
        status_lines.append(f"exit code: {result.exit_code}")
        status_lines.append(output)
        imported_json = json.loads(output)
        store_after_import = sandbox_store.exists()
        result, output = _capture(["auth", "status"], env)
        status_lines.append("$ machina auth status        # sandbox after import (human card)")
        status_lines.append(f"exit code: {result.exit_code}")
        status_lines.append(output)

        # 3. URL generation (bounded wait; no browser, no exchange in automation).
        result, output = _capture(["auth", "login", "--no-browser", "--timeout", "0.3"], env)
        login_lines.append("$ machina auth login --no-browser --timeout 0.3")
        login_lines.append(f"exit code: {result.exit_code} (expected 1: timeout, no browser)")
        login_lines.append(output)
        login_lines.append(
            "note: the printed URL contains the one-time PKCE state and challenge; "
            "the code verifier is never printed, and no token exchange was attempted."
        )
        login_ok = result.exit_code == 1 and "Open this URL" in output

        # 4. Logout removes ONLY the sandboxed Machina store.
        store_mode = (
            oct(sandbox_store.stat().st_mode & 0o777) if sandbox_store.exists() else "(absent)"
        )
        codex_after_import = _sha256(codex_copy)
        result, output = _capture(["auth", "logout"], env)
        scope_lines.append("$ machina auth logout        # sandboxed store only")
        scope_lines.append(f"exit code: {result.exit_code}")
        scope_lines.append(output)
        result, output = _capture(["auth", "status", "--json"], env)
        scope_lines.append("$ machina auth status --json # sandbox after logout")
        scope_lines.append(f"exit code: {result.exit_code}")
        scope_lines.append(output)
        after_logout_json = json.loads(output)
        codex_after_logout = _sha256(codex_copy)
        scope_lines.append("")
        scope_lines.append("scope evidence (sandbox):")
        scope_lines.append(f"  machina store perms before logout: {store_mode}")
        scope_lines.append(f"  machina store exists after logout: {sandbox_store.exists()}")
        scope_lines.append(
            f"  codex auth.json sha256 before/after import: {codex_before} / {codex_after_import}"
        )
        scope_lines.append(f"  codex auth.json sha256 after logout:        {codex_after_logout}")
        scope_lines.append("  codex copy untouched: " + str(codex_before == codex_after_logout))

        # Expectations (live truth, asserted for the runner's exit code).
        checks = [
            ("real status signed_in is a boolean", isinstance(real_json.get("signed_in"), bool)),
            ("sandbox codex-only status signed_in", codex_json.get("signed_in") is True),
            ("sandbox codex-only status source", codex_json.get("source") == "codex"),
            ("import wrote the sandbox store", store_after_import),
            ("import status source machina", imported_json.get("source") == "machina"),
            ("import status account id", bool(imported_json.get("account_id"))),
            ("codex file byte-identical after import", codex_before == codex_after_import),
            ("login printed a URL and timed out cleanly", login_ok),
            ("logout removed the sandbox store", not sandbox_store.exists()),
            ("codex file byte-identical after logout", codex_before == codex_after_logout),
            ("status after logout falls back to codex", after_logout_json.get("source") == "codex"),
        ]
        for label, ok in checks:
            if not ok:
                _FAILURES.append(label)
        scope_lines.append("")
        scope_lines.append("checks:")
        scope_lines.extend(f"  {'PASS' if ok else 'FAIL'} {label}" for label, ok in checks)

        # Redaction scan BEFORE writing: no live token value in any artifact.
        artifacts = "\n".join(status_lines + login_lines + scope_lines)
        leaked = [value for value in _live_tokens() if value in artifacts]
        scope_lines.append("")
        scope_lines.append(
            f"redaction scan: {'0 token values found' if not leaked else f'LEAK: {len(leaked)}'}"
        )
        if leaked:
            _FAILURES.append("token material leaked into artifacts")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    (PROOF_DIR / "auth_status.txt").write_text("\n".join(status_lines) + "\n", encoding="utf-8")
    (PROOF_DIR / "auth_login_url.txt").write_text("\n".join(login_lines) + "\n", encoding="utf-8")
    (PROOF_DIR / "auth_import_logout.txt").write_text(
        "\n".join(scope_lines) + "\n", encoding="utf-8"
    )

    print("\n".join(scope_lines))
    if _FAILURES:
        print("\nFAILED checks:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
