"""Adversarial proofs for the round-4 ChatGPT auth fences (R4-040/041/049).

Real store files on disk, real cross-process ``flock`` contention (the lock is
held by an actual child process), and HTTP mocked only at the ``httpx``
transport boundary through the ``client=`` injection point the module already
exposes. No module function is patched; the only test-time knob is the
documented lock-timeout constant, shortened so a contended acquisition is fast
to assert (the contention itself stays real).

RED evidence against the unfixed tree (captured in
``docs/verification/proofs/codebase_review_round4_2026-10-14/R4-040_041_049_chatgpt.md``):

* R4-040 — a refresh started from tokens loaded before a logout recreated the
  removed store.
* R4-041 — a benign peer rotation made a completed login exchange raise
  ``chatgpt_auth_cancelled`` and waste the already-spent single-use code.
* R4-049 — a contended store lock blocked forever instead of failing typed
  inside a bounded window.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from machina.security import chatgpt_auth, store_fence
from machina.security.chatgpt_auth import ChatGPTAuthError

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Watchdog for a bounded operation; deliberately far above the reduced bound.
_LOCK_WINDOW_SECONDS = 5.0
_REDUCED_LOCK_TIMEOUT_SECONDS = 0.4


def _tokens(*, access: str, refresh: str, expires_in: float = 3600.0) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + expires_in,
    }


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)


# ─── cross-process lock contention harness ──────────────────────────────────

_HOLD_LOCK_CHILD = """
import sys, time
from pathlib import Path

{import_line}

target = Path(sys.argv[1])
with {lock_expr}:
    print("locked", flush=True)
    time.sleep(float(sys.argv[2]))
