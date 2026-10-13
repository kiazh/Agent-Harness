#!/usr/bin/env python3
"""End-to-end acceptance gate for Machina (stdlib only).

Runs real journeys against real components (PostgreSQL, DeepSeek, live web,
the self-hosted Firecrawl MCP stack) and records the observed end state in
`docs/verification/proofs/<UTC date>-e2e-gate.{json,md}`.

Policy: `docs/verification/E2E_PROTOCOL.md`. Journeys assert OBSERVABLE end
state (DB rows, indexed chunks, tool outcomes, CLI exit/error surface) — never
unit-test counts. Every "fixed/passes" claim must carry the recorded command
and observed output this tool writes.

Usage
-----
    .venv/bin/python tools/e2e_gate.py                      # all journeys
    .venv/bin/python tools/e2e_gate.py --journeys j1,j5     # subset
    .venv/bin/python tools/e2e_gate.py --json /tmp/gate.json
    .venv/bin/python tools/e2e_gate.py --scratch-dir /tmp/machina-e2e

Exit codes: 0 = no failures (skips allowed), 1 = at least one FAIL,
2 = usage/environment error.

The outer script is stdlib-only; every step that needs Machina imports or
PostgreSQL runs inside the repo venv python (asyncpg lives there), the same
delegation pattern as machina/research/head_to_head.py. Secrets are never
printed: observed values are ids, counts, substrings and bounded excerpts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = REPO_ROOT / ".venv"
PROOFS_DIR = REPO_ROOT / "docs" / "verification" / "proofs"
JOURNEY_IDS = ("j1", "j2", "j3", "j4", "j5")
JOURNEY_TITLES = {
    "j1": "memory-roundtrip (chat -> consolidate -> memories row + cursor)",
    "j2": "web-research-rag (real web_research -> context_chunks for source)",
    "j3": "web-crawl (real web_crawl -> indexed chunks for source)",
    "j4": "mcp-firecrawl (connector connect -> execute_call scrape -> markdown)",
    "j5": "cli-smoke (machina chat exit 0, no InterfaceError / extraction failure)",
}
ERROR_SUBSTRINGS = ("InterfaceError", "Memory extraction failed")
FIRECRAWL_DEFAULT_URL = "http://localhost:3002"
JSON_PAGE = "https://docs.python.org/3/library/json.html"
# No `site:` operator: the DuckDuckGo HTML endpoint intermittently drops those
# entirely (a real gate run failed on it), and the journey must not depend on
# a fragile operator. One recorded retry below absorbs provider flake — never
# an assertion's strength.
JSON_QUERY = "python json encoder and decoder documentation"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"[e2e-gate] {message}", file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def child_env() -> dict[str, str]:
    """Inherited env minus harness markers; bytecode writes disabled."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("OPENCODE") or key == "AI_AGENT" or key.startswith("NO_MISTAKES"):
            env.pop(key, None)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_cmd(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 240.0,
) -> dict:
    """Run a subprocess with a hard timeout; kill the whole process group."""
    start = time.monotonic()
    env = dict(env) if env is not None else dict(os.environ)
    env["PWD"] = str(cwd)
    proc = subprocess.Popen(
        [str(part) for part in cmd],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # A detached grandchild can hold the pipes open forever; the
                # gate cap must not be defeated by a runner that never returns.
                for stream in (proc.stdout, proc.stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
                stdout, stderr = "", ""
    return {
        "cmd": [str(part) for part in cmd],
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - start, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


def git_capture(repo: Path) -> dict:
    """Provenance: short HEAD plus sha256 of `git status --short`."""
    try:
        head = run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=30)
        status = run_cmd(["git", "status", "--short"], cwd=repo, timeout=30)
        status_text = status["stdout"] if status["exit_code"] == 0 else ""
        return {
            "git_head": head["stdout"].strip() if head["exit_code"] == 0 else None,
            "status_short": [line for line in status_text.splitlines() if line.strip()],
            "status_sha256_16": hashlib.sha256(status_text.encode("utf-8")).hexdigest()[:16],
        }
    except Exception as error:  # noqa: BLE001 - provenance must never kill a run
        return {"git_capture_error": f"{type(error).__name__}: {error}"}


def error_hits(text: str) -> list[str]:
    return [needle for needle in ERROR_SUBSTRINGS if needle in text]


def new_record(jid: str) -> dict:
    return {
        "journey": jid,
        "title": JOURNEY_TITLES[jid],
        "status": "FAIL",
        "reason": "",
        "started_at": utc_now(),
        "finished_at": "",
        "duration_seconds": 0.0,
        "commands": [],
        "checks": [],
        "observed": {},
        "notes": [],
    }


def check(record: dict, name: str, ok: bool, expected: str, observed: object) -> bool:
    record["checks"].append(
        {"name": name, "ok": bool(ok), "expected": expected, "observed": observed}
    )
    return bool(ok)


def finish(record: dict, status: str, reason: str = "") -> dict:
    record["status"] = status
    record["reason"] = reason
    record["finished_at"] = utc_now()
    return record


# ---------------------------------------------------------------------------
# Venv-python embedded scripts (PostgreSQL / Machina imports live there)
# ---------------------------------------------------------------------------

_DB_QUERY_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

def to_jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

async def main():
    await db.connect()
    try:
        results = {}
        for query in SPEC["queries"]:
            rows = await db.fetch(query["sql"], *query.get("args", []))
            results[query["name"]] = [{k: to_jsonable(v) for k, v in dict(row).items()} for row in rows]
        print(json.dumps({"ok": True, "results": results}, default=str))
    finally:
        await db.close()

asyncio.run(main())
"""

_SESSION_SETUP_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

async def main():
    await db.connect()
    try:
        try:
            await db.initialize_runtime_schema()
        except Exception:
            pass
        from machina.core.session import session_manager
        from machina.core.session_mode import activate_durable_mode
        session = await session_manager.create(title=SPEC["title"], goal=SPEC["goal"], agent_id=SPEC["agent_id"])
        await activate_durable_mode(str(session.id), SPEC["agent_id"], "full")
        print(json.dumps({"ok": True, "session_id": str(session.id)}))
    finally:
        await db.close()

asyncio.run(main())
"""

_SESSION_REVOKE_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

async def main():
    await db.connect()
    try:
        from machina.core.session_mode import activate_durable_mode
        revoked = await activate_durable_mode(SPEC["session_id"], SPEC["agent_id"], "ask")
        print(json.dumps({"ok": True, "revoked_grants": revoked}))
    finally:
        await db.close()

asyncio.run(main())
"""

_J1_ASSERT_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
import msgpack
from machina.db.connection import db

SID = sys.argv[1]

async def main():
    await db.connect()
    try:
        row = await db.fetchrow(
            "SELECT state_msgpack, execution_mode FROM sessions WHERE id = $1::uuid", SID
        )
        state = {}
        if row is not None and row["state_msgpack"]:
            state = msgpack.unpackb(row["state_msgpack"], raw=False)
        memories = await db.fetch(
            "SELECT content, category, importance, created_at FROM memories "
            "WHERE session_id = $1::uuid ORDER BY created_at",
            SID,
        )
        out = {
            "ok": True,
            "session_found": row is not None,
            "execution_mode": str(row["execution_mode"]) if row is not None else None,
            "cursor": state.get("mem_consolidated_up_to"),
            "memory_count": len(memories),
            "memories": [
                {
                    "content": item["content"],
                    "category": item["category"],
                    "importance": float(item["importance"]) if item["importance"] is not None else None,
                    "created_at": item["created_at"].isoformat() if item["created_at"] else None,
                }
                for item in memories
            ],
        }
        print(json.dumps(out, default=str))
    finally:
        await db.close()

asyncio.run(main())
"""

_J1_CONSOLIDATE_SCRIPT = """
import asyncio, json, sys, uuid
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SID = sys.argv[1]

async def main():
    await db.connect()
    try:
        await db.initialize_runtime_schema()
        from machina.core.agent_factory import build_agent_for_session, close_agent_provider
        from machina.core.session import session_manager
        session = await session_manager.get_fresh(uuid.UUID(SID))
        if session is None:
            print(json.dumps({"ok": False, "error": "session not found"}))
            return
        agent = await build_agent_for_session(session)
        try:
            wired = agent.memory_consolidator is not None
            await agent._consolidate_memories(session.id)
            print(json.dumps({"ok": True, "consolidator_wired": wired}))
        finally:
            await close_agent_provider(agent)
    finally:
        await db.close()

asyncio.run(main())
"""

_TOOL_CALL_SCRIPT = """
import asyncio, json, sys, uuid
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

async def main():
    await db.connect()
    try:
        import machina.tools  # noqa: F401 - registers built-in tools
        from machina.tools.agents import current_agent_id, current_session_id
        from machina.tools.base import registry
        owner = await db.fetchval(
            "SELECT agent_id FROM sessions WHERE id = $1::uuid", SPEC["session_id"]
        )
        if owner != SPEC["agent_id"]:
            print(json.dumps({"ok": False, "error": "session agent mismatch"}))
            return
        current_session_id.set(uuid.UUID(SPEC["session_id"]))
        current_agent_id.set(SPEC["agent_id"])
        try:
            result = await asyncio.wait_for(
                registry.execute(SPEC["tool"], **SPEC["kwargs"]), timeout=SPEC["timeout"]
            )
            text = str(result)
            print(json.dumps({"ok": True, "result_len": len(text), "result_preview": text[:400]}))
        except Exception as error:
            print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
    finally:
        await db.close()

asyncio.run(main())
"""

_MCP_FIRECRAWL_SCRIPT = """
import asyncio, json, os, sys
sys.path.insert(0, "__REPO__")

SPEC = json.loads(sys.argv[1])
os.environ.setdefault("FIRECRAWL_API_URL", SPEC.get("api_url", "http://localhost:3002"))

from machina.db.connection import db

def emit(payload):
    print(json.dumps(payload, default=str))

async def main():
    await db.connect()
    service = None
    connector_id = None
    try:
        from machina.core.runtime import runtime_services
        service = runtime_services.get_connector_service()
        await service.refresh()
        configs = [c for c in await service.list() if "firecrawl" in c["id"].lower()]
        if not configs:
            emit({"skip": "no firecrawl connector configured in ~/.machina/mcp.json"})
            return
        connector_id = configs[0]["id"]

        from machina.connectors.execution import (
            call_spec,
            connect_authorized,
            execute_call,
            prepare_connect,
        )
        from machina.core.execution_context import resolve_execution_context
        from machina.permissions import broker as broker_mod
        from machina.permissions import store as store_mod
        from machina.permissions.policy import build_request

        session_id = SPEC["session_id"]
        agent_id = SPEC["agent_id"]
        try:
            prepared = await prepare_connect(service, connector_id, session_id, agent_id)
            await connect_authorized(
                service, connector_id, session_id, agent_id, prepared["digest"]
            )
        except Exception as error:
            emit({"skip": f"firecrawl connector unavailable: {type(error).__name__}: {error}"})
            return

        names = [tool.name for tool in service.registry.tools_for(connector_id)]
        tool_name = next(
            (n for n in names if n == "firecrawl__firecrawl_scrape"),
            next((n for n in names if "scrape" in n), ""),
        )
        if not tool_name:
            emit({"skip": f"no scrape tool exposed by {connector_id}: {names}"})
            return
        arguments = {"url": SPEC["url"], "formats": ["markdown"]}

        context = await resolve_execution_context(session_id, "mcp_call")
        spec = call_spec(service, {"name": tool_name, "arguments": arguments})
        request = build_request(
            **spec,
            tool="mcp_call",
            mode=context.mode,
            backend=context.backend,
            session_id=session_id,
            agent_id=agent_id,
        )

        async def reviewed(card):
            record = await store_mod.resolve_decision(
                card["request_id"], "approved", "mcp-management"
            )
            return "approved" if record is not None else "denied"

        tokens = [
            (broker_mod._approval_handler, broker_mod._approval_handler.set(reviewed)),
            (broker_mod._principal, broker_mod._principal.set("mcp-management")),
        ]
        try:
            await broker_mod.permission_broker.guard(request)
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

        outcome = await execute_call(
            service, tool_name, arguments, session_id=session_id, agent_id=agent_id
        )
        text = outcome.text or ""
        structured = outcome.structured
        emit(
            {
                "ok": bool(outcome.ok),
                "tool": tool_name,
                "text_len": len(text),
                "text_preview": text[:400],
                "structured_len": len(json.dumps(structured, default=str)) if structured else 0,
                "structured_preview": json.dumps(structured, default=str)[:400] if structured else "",
                "outcome_unknown": bool(outcome.outcome_unknown),
                "error": outcome.error or "",
            }
        )
    finally:
        try:
            if service is not None and connector_id:
                await service.manager.disconnect(connector_id)
        except Exception:
            pass
        await db.close()

asyncio.run(main())
"""


def call_script(
    ctx: dict, script: str, params: dict | None = None, timeout: float = 60.0
) -> tuple[dict, dict]:
    """Run an embedded venv-python script; return (raw subprocess, parsed JSON)."""
    cmd = [str(ctx["venv_python"]), "-c", script]
    if params is not None:
        cmd.append(json.dumps(params))
    raw = run_cmd(cmd, cwd=REPO_ROOT, env=child_env(), timeout=timeout)
    payload: dict = {
        "ok": False,
        "error": f"exit={raw['exit_code']} produced no JSON; stderr tail: {raw['stderr'][-300:]}",
    }
    for line in reversed(raw["stdout"].splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return raw, payload


def setup_session(ctx: dict, jid: str, goal: str) -> tuple[str | None, dict, dict]:
    raw, payload = call_script(
        ctx,
        _SESSION_SETUP_SCRIPT,
        {"title": f"e2e-gate {jid}", "goal": goal, "agent_id": "harness"},
        timeout=120,
    )
    return payload.get("session_id"), raw, payload


def revoke_session(ctx: dict, session_id: str, record: dict) -> None:
    raw, payload = call_script(
        ctx,
        _SESSION_REVOKE_SCRIPT,
        {"session_id": session_id, "agent_id": "harness"},
        timeout=60,
    )
    record["observed"]["revoked_grants"] = payload.get("revoked_grants")
    record["observed"]["revoke_ok"] = bool(payload.get("ok"))
    if raw["exit_code"] != 0 or not payload.get("ok"):
        record["notes"].append(f"revoke did not confirm: exit={raw['exit_code']}")


def db_query(ctx: dict, queries: list[dict], timeout: float = 60.0) -> dict:
    raw, payload = call_script(ctx, _DB_QUERY_SCRIPT, {"queries": queries}, timeout=timeout)
    if raw["exit_code"] != 0 or not payload.get("ok"):
        raise RuntimeError(
            f"database query failed: exit={raw['exit_code']} "
            f"{payload.get('error', raw['stderr'][-300:])}"
        )
    return payload["results"]


def record_cli_checks(record: dict, raw: dict, label: str) -> None:
    hits = error_hits(raw["stderr"])
    check(
        record,
        f"{label}_exit_zero",
        raw["exit_code"] == 0 and not raw["timed_out"],
        "exit code 0 (no timeout)",
        {"exit_code": raw["exit_code"], "timed_out": raw["timed_out"]},
    )
    check(
        record,
        f"{label}_no_error_substrings",
        not hits,
        f"stderr free of {list(ERROR_SUBSTRINGS)}",
        {"hits": hits, "stderr_excerpt": raw["stderr"][-400:]},
    )
    record["observed"][f"{label}_wall_seconds"] = raw["wall_seconds"]


# ---------------------------------------------------------------------------
# Journeys
# ---------------------------------------------------------------------------


def run_j1(ctx: dict) -> dict:
    record = new_record("j1")
    scratch = ctx["scratch_root"] / "j1"
    scratch.mkdir(parents=True, exist_ok=True)
    session_id, _, setup_payload = setup_session(
        ctx, "j1", "e2e gate: prove chat -> consolidation -> memories roundtrip"
    )
    if not session_id:
        return finish(record, "FAIL", f"session setup failed: {setup_payload}")
    record["observed"]["session_id"] = session_id

    # Collision-proof fact: near-duplicate memories are deduplicated against
    # stored rows, so a unique fixture name is required for a repeatable gate.
    pet_name = f"Zephyrine{random.randint(1000, 9999)}"
    fixture_fact = f"pet turtle named {pet_name}"
    record["observed"]["fixture_fact"] = fixture_fact
    prompt = (
        f"I just adopted a pet turtle named {pet_name}. Please remember that fact about me. "
        "Reply with exactly: NOTED"
    )
    cli = run_cmd(
        [str(ctx["machina_bin"]), "chat", "--session", session_id, prompt],
        cwd=scratch,
        env=child_env(),
        timeout=ctx["timeout"],
    )
    record["commands"].append(
        f'cd {scratch} && machina chat --session {session_id} "<facts prompt>"'
    )
    record_cli_checks(record, cli, "cli")
    record["observed"]["cli_reply_excerpt"] = cli["stdout"].strip()[-300:]
    # The assert/consolidate scripts take the session id as a positional argv[1].
    _, state = _run_j1_assert(ctx, session_id)
    cursor_after_cli = state.get("cursor")
    record["observed"]["cursor_after_cli"] = cursor_after_cli
    record["observed"]["memory_count_after_cli"] = state.get("memory_count")

    consolidate_raw, consolidate_payload = _run_j1_consolidate(ctx, session_id)
    record["commands"].append(
        f'{ctx["venv_python"]} -c "<build_agent_for_session + _consolidate_memories>" {session_id}'
    )
    check(
        record,
        "consolidation_call_ok",
        consolidate_raw["exit_code"] == 0 and bool(consolidate_payload.get("ok")),
        "explicit production consolidation call succeeds",
        {
            "ok": bool(consolidate_payload.get("ok")),
            "error": consolidate_payload.get("error", ""),
        },
    )
    check(
        record,
        "consolidator_wired",
        bool(consolidate_payload.get("consolidator_wired")),
        "agent_factory wires a memory consolidator (provider passed)",
        consolidate_payload.get("consolidator_wired"),
    )

    _, state = _run_j1_assert(ctx, session_id)
    memories = state.get("memories") or []
    blob = " ".join(str(item.get("content", "")) for item in memories).casefold()
    cursor = state.get("cursor") or {}
    record["observed"]["session_found"] = state.get("session_found")
    record["observed"]["memory_count_final"] = state.get("memory_count")
    record["observed"]["memories"] = memories
    record["observed"]["cursor_final"] = cursor
    check(
        record,
        "memory_row_for_session",
        bool(memories),
        ">=1 memories row written for this session",
        state.get("memory_count"),
    )
    check(
        record,
        "memory_content_fixture",
        pet_name.casefold() in blob,
        f"memories row content contains '{pet_name}'",
        [item.get("content") for item in memories],
    )
    check(
        record,
        "session_cursor_set",
        bool(cursor.get("id")) and bool(cursor.get("at")),
        "sessions.state.mem_consolidated_up_to has id + at",
        cursor,
    )
    revoke_session(ctx, session_id, record)

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def _run_j1_assert(ctx: dict, session_id: str) -> tuple[dict, dict]:
    cmd = [str(ctx["venv_python"]), "-c", _J1_ASSERT_SCRIPT, session_id]
    raw = run_cmd(cmd, cwd=REPO_ROOT, env=child_env(), timeout=60)
    return raw, _last_json(raw)


def _run_j1_consolidate(ctx: dict, session_id: str) -> tuple[dict, dict]:
    cmd = [str(ctx["venv_python"]), "-c", _J1_CONSOLIDATE_SCRIPT, session_id]
    raw = run_cmd(cmd, cwd=REPO_ROOT, env=child_env(), timeout=ctx["timeout"])
    return raw, _last_json(raw)


def _last_json(raw: dict) -> dict:
    for line in reversed(raw["stdout"].splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "error": f"exit={raw['exit_code']} produced no JSON"}


def run_j2(ctx: dict) -> dict:
    record = new_record("j2")
    session_id, _, setup_payload = setup_session(
        ctx, "j2", "e2e gate: real web research indexes docs.python.org json page"
    )
    if not session_id:
        return finish(record, "FAIL", f"session setup failed: {setup_payload}")
    record["observed"]["session_id"] = session_id
    record["commands"].append(
        f'registry.execute("web_research", query="{JSON_QUERY}", limit=1) in-session {session_id}'
    )
    attempts = 0
    raw: dict = {}
    payload: dict = {}
    for attempts in range(1, 3):
        raw, payload = call_script(
            ctx,
            _TOOL_CALL_SCRIPT,
            {
                "session_id": session_id,
                "agent_id": "harness",
                "tool": "web_research",
                "kwargs": {"query": JSON_QUERY, "limit": 1},
                "timeout": 120,
            },
            timeout=180,
        )
        if payload.get("ok") or "no search results" not in str(payload.get("error", "")):
            break
        record["notes"].append(
            f"attempt {attempts}: search backend returned no results; retrying once"
        )
    record["observed"]["attempts"] = attempts
    record["observed"]["tool_exit_code"] = raw["exit_code"]
    record["observed"]["tool_result_preview"] = payload.get("result_preview", "")
    check(
        record,
        "web_research_ok",
        bool(payload.get("ok")),
        "web_research completes without raising",
        {"ok": bool(payload.get("ok")), "error": payload.get("error", "")},
    )
    try:
        results = db_query(
            ctx,
            [
                {
                    "name": "chunks",
                    "sql": (
                        "SELECT COUNT(*)::int AS chunk_count FROM context_chunks "
                        "WHERE session_id = $1::uuid AND chunk_type = 'document' "
                        "AND document_source = $2"
                    ),
                    "args": [session_id, JSON_PAGE],
                },
                {
                    "name": "sources",
                    "sql": (
                        "SELECT document_source, COUNT(*)::int AS chunk_count FROM context_chunks "
                        "WHERE session_id = $1::uuid AND document_source IS NOT NULL "
                        "GROUP BY document_source ORDER BY chunk_count DESC"
                    ),
                    "args": [session_id],
                },
            ],
        )
        chunk_count = results["chunks"][0]["chunk_count"] if results["chunks"] else 0
        record["observed"]["chunks_for_json_page"] = chunk_count
        record["observed"]["indexed_sources"] = results["sources"]
        check(
            record,
            "chunks_for_json_page",
            chunk_count >= 1,
            f">=1 context_chunk row with document_source={JSON_PAGE}",
            chunk_count,
        )
    except Exception as error:  # noqa: BLE001 - recorded as a failed check below
        check(record, "chunks_query", False, "database query succeeds", str(error))
    revoke_session(ctx, session_id, record)

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def run_j3(ctx: dict) -> dict:
    record = new_record("j3")
    session_id, _, setup_payload = setup_session(
        ctx, "j3", "e2e gate: real web crawl indexes the json docs page"
    )
    if not session_id:
        return finish(record, "FAIL", f"session setup failed: {setup_payload}")
    record["observed"]["session_id"] = session_id
    record["commands"].append(
        f'registry.execute("web_crawl", url="{JSON_PAGE}", max_pages=1, max_depth=0) '
        f"in-session {session_id}"
    )
    raw, payload = call_script(
        ctx,
        _TOOL_CALL_SCRIPT,
        {
            "session_id": session_id,
            "agent_id": "harness",
            "tool": "web_crawl",
            "kwargs": {"url": JSON_PAGE, "max_pages": 1, "max_depth": 0, "same_host": True},
            "timeout": 120,
        },
        timeout=180,
    )
    record["observed"]["tool_exit_code"] = raw["exit_code"]
    record["observed"]["tool_result_preview"] = payload.get("result_preview", "")
    check(
        record,
        "web_crawl_ok",
        bool(payload.get("ok")),
        "web_crawl completes without raising",
        {"ok": bool(payload.get("ok")), "error": payload.get("error", "")},
    )
    try:
        results = db_query(
            ctx,
            [
                {
                    "name": "chunks",
                    "sql": (
                        "SELECT COUNT(*)::int AS chunk_count FROM context_chunks "
                        "WHERE session_id = $1::uuid AND chunk_type = 'document' "
                        "AND document_source = $2"
                    ),
                    "args": [session_id, JSON_PAGE],
                }
            ],
        )
        chunk_count = results["chunks"][0]["chunk_count"] if results["chunks"] else 0
        record["observed"]["chunks_for_source"] = chunk_count
        check(
            record,
            "chunks_for_source",
            chunk_count >= 1,
            f">=1 context_chunk row with document_source={JSON_PAGE}",
            chunk_count,
        )
    except Exception as error:  # noqa: BLE001
        check(record, "chunks_query", False, "database query succeeds", str(error))
    revoke_session(ctx, session_id, record)

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_j4(ctx: dict) -> dict:
    record = new_record("j4")
    api_url = os.environ.get("FIRECRAWL_API_URL", FIRECRAWL_DEFAULT_URL)
    record["observed"]["firecrawl_api_url"] = api_url
    parsed = urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _port_open(host, port):
        return finish(
            record,
            "SKIP",
            f"self-hosted Firecrawl stack unreachable at {host}:{port} (optional journey)",
        )
    session_id, _, setup_payload = setup_session(
        ctx, "j4", "e2e gate: firecrawl MCP scrape through the broker-approved path"
    )
    if not session_id:
        return finish(record, "FAIL", f"session setup failed: {setup_payload}")
    record["observed"]["session_id"] = session_id
    record["commands"].append(
        "os.environ['FIRECRAWL_API_URL']="
        f"'{api_url}'; connect_authorized(service,'firecrawl',...) + "
        "execute_call(service,'firecrawl__firecrawl_scrape',{'url':'https://example.com',"
        "'formats':['markdown']})"
    )
    raw, payload = call_script(
        ctx,
        _MCP_FIRECRAWL_SCRIPT,
        {"session_id": session_id, "agent_id": "harness", "url": "https://example.com"},
        timeout=ctx["timeout"],
    )
    record["observed"]["script_exit_code"] = raw["exit_code"]
    if payload.get("skip"):
        revoke_session(ctx, session_id, record)
        return finish(record, "SKIP", str(payload["skip"]))
    record["observed"]["tool"] = payload.get("tool")
    record["observed"]["text_len"] = payload.get("text_len")
    record["observed"]["text_preview"] = payload.get("text_preview", "")
    record["observed"]["structured_len"] = payload.get("structured_len")
    record["observed"]["outcome_unknown"] = payload.get("outcome_unknown")
    combined = f"{payload.get('text_preview', '')} {payload.get('structured_preview', '')}".lower()
    record["observed"]["contains_example_domain"] = "example domain" in combined
    check(
        record,
        "mcp_call_ok",
        bool(payload.get("ok")),
        "CallOutcome.ok is True",
        {"ok": bool(payload.get("ok")), "error": payload.get("error", "")},
    )
    check(
        record,
        "markdown_non_empty",
        bool(combined.strip()),
        "scrape result carries non-empty text/structured markdown",
        {"text_len": payload.get("text_len"), "structured_len": payload.get("structured_len")},
    )
    revoke_session(ctx, session_id, record)

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def run_j5(ctx: dict) -> dict:
    record = new_record("j5")
    scratch = ctx["scratch_root"] / "j5"
    scratch.mkdir(parents=True, exist_ok=True)
    cli = run_cmd(
        [str(ctx["machina_bin"]), "chat", "Reply with exactly: OK", "-q"],
        cwd=scratch,
        env=child_env(),
        timeout=ctx["timeout"],
    )
    record["commands"].append(f'cd {scratch} && machina chat "Reply with exactly: OK" -q')
    record_cli_checks(record, cli, "cli")
    record["observed"]["stdout_contains_ok"] = "ok" in cli["stdout"].casefold()
    record["observed"]["stdout_excerpt"] = cli["stdout"].strip()[-200:]
    check(
        record,
        "cli_reply_contains_ok",
        "ok" in cli["stdout"].casefold(),
        'stdout contains "OK" (the requested reply)',
        {"stdout_excerpt": cli["stdout"].strip()[-200:]},
    )

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


JOURNEYS = {
    "j1": run_j1,
    "j2": run_j2,
    "j3": run_j3,
    "j4": run_j4,
    "j5": run_j5,
}


# ---------------------------------------------------------------------------
# Proof files
# ---------------------------------------------------------------------------


def render_markdown(doc: dict) -> str:
    lines: list[str] = []
    lines.append(f"# E2E gate proof — {doc['generated_at_utc'][:10]}")
    lines.append("")
    lines.append(f"- **Generated:** {doc['generated_at_utc']}")
    lines.append(f"- **Command:** `{doc['command_line']}`")
    lines.append(f"- **Repo:** `{doc['repo_root']}`")
    git = doc.get("git") or {}
    lines.append(
        f"- **Git HEAD:** `{git.get('git_head')}` (status sha256_16 `{git.get('status_sha256_16')}`)"
    )
    lines.append(f"- **Venv python:** `{doc['venv_python']}`")
    lines.append(
        f"- **Summary:** {doc['summary']['passed']} PASS, {doc['summary']['failed']} FAIL, "
        f"{doc['summary']['skipped']} SKIP of {doc['summary']['total']} journey(s)"
    )
    lines.append("- **Policy:** `docs/verification/E2E_PROTOCOL.md`")
    lines.append("")
    for journey in doc["journeys"]:
        lines.append(f"## {journey['journey']} — {journey['title']} — {journey['status']}")
        lines.append("")
        lines.append(
            f"- Started: {journey['started_at']} | finished: {journey['finished_at']} | "
            f"duration: {journey['duration_seconds']}s"
        )
        if journey.get("reason"):
            lines.append(f"- Reason: {journey['reason']}")
        if journey["commands"]:
            lines.append("- Commands:")
            for command in journey["commands"]:
                lines.append(f"  - `{command}`")
        if journey.get("checks"):
            lines.append("")
            lines.append("| check | result | expected | observed |")
            lines.append("| --- | --- | --- | --- |")
            for item in journey["checks"]:
                observed = json.dumps(item["observed"], default=str)
                if len(observed) > 300:
                    observed = observed[:297] + "..."
                observed = observed.replace("|", "\\|")
                expected = str(item["expected"]).replace("|", "\\|")
                lines.append(
                    f"| {item['name']} | {'PASS' if item['ok'] else 'FAIL'} | {expected} | {observed} |"
                )
        lines.append("")
        lines.append("Observed values:")
        lines.append("")
        lines.append("```json")
        observed = json.dumps(journey.get("observed", {}), indent=2, default=str)
        lines.append(observed[:6000])
        lines.append("```")
        if journey.get("notes"):
            lines.append("")
            for note in journey["notes"]:
                lines.append(f"> note: {note}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_proofs(doc: dict, json_out: str | None) -> dict:
    PROOFS_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    json_path = PROOFS_DIR / f"{date}-e2e-gate.json"
    md_path = PROOFS_DIR / f"{date}-e2e-gate.md"
    document = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    json_path.write_text(document, encoding="utf-8")
    md_path.write_text(render_markdown(doc), encoding="utf-8")
    if json_out:
        override = Path(json_out).expanduser()
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(document, encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "json_out": json_out or ""}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--journeys",
        default=",".join(JOURNEY_IDS),
        help=f"comma-separated subset of {','.join(JOURNEY_IDS)} (default: all)",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="extra JSON report path")
    parser.add_argument("--venv", default=str(DEFAULT_VENV), help="repo venv directory")
    parser.add_argument(
        "--timeout", type=float, default=240.0, help="per agent/CLI command timeout (s)"
    )
    parser.add_argument(
        "--scratch-dir", default=None, help="scratch directory (default: created in temp)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [part.strip() for part in args.journeys.split(",") if part.strip()]
    unknown = [part for part in selected if part not in JOURNEY_IDS]
    if unknown:
        log(f"unknown journeys: {unknown}; valid: {list(JOURNEY_IDS)}")
        return 2
    venv_python = Path(args.venv).expanduser().resolve() / "bin" / "python"
    machina_bin = venv_python.parent / "machina"
    if not venv_python.is_file():
        log(f"venv python not found: {venv_python}")
        return 2
    if not machina_bin.is_file():
        log(f"machina CLI not found: {machina_bin}")
        return 2

    if args.scratch_dir:
        scratch_root = Path(args.scratch_dir).expanduser().resolve()
        scratch_root.mkdir(parents=True, exist_ok=True)
    else:
        scratch_root = Path(tempfile.mkdtemp(prefix="machina-e2e-gate-"))
    ctx = {
        "repo_root": REPO_ROOT,
        "venv_python": venv_python,
        "machina_bin": machina_bin,
        "scratch_root": scratch_root,
        "timeout": args.timeout,
    }

    doc = {
        "schema_version": 1,
        "tool": "tools/e2e_gate.py",
        "generated_at_utc": utc_now(),
        "command_line": " ".join([sys.executable, "tools/e2e_gate.py", *sys.argv[1:]]),
        "repo_root": str(REPO_ROOT),
        "git": git_capture(REPO_ROOT),
        "venv_python": str(venv_python),
        "scratch_root": str(scratch_root),
        "journeys_selected": selected,
        "journeys": [],
        "summary": {"passed": 0, "failed": 0, "skipped": 0, "total": len(selected)},
    }

    for jid in selected:
        log(f"running {jid}: {JOURNEY_TITLES[jid]}")
        start = time.monotonic()
        try:
            record = JOURNEYS[jid](ctx)
        except Exception as error:  # noqa: BLE001 - a crashed journey is a FAIL, not a traceback dump
            record = new_record(jid)
            record["reason"] = f"unhandled {type(error).__name__}: {error}"
            finish(record, "FAIL", record["reason"])
        record["duration_seconds"] = round(time.monotonic() - start, 3)
        record.setdefault("notes", []).append(f"scratch: {scratch_root}")
        doc["journeys"].append(record)
        if record["status"] == "PASS":
            doc["summary"]["passed"] += 1
        elif record["status"] == "SKIP":
            doc["summary"]["skipped"] += 1
        else:
            doc["summary"]["failed"] += 1
        log(f"{jid}: {record['status']}" + (f" — {record['reason']}" if record["reason"] else ""))

    doc["proof_files"] = write_proofs(doc, args.json_out)
    print(
        f"e2e-gate: {doc['summary']['passed']} PASS / {doc['summary']['failed']} FAIL / "
        f"{doc['summary']['skipped']} SKIP (proof: {doc['proof_files']['markdown']})"
    )
    return 1 if doc["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
