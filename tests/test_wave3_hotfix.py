"""Wave-3 hotfix regressions (blocking findings from the RF audit wave).

Covers:
- RF8-C1: secrets files are created/repaired 0600 on every write.
- RF4-W3: a non-ASCII auth token fails closed instead of crashing stdio.
- RF4-C1: cancellation during claim release must not orphan the renewal loop;
  the claim is released and the turn reaches its terminal event.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.models import AgentResponse, StreamEvent
from machina.gateway.errors import UNAUTHORIZED
from machina.gateway.server import Gateway


class _Agent:
    """Minimal streaming agent: one done event, no tools."""

    _learning_tasks: tuple = ()

    async def run_stream(self, session_id, user_message, verbose=False, **_kw):
        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="ok", tool_calls=[], tokens_used=1, iterations=1
            ),
        )


def _frames() -> tuple[list, list]:
    frames: list = []
    return frames, (lambda f: frames.append(f))


# ─── RF8-C1: env file mode ───────────────────────────────────────────────────


def test_env_file_written_0600_and_repairs_existing(tmp_path):
    from machina.security.env_file import set_env_values

    env = tmp_path / ".env"
    set_env_values({"OPENROUTER_API_KEY": "sk-test"}, env)
    assert (env.stat().st_mode & 0o777) == 0o600

    # A pre-existing world-readable file is repaired by the next write.
    env.chmod(0o644)
    set_env_values({"OPENAI_API_KEY": "sk-2"}, env)
    assert (env.stat().st_mode & 0o777) == 0o600
    assert "sk-2" in env.read_text(encoding="utf-8")


# ─── RF4-W3: non-ASCII token must not crash the gateway ─────────────────────


def test_non_ascii_token_fails_closed_without_crash():
    frames, write = _frames()
    gw = Gateway(write=write)
    gw._auth_token = "sekret"
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "usage.get", "params": {"token": "éééé"}}
    )
    asyncio.run(gw.handle_line(line))  # must not raise
    assert frames, "gateway must answer"
    assert frames[-1]["error"]["code"] == UNAUTHORIZED


# ─── RF3-F1: case-aliased protected paths require review ────────────────────


def test_case_aliased_git_and_machina_writes_require_security_review():
    from machina.permissions.policy import build_request, decide

    for target in (".GIT/hooks/pre-commit", ".MACHINA/state.db"):
        req = build_request(operation="file.write", targets=[target], content="x")
        assert decide(req).verdict == "pending", target
    req_exec = build_request(operation="process.exec", argv=["touch", ".GIT/config"], cwd=".")
    assert decide(req_exec).verdict == "pending"


# ─── RF3-F2/F3: approval surfaces must not carry secrets ────────────────────


def test_write_diff_and_card_redact_secrets(tmp_path):
    from machina.permissions.broker import _approval_view, _file_write_diff
    from machina.permissions.policy import build_request

    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz00"
    existing = tmp_path / "conf.py"
    existing.write_text(f'ANTHROPIC_KEY = "{secret}"\n', encoding="utf-8")

    req = build_request(
        operation="file.write",
        targets=[str(existing)],
        content="ANTHROPIC_KEY = 'changed'\n",
    )
    diff = _file_write_diff(req)
    assert secret not in diff["diff"]
    assert secret not in diff["current_preview"]

    exec_req = build_request(
        operation="process.exec",
        argv=["curl", "-H", f"Authorization: Bearer {secret}", "https://x.example"],
        cwd=".",
        tool="terminal",
    )
    card = _approval_view(
        exec_req, {"request_id": exec_req.request_id, "status": "pending"}
    )
    assert all(secret not in arg for arg in card["argv"])
    assert secret not in card["shell"]


# ─── RF4-C1: cancel during release must not orphan renewal/claim ────────────


@pytest.mark.asyncio
async def test_cancel_during_release_releases_claim_and_terminates(monkeypatch):
    frames, write = _frames()
    gw = Gateway(write=write, agent_factory=lambda _m, _p: _Agent())

    sid = uuid.uuid4()
    turn_id = "hotfix-turn"
    token = "tok-1"
    session = SimpleNamespace(id=sid, agent_id="harness", model=None, provider=None)
    from machina.core.session import session_manager

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))

    release_started = asyncio.Event()
    release_gate = asyncio.Event()
    end_calls: list = []

    async def slow_end(_session_id, released_token):
        end_calls.append(released_token)
        release_started.set()
        await release_gate.wait()

    monkeypatch.setattr("machina.core.turns.end_turn", slow_end)

    renew_calls: list = []

    async def fake_renew(_session_id, _token):
        renew_calls.append(1)
        return True

    monkeypatch.setattr("machina.core.turns.renew_turn", fake_renew)

    gw._turn_tokens[str(sid)] = token
    marker = object()
    gw._turns[str(sid)] = marker  # type: ignore[assignment]

    task = asyncio.create_task(gw._run_turn(sid, turn_id, "hello"))
    # Wait until cleanup reaches the (slow) release, then cancel mid-release.
    await asyncio.wait_for(release_started.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)  # deliver the cancellation
    release_gate.set()

    # The hotfix consumes the mid-release cancellation, settles the release,
    # and finishes cleanup + terminal emission instead of unwinding.
    await asyncio.wait_for(task, timeout=5)

    assert end_calls == [token], "claim release ran exactly once"
    assert gw._turn_tokens.get(str(sid)) is None, "owner token cleared"
    assert gw._turns.get(str(sid)) is None, "turn registration cleared"
    kinds = [f["params"]["type"] for f in frames if f.get("method") == "event"]
    assert "message.start" in kinds
    assert "message.complete" in kinds, f"terminal event missing: {kinds}"
    assert renew_calls == [], "renewal must never run after release starts"