"""


@contextmanager
def _lock_held_in_a_child_process(
    *, target: Path, module: str, lock_expr: str, hold_seconds: float = 30.0
):
    """Hold the store lock in a real child process for the duration of the block."""
    code = _HOLD_LOCK_CHILD.format(
        import_line=f"from machina.security import {module}", lock_expr=lock_expr
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(target), str(hold_seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    try:
        ready = process.stdout.readline() if process.stdout is not None else ""
        if ready.strip() != "locked":
            stderr = process.stderr.read() if process.stderr is not None else ""
            pytest.fail(f"lock-holder child never acquired the lock: {ready!r} stderr={stderr!r}")
        yield process
    finally:
        process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _bounded_call(operation, *, window: float = _LOCK_WINDOW_SECONDS) -> BaseException | None:
    """Run *operation* in a daemon thread; fail the test if it is still blocked.

    Returns the exception the operation raised (``None`` when it returned).
    """
    outcome: dict[str, BaseException | None] = {}
    finished = threading.Event()

    def call() -> None:
        try:
            operation()
        except BaseException as exc:  # reported to the test for classification
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=call, daemon=True, name="bounded-lock-probe")
    thread.start()
    assert finished.wait(timeout=window), (
        f"operation was still blocked after {window:g}s: the contended store lock "
        "never failed with a typed error"
    )
    return outcome.get("error")


# ─── R4-040: pre-logout tokens must not resurrect the store ─────────────────


def test_pre_logout_tokens_never_recreate_the_store(tmp_path):
    """A refresh from tokens loaded before a logout fails auth-required."""
    target = tmp_path / "auth" / "chatgpt.json"
    attempts = 5

    for attempt in range(attempts):
        chatgpt_auth.save_tokens(
            _tokens(access=f"at-stale-{attempt}", refresh=f"rt-stale-{attempt}"), target
        )
        stale = chatgpt_auth.load_tokens(target)
        assert stale is not None
        assert chatgpt_auth.logout(path=target) is True
        assert not target.exists(), "logout itself must remove the store"

        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - fenced
            requests.append(str(request.url))
            return httpx.Response(
                200, json={"access_token": "at-ghost", "refresh_token": "rt-ghost"}
            )

        with pytest.raises(ChatGPTAuthError) as excinfo:
            chatgpt_auth.refresh(stale, path=target, client=_mock_client(handler))

        assert excinfo.value.code == "chatgpt_auth_signed_out"
        assert "codex login" in str(excinfo.value)
        assert requests == [], (
            f"attempt {attempt}: a pre-logout refresh must not replay the stale refresh token"
        )
        assert not target.exists(), (
            f"attempt {attempt}: a pre-logout refresh recreated the removed store"
        )


# ─── R4-041: peer rotation must not false-cancel a login exchange ───────────


def test_login_exchange_publishes_through_a_peer_rotation(tmp_path):
    """A benign peer rotation (same generation, new content) must not cancel."""
    target = tmp_path / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(access="at-peer-old", refresh="rt-peer-old"), target)
    generation = chatgpt_auth.credential_generation(target)
    peer_posts: list[str] = []

    def peer_handler(request: httpx.Request) -> httpx.Response:
        supplied = parse_qs(request.content.decode())["refresh_token"][0]
        peer_posts.append(supplied)
        return httpx.Response(
            200, json={"access_token": "at-rotated", "refresh_token": "rt-rotated"}
        )

    def exchange_handler(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["authorization_code"]
        # A peer process rotates the store while this exchange is in flight:
        # same generation, different content.
        stored = chatgpt_auth.load_tokens(target)
        assert stored is not None
        rotated = chatgpt_auth.refresh(stored, path=target, client=_mock_client(peer_handler))
        assert rotated["refresh_token"] == "rt-rotated"
        return httpx.Response(200, json={"access_token": "at-login", "refresh_token": "rt-login"})

    issued = chatgpt_auth.exchange_code(
        "the-code",
        code_verifier="verifier",
        path=target,
        client=_mock_client(exchange_handler),
        generation=generation,
    )

    assert issued["refresh_token"] == "rt-login"
    assert peer_posts == ["rt-peer-old"], "the peer must rotate exactly once from the old token"
    stored = chatgpt_auth.load_tokens(target)
    assert stored is not None
    assert stored["access_token"] == "at-login", (
        "the completed exchange must publish its tokens instead of cancelling and "
        "wasting the single-use code"
    )
    assert stored["refresh_token"] == "rt-login"


def test_concurrent_refresh_still_adopts_a_single_rotation(tmp_path):
    """Regression: two concurrent refreshes still spend one POST and both adopt."""
    target = tmp_path / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(access="at-old", refresh="rt-old"), target)
    posts: list[str] = []
    posts_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        supplied = parse_qs(request.content.decode())["refresh_token"][0]
        with posts_lock:
            posts.append(supplied)
        time.sleep(0.2)  # keep the store lock held so the peer must wait it out
        return httpx.Response(200, json={"access_token": "at-new", "refresh_token": "rt-new"})

    barrier = threading.Barrier(2)

    def call() -> tuple[str, str]:
        with _mock_client(handler) as client:
            barrier.wait(timeout=10)
            try:
                updated = chatgpt_auth.refresh(
                    _tokens(access="at-old", refresh="rt-old"), path=target, client=client
                )
            except ChatGPTAuthError as error:  # pragma: no cover - the race bug
                return ("error", error.code)
            return ("ok", str(updated["refresh_token"]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: call(), range(2)))

    assert posts == ["rt-old"], "the rotating token must be submitted exactly once"
    assert outcomes == [("ok", "rt-new"), ("ok", "rt-new")]
    stored = chatgpt_auth.load_tokens(target)
    assert stored is not None and stored["refresh_token"] == "rt-new"


# ─── R4-049: bounded lock acquisition ───────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics are required")
def test_store_lock_default_wait_is_bounded(monkeypatch, tmp_path):
    """The shared store lock cannot block indefinitely under contention."""
    target = tmp_path / "auth" / "chatgpt.json"
    monkeypatch.setattr(
        store_fence, "STORE_LOCK_TIMEOUT_SECONDS", _REDUCED_LOCK_TIMEOUT_SECONDS, raising=False
    )

    def acquire() -> None:
        with store_fence.store_lock(target):
            pass

    with _lock_held_in_a_child_process(
        target=target, module="store_fence", lock_expr="store_fence.store_lock(target)"
    ):
        started = time.monotonic()
        error = _bounded_call(acquire)
        elapsed = time.monotonic() - started

    assert isinstance(error, store_fence.StoreLockTimeout), f"got {error!r}"
    assert elapsed < _LOCK_WINDOW_SECONDS


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics are required")
def test_chatgpt_refresh_under_lock_contention_fails_typed(monkeypatch, tmp_path):
    """The chatgpt store lock surfaces a typed error instead of blocking forever."""
    target = tmp_path / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(access="at-live", refresh="rt-live"), target)
    tokens = chatgpt_auth.load_tokens(target)
    assert tokens is not None
    monkeypatch.setattr(
        store_fence, "STORE_LOCK_TIMEOUT_SECONDS", _REDUCED_LOCK_TIMEOUT_SECONDS, raising=False
    )
    network_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        network_hits.append(str(request.url))
        return httpx.Response(500)

    with _lock_held_in_a_child_process(
        target=target, module="chatgpt_auth", lock_expr="chatgpt_auth._store_lock(target)"
    ):
        error = _bounded_call(
            lambda: chatgpt_auth.refresh(tokens, path=target, client=_mock_client(handler))
        )

    assert isinstance(error, ChatGPTAuthError), f"got {error!r}"
    assert error.code == "chatgpt_auth_lock_timeout"
    assert network_hits == [], "a lock-starved refresh must not reach the token endpoint"
