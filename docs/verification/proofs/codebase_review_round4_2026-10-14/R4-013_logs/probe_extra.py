"""Ad-hoc probes for R4-013/R4-014 behaviors the 7-test gate does not cover.

Run with ``.venv/bin/python`` from the repo root. Exits non-zero on the first
failed probe. Uses only tmp dirs + mocked HTTP at the transport boundary.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from machina.core.config import config
from machina.security import claude_auth, gemini_auth  # noqa: E402
from machina.security.store_fence import credential_generation  # noqa: E402

config.machina_home = ""


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)


def probe_gemini_refresh_after_logout() -> None:
    """R4-014: a gemini refresh finishing after logout must not recreate the store."""
    with TemporaryDirectory() as tmp:
        store = Path(tmp) / "gemini.json"
        gemini_auth.save_tokens(
            {
                "access_token": "at-old",
                "refresh_token": "rt-old",
                "scope": gemini_auth.SCOPES,
                "expires_at": time.time() - 60,
            },
            store,
        )
        dispatched, release = threading.Event(), threading.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            dispatched.set()
            assert release.wait(timeout=10)
            return httpx.Response(
                200,
                json={"access_token": "at-new", "expires_in": 3600, "scope": gemini_auth.SCOPES},
            )

        outcome: dict = {}

        def call() -> None:
            with _client(handler) as client:
                try:
                    outcome["tokens"] = gemini_auth.refresh(path=store, client=client)
                except gemini_auth.GeminiAuthError as error:
                    outcome["error"] = error.code

        thread = threading.Thread(target=call)
        thread.start()
        assert dispatched.wait(timeout=10), "refresh never reached the token endpoint"
        assert gemini_auth.logout(path=store) is True
        assert not store.exists(), "logout must remove the store before the response lands"
        release.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not store.exists(), "a refresh completing after logout recreated the store"
        assert gemini_auth.load_tokens(store) is None
        # The caller may keep the fresh in-memory tokens; the store must not.
        assert outcome.get("tokens", {}).get("access_token") == "at-new" or outcome.get("error")
        print("PASS probe_gemini_refresh_after_logout")


def probe_claude_post_error_adoption() -> None:
    """R4-013 step 5: a rejected consumed token adopts the peer's rotated pair."""
    with TemporaryDirectory() as tmp:
        store = Path(tmp) / "claude.json"
        claude_auth.save_tokens(
            {"access_token": "at-old", "refresh_token": "rt-old", "expires_at": time.time() - 60},
            store,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            supplied = str(json.loads(request.content.decode())["refresh_token"])
            if supplied == "rt-old":
                # A peer rotated the store while our request was in flight.
                claude_auth.save_tokens(
                    {
                        "access_token": "at-peer",
                        "refresh_token": "rt-peer",
                        "expires_at": time.time() + 3600,
                    },
                    store,
                )
            return httpx.Response(400, json={"error": "invalid_grant"})

        with _client(handler) as client:
            adopted = claude_auth.refresh(
                {"access_token": "at-stale", "refresh_token": "rt-old"},
                path=store,
                client=client,
            )
        assert adopted["access_token"] == "at-peer", adopted
        assert adopted["refresh_token"] == "rt-peer", adopted
        stored = claude_auth.load_tokens(store)
        assert stored is not None and stored["refresh_token"] == "rt-peer"
        print("PASS probe_claude_post_error_adoption")


def probe_generation_bump_is_durable() -> None:
    """R4-013/14/15: fenced logout bumps the durable generation by one."""
    with TemporaryDirectory() as tmp:
        for name, module in (
            ("claude.json", claude_auth),
            ("gemini.json", gemini_auth),
        ):
            store = Path(tmp) / name
            module.save_tokens({"access_token": "at", "refresh_token": "rt"}, store)
            before = credential_generation(store)
            assert module.logout(path=store) is True
            after = credential_generation(store)
            assert after == before + 1, (name, before, after)
            assert int((Path(tmp) / f"{name}.generation").read_text().strip()) == after
            assert module.logout(path=store) is False
            assert credential_generation(store) == after + 1
            print(f"PASS probe_generation_bump_is_durable[{name}]")


if __name__ == "__main__":
    home = TemporaryDirectory()
    os.environ["HOME"] = home.name
    for variable in (
        "MACHINA_CLAUDE_AUTH_FILE",
        "CLAUDE_CONFIG_DIR",
        "MACHINA_GEMINI_AUTH_FILE",
        "MACHINA_GEMINI_IMPORT_FILE",
        "MACHINA_HOME",
        "MACHINA_MACHINA_HOME",
    ):
        os.environ.pop(variable, None)
    claude_auth._read_claude_code_keychain = lambda: None
    probe_gemini_refresh_after_logout()
    probe_claude_post_error_adoption()
    probe_generation_bump_is_durable()
    print("all probes passed")
