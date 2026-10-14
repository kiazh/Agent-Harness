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
import http.client
import json
import os
import queue
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = REPO_ROOT / ".venv"
PROOFS_DIR = REPO_ROOT / "docs" / "verification" / "proofs"
JOURNEY_IDS = ("j1", "j2", "j3", "j4", "j5", "j6", "j7", "j8", "j9")
JOURNEY_TITLES = {
    "j1": "memory-roundtrip (chat -> consolidate -> memories row + cursor)",
    "j2": "web-research-rag (real web_research -> context_chunks for source)",
    "j3": "web-crawl (real web_crawl -> indexed chunks for source)",
    "j4": "mcp-firecrawl (connector connect -> execute_call scrape -> markdown)",
    "j5": "cli-smoke (machina chat exit 0, no InterfaceError / extraction failure)",
    "j6": (
        "compaction-triggers (SSE auto + gateway auto + explicit compress; archive/summary/state)"
    ),
    "j7": "turn-parity (SSE turn -> stdio gateway turn -> SSE turn; frames, transcript, claims)",
    "j8": "provider-facade (config route -> real call via usage_store -> llm_usage row + status)",
    "j9": "opencode-mcp (opencode run -> machina_chat tool call -> Machina session row contains token)",
}
# Per-journey wall-clock budgets (sum of bounded steps). Recorded as notes on
# every run; a journey is expected to finish well under its budget.
JOURNEY_TIMEOUT_BUDGET_SECONDS = {
    "j1": 300.0,
    "j2": 240.0,
    "j3": 240.0,
    "j4": 300.0,
    "j5": 300.0,
    "j6": 720.0,
    "j7": 600.0,
    "j8": 300.0,
    "j9": 420.0,
}
ERROR_SUBSTRINGS = ("InterfaceError", "Memory extraction failed")
FIRECRAWL_DEFAULT_URL = "http://localhost:3002"
JSON_PAGE = "https://docs.python.org/3/library/json.html"
# No `site:` operator: the DuckDuckGo HTML endpoint intermittently drops those
# entirely (a real gate run failed on it), and the journey must not depend on
# a fragile operator. One recorded retry below absorbs provider flake — never
# an assertion's strength.
JSON_QUERY = "python json encoder and decoder documentation"
# j6 forces the post-turn compaction trigger to fire on otherwise tiny
# sessions (the threshold branch must not mask the trigger/guard paths under
# test). It is set on the journey's own child processes only; assertions stay
# strict and the observed effective value is recorded.
E2E_COMPRESSION_THRESHOLD = "0.001"
SSE_DONE_SENTINEL = "[DONE]"
SSE_PROMPT_PATH = "/api/v1/sessions/{session_id}/prompt"


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
    except Exception as error:  # provenance must never kill a run
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

_SEED_COMPACTION_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

async def main():
    await db.connect()
    try:
        await db.initialize_runtime_schema()
        from machina.core.context import context_manager
        from machina.core.session import session_manager
        from machina.core.session_mode import activate_durable_mode

        session = await session_manager.create(
            title=SPEC["title"], goal=SPEC["goal"], agent_id=SPEC["agent_id"]
        )
        await activate_durable_mode(str(session.id), SPEC["agent_id"], "full")
        filler = " ".join(f"seed{i}" for i in range(40))
        chunks = [
            {
                "session_id": session.id,
                "agent_id": SPEC["agent_id"],
                "chunk_type": "assistant_message",
                "payload": {"content": f"{SPEC['marker']}-{i} {filler}"},
                "token_count": 400,
            }
            for i in range(SPEC["seed_count"])
        ]
        chunks.append(
            {
                "session_id": session.id,
                "agent_id": SPEC["agent_id"],
                "chunk_type": "document",
                "payload": {"content": f"{SPEC['marker']}-DOC fixture document"},
                "token_count": 1500,
                "document_source": SPEC["document_source"],
            }
        )
        inserted = await context_manager.add_chunks_batch(chunks)
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": str(session.id),
                    "seeded_ids": [str(c.id) for c in inserted[:-1]],
                    "document_id": str(inserted[-1].id),
                }
            )
        )
    finally:
        await db.close()

asyncio.run(main())
"""

_J6_STATE_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
import msgpack
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])
SID = SPEC["session_id"]

async def main():
    await db.connect()
    try:
        row = await db.fetchrow(
            "SELECT state_msgpack, claim_owner, claim_kind FROM sessions WHERE id = $1::uuid",
            SID,
        )
        state = {}
        if row is not None and row["state_msgpack"]:
            state = msgpack.unpackb(row["state_msgpack"], raw=False)
        summary = await db.fetchrow(
            "SELECT COUNT(*)::int AS n, "
            "(SELECT id::text FROM context_chunks WHERE session_id = $1::uuid "
            " AND chunk_type = 'compression_summary' "
            " ORDER BY created_at DESC, id DESC LIMIT 1) AS latest_id "
            "FROM context_chunks WHERE session_id = $1::uuid "
            "AND chunk_type = 'compression_summary'",
            SID,
        )
        archive = await db.fetch(
            "SELECT chunk_id::text AS chunk_id FROM context_archive "
            "WHERE session_id = $1::uuid AND archive_reason = 'compressed'",
            SID,
        )
        document_present = None
        document_archived = None
        if SPEC.get("document_id"):
            document_present = bool(
                await db.fetchval(
                    "SELECT COUNT(*)::int FROM context_chunks WHERE id = $1::uuid",
                    SPEC["document_id"],
                )
            )
            document_archived = bool(
                await db.fetchval(
                    "SELECT COUNT(*)::int FROM context_archive WHERE chunk_id = $1::uuid",
                    SPEC["document_id"],
                )
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_found": row is not None,
                    "summary_count": int(summary["n"]) if summary else 0,
                    "latest_summary_id": summary["latest_id"] if summary else None,
                    "archive_count": len(archive),
                    "archived_chunk_ids": [r["chunk_id"] for r in archive[:50]],
                    "document_present": document_present,
                    "document_archived": document_archived,
                    "claim_owner": row["claim_owner"] if row is not None else None,
                    "claim_kind": row["claim_kind"] if row is not None else None,
                    "last_auto_compact_tokens": state.get("last_auto_compact_tokens"),
                },
                default=str,
            )
        )
    finally:
        await db.close()

asyncio.run(main())
"""

_J7_TRANSCRIPT_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
import msgpack
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])
SID = SPEC["session_id"]

async def main():
    await db.connect()
    try:
        rows = await db.fetch(
            "SELECT chunk_type, payload_msgpack FROM context_chunks "
            "WHERE session_id = $1::uuid ORDER BY created_at ASC, id ASC",
            SID,
        )
        user_blob = []
        assistant_count = 0
        for row in rows:
            chunk_type = row["chunk_type"]
            if chunk_type not in ("user_message", "assistant_message"):
                continue
            try:
                payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
            except Exception:
                payload = {}
            content = str(payload.get("content", ""))
            if chunk_type == "user_message":
                user_blob.append(content)
            else:
                assistant_count += 1
        blob = "\\n".join(user_blob).casefold()
        print(
            json.dumps(
                {
                    "ok": True,
                    "user_message_count": len(user_blob),
                    "assistant_message_count": assistant_count,
                    "user_tokens": {
                        token: (str(token).casefold() in blob) for token in SPEC["tokens"]
                    },
                },
                default=str,
            )
        )
    finally:
        await db.close()

asyncio.run(main())
"""

_CONFIG_PROBE_SCRIPT = """
import json, sys
sys.path.insert(0, "__REPO__")
from machina.core.config import config

print(json.dumps({"ok": True, "provider": config.get("provider"), "model": config.get("model")}))
"""

_J8_PROVIDER_SCRIPT = """
import asyncio, json, sys, uuid
sys.path.insert(0, "__REPO__")
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])

async def main():
    await db.connect()
    provider = None
    try:
        import machina.core.provider as provider_mod
        from machina.core.config import config
        from machina.core.provider import (
            PROVIDERS,
            PROVIDER_SPECS,
            LLMProvider,
            get_provider,
        )
        from machina.core.runtime import _detect_providers
        from machina.core.usage import usage_store

        facade_missing = [
            name for name in provider_mod.__all__ if not hasattr(provider_mod, name)
        ]
        selected = str(config.get("provider") or "")
        model = str(config.get("model") or "")
        spec = PROVIDER_SPECS.get(selected)
        detection = _detect_providers()
        provider = get_provider(provider=selected, model=model)
        prompt = f"Reply with exactly: {SPEC['token']}"
        response = await usage_store.complete_call(
            provider,
            uuid.UUID(SPEC["session_id"]),
            SPEC["agent_id"],
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=64,
            temperature=0.0,
        )
        content = str(getattr(response, "content", "") or "")
        usage = getattr(response, "usage", None) or {}
        total_tokens = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
        print(
            json.dumps(
                {
                    "ok": True,
                    "selected": selected,
                    "model": model,
                    "expected_key_env": spec.api_key_env if spec is not None else None,
                    "facade_missing": facade_missing,
                    "facade_consistent": bool(
                        not facade_missing
                        and selected in PROVIDERS
                        and set(PROVIDER_SPECS)
                        | {"openrouter", "anthropic", "chatgpt", "custom", "ollama"}
                        == set(PROVIDERS)
                    ),
                    "chat_provider": detection.get("chat_provider"),
                    "chat_provider_selected": detection.get("chat_provider_selected"),
                    "provider_class": type(provider).__name__,
                    "provider_is_llm": isinstance(provider, LLMProvider),
                    "provider_model": str(getattr(provider, "model", "")),
                    "response_model": str(getattr(response, "model", "") or ""),
                    "contains_token": SPEC["token"].casefold() in content.casefold(),
                    "content_excerpt": content[-200:],
                    "total_tokens": total_tokens,
                },
                default=str,
            )
        )
    except Exception as error:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}
            )
        )
    finally:
        try:
            if provider is not None:
                close = getattr(provider, "close", None)
                if callable(close):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
        except Exception:
            pass
        await db.close()

asyncio.run(main())
"""


_J9_ASSERT_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, "__REPO__")
import msgpack
from machina.db.connection import db

SPEC = json.loads(sys.argv[1])
TOKEN = SPEC["token"]

async def main():
    await db.connect()
    try:
        rows = await db.fetch(
            "SELECT c.session_id, c.chunk_type, c.payload_msgpack, c.created_at, s.title "
            "FROM context_chunks c JOIN sessions s ON s.id = c.session_id "
            "WHERE c.created_at > now() - interval '30 minutes' "
            "ORDER BY c.created_at DESC LIMIT 400"
        )
        matches = []
        for row in rows:
            try:
                payload = msgpack.unpackb(row["payload_msgpack"], raw=False)
            except Exception:
                continue
            if TOKEN not in json.dumps(payload, default=str):
                continue
            matches.append(
                {
                    "session_id": str(row["session_id"]),
                    "chunk_type": row["chunk_type"],
                    "title": row["title"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
            )
        print(json.dumps({"ok": True, "scanned": len(rows), "matches": matches[:5]}))
    finally:
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
# Loopback transport helpers (j6/j7): real uvicorn server + real stdio gateway
# ---------------------------------------------------------------------------


def pick_port() -> int:
    """Allocate a free loopback port for a short-lived real server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def uvicorn_available(ctx: dict) -> bool:
    """j6/j7 SKIP probe: the venv must ship uvicorn for the HTTP stack."""
    raw = run_cmd(
        [str(ctx["venv_python"]), "-c", "import uvicorn; print('uvicorn-ok')"],
        cwd=REPO_ROOT,
        env=child_env(),
        timeout=60,
    )
    return raw["exit_code"] == 0 and "uvicorn-ok" in raw["stdout"]


def _bounded_tail(text: object, limit: int = 300) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[-limit:]


def _tail_text(path: Path, limit: int = 400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _json_or_excerpt(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"raw_excerpt": _bounded_tail(text, 300)}
    return payload if isinstance(payload, dict) else {"payload": payload}


def http_json(
    base_url: str,
    method: str,
    path: str,
    api_key: str | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    """One real HTTP call; returns (status, parsed JSON or a bounded excerpt)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_or_excerpt(response.read(65536))
    except urllib.error.HTTPError as error:
        return error.code, _json_or_excerpt(error.read(65536))
    except Exception as error:  # reported by the caller as a failed check
        return 0, {"error": f"{type(error).__name__}: {error}"}


class ApiServer:
    """A real ``python -m machina.api`` child over loopback (no mocks).

    stdout/stderr go to scratch files (no pipe-buffer deadlocks); readiness is
    the real ``GET /ready`` (DB + schema); teardown always kills the process
    group. The ephemeral API key lives only in the child env and in the
    caller's local variable — never in any recorded command or observation.
    """

    def __init__(self, ctx: dict, name: str, scratch: Path, env_overrides: dict[str, str]) -> None:
        self.host = "127.0.0.1"
        self.port = pick_port()
        self.base_url = f"http://{self.host}:{self.port}"
        self.out_path = scratch / f"{name}.server.out"
        self.err_path = scratch / f"{name}.server.err"
        self._venv_python = str(ctx["venv_python"])
        env = child_env()
        env.update(env_overrides)
        stdout = open(self.out_path, "wb")
        stderr = open(self.err_path, "wb")
        self.proc = subprocess.Popen(
            [
                self._venv_python,
                "-m",
                "machina.api",
                "--host",
                self.host,
                "--port",
                str(self.port),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        stdout.close()
        stderr.close()

    def command_display(self) -> str:
        return (
            f"MACHINA_API_KEY=<redacted> {self._venv_python} -m machina.api "
            f"--host {self.host} --port {self.port}"
        )

    def wait_ready(self, deadline_s: float = 60.0) -> dict:
        start = time.monotonic()
        last_status = 0
        while time.monotonic() - start < deadline_s:
            if self.proc.poll() is not None:
                break
            try:
                last_status, _ = http_json(self.base_url, "GET", "/ready", timeout=3.0)
            except Exception:  # retried until the deadline
                last_status = 0
            if last_status == 200:
                return {
                    "ready": True,
                    "waited_seconds": round(time.monotonic() - start, 3),
                    "last_status": last_status,
                }
            time.sleep(0.5)
        return {
            "ready": False,
            "waited_seconds": round(time.monotonic() - start, 3),
            "last_status": last_status,
            "exit_code": self.proc.poll(),
            "stderr_excerpt": _tail_text(self.err_path),
        }

    def stop(self, timeout: float = 15.0) -> dict:
        if self.proc.poll() is None:
            _kill_group(self.proc, signal.SIGTERM)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(self.proc, signal.SIGKILL)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        return {"exit_code": self.proc.returncode, "stderr_excerpt": _tail_text(self.err_path)}


def start_api_server(
    ctx: dict, name: str, env_overrides: dict[str, str], attempts: int = 2
) -> tuple[ApiServer | None, dict]:
    """Start the real API server, retrying once on a bind race."""
    scratch = ctx["scratch_root"] / name
    scratch.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    for attempt in range(1, attempts + 1):
        server = ApiServer(ctx, name, scratch, env_overrides)
        meta = {"port": server.port, "attempt": attempt}
        meta.update(server.wait_ready())
        if meta.get("ready"):
            return server, meta
        server.stop()
        meta["stderr_excerpt_after_stop"] = _tail_text(server.err_path)
    return None, meta


def sse_prompt(
    server: ApiServer, api_key: str, session_id: str, text: str, timeout: float = 180.0
) -> dict:
    """POST one prompt and consume the real SSE stream until ``[DONE]``/deadline."""
    start = time.monotonic()
    out: dict = {
        "status": 0,
        "media_type": "",
        "done_seen": False,
        "frame_type_counts": {},
        "frames_read": 0,
        "text_concat": "",
        "done_content": "",
        "raw_excerpt": "",
        "error": "",
        "wall_seconds": 0.0,
    }
    connection = http.client.HTTPConnection(server.host, server.port, timeout=min(120.0, timeout))
    body = json.dumps({"text": text, "protocolVersion": 1})
    try:
        connection.request(
            "POST",
            SSE_PROMPT_PATH.format(session_id=session_id),
            body=body,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
        )
        response = connection.getresponse()
        out["status"] = response.status
        out["media_type"] = response.getheader("Content-Type", "")
        if response.status != 200:
            out["raw_excerpt"] = _bounded_tail(
                response.read(4096).decode("utf-8", errors="replace"), 300
            )
            return out
        deadline = start + timeout
        while time.monotonic() < deadline:
            try:
                raw_line = response.readline()
            except (TimeoutError, OSError) as error:
                out["error"] = f"stream read failed: {type(error).__name__}"
                return out
            if not raw_line:
                out["error"] = "stream closed before [DONE]"
                return out
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == SSE_DONE_SENTINEL:
                out["done_seen"] = True
                return out
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            out["frames_read"] += 1
            frame_type = str(frame.get("type", "?"))
            counts = out["frame_type_counts"]
            counts[frame_type] = counts.get(frame_type, 0) + 1
            if frame_type == "text":
                out["text_concat"] += str(frame.get("content", ""))
                out["text_concat"] = _bounded_tail(out["text_concat"], 8000)
            elif frame_type == "done":
                out["done_content"] = str(frame.get("content", ""))
        out["error"] = "deadline exceeded before [DONE]"
        return out
    except (http.client.HTTPException, OSError) as error:
        out["error"] = f"{type(error).__name__}: {error}"
        return out
    finally:
        try:
            connection.close()
        except Exception:
            pass
        out["wall_seconds"] = round(time.monotonic() - start, 3)


def sse_observed(result: dict) -> dict:
    """Bounded, secret-free view of one SSE result for the proof."""
    return {
        "status": result.get("status"),
        "media_type": result.get("media_type"),
        "done_seen": result.get("done_seen"),
        "frames_read": result.get("frames_read"),
        "frame_type_counts": result.get("frame_type_counts"),
        "wall_seconds": result.get("wall_seconds"),
        "error": result.get("error", ""),
        "text_excerpt": _bounded_tail(result.get("text_concat", ""), 300),
        "done_content_excerpt": _bounded_tail(result.get("done_content", ""), 300),
    }


class GatewayStdio:
    """Drive a real ``python -m machina.gateway`` over its stdio JSON-RPC line.

    The ephemeral ``MACHINA_GATEWAY_TOKEN`` lives only in the child env and in
    ``self.token`` — never in a recorded command or observation. stdout is the
    protocol channel pumped through a queue so every wait is deadline-bounded;
    stderr goes to a scratch file.
    """

    def __init__(
        self, ctx: dict, name: str, scratch: Path, extra_env: dict[str, str] | None = None
    ) -> None:
        self.token = secrets.token_hex(16)
        self.events: list[dict] = []
        self._event_cursor = 0
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        self._venv_python = str(ctx["venv_python"])
        self.err_path = scratch / f"{name}.gateway.err"
        env = child_env()
        env["MACHINA_GATEWAY_TOKEN"] = self.token
        if extra_env:
            env.update(extra_env)
        stderr = open(self.err_path, "wb")
        self.proc = subprocess.Popen(
            [self._venv_python, "-m", "machina.gateway"],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        stderr.close()
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()

    def command_display(self) -> str:
        return f"MACHINA_GATEWAY_TOKEN=<redacted> {self._venv_python} -m machina.gateway"

    def _pump_stdout(self) -> None:
        try:
            stream = self.proc.stdout
            if stream is None:
                return
            for line in stream:
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _read_frame(self, deadline: float) -> dict | None:
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                raise EOFError("gateway stdout closed")
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                return frame
        return None

    def call(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        self._next_id += 1
        request_id = self._next_id
        full_params = dict(params)
        full_params["token"] = self.token
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": full_params}
        try:
            if self.proc.stdin is None:
                raise BrokenPipeError("gateway stdin closed")
            self.proc.stdin.write(json.dumps(frame) + "\n")
            self.proc.stdin.flush()
        except Exception as error:  # reported by the caller as a failed check
            return {"error": {"message": f"gateway write failed: {type(error).__name__}"}}
        deadline = time.monotonic() + timeout
        while True:
            try:
                incoming = self._read_frame(deadline)
            except EOFError:
                return {"error": {"message": f"gateway exited before answering {method}"}}
            if incoming is None:
                return {"error": {"message": f"no response to {method} before timeout"}}
            if incoming.get("method") == "event":
                self.events.append(incoming.get("params") or {})
                continue
            if incoming.get("id") == request_id:
                return incoming

    def wait_event(
        self, event_type: str, turn_id: str | None = None, timeout: float = 180.0
    ) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            while self._event_cursor < len(self.events):
                event = self.events[self._event_cursor]
                self._event_cursor += 1
                if event.get("type") == event_type and (
                    turn_id is None or event.get("turnId") == turn_id
                ):
                    return event
            if time.monotonic() >= deadline:
                return None
            try:
                incoming = self._read_frame(deadline)
            except EOFError:
                return None
            if incoming is None:
                return None
            if incoming.get("method") == "event":
                self.events.append(incoming.get("params") or {})

    def event_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            event_type = str(event.get("type", "?"))
            counts[event_type] = counts.get(event_type, 0) + 1
        return counts

    def stop(self, timeout: float = 10.0) -> dict:
        try:
            self.call("shutdown", {}, timeout=5.0)
        except Exception:
            pass
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(self.proc, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _kill_group(self.proc, signal.SIGKILL)
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        result = {"exit_code": self.proc.returncode, "stderr_excerpt": _tail_text(self.err_path)}
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except Exception:
            pass
        return result


def wait_db(
    ctx: dict,
    queries: list[dict],
    predicate: Callable[[dict], bool],
    deadline_s: float = 90.0,
    interval: float = 2.0,
) -> tuple[dict, float, bool]:
    """Poll real DB queries until ``predicate(results)`` is true or the deadline."""
    start = time.monotonic()
    last: dict = {}
    while True:
        try:
            last = db_query(ctx, queries)
            if predicate(last):
                return last, round(time.monotonic() - start, 3), True
        except Exception as error:  # reported by the caller's check
            last = {"error": f"{type(error).__name__}: {error}"}
        if time.monotonic() - start >= deadline_s:
            return last, round(time.monotonic() - start, 3), False
        time.sleep(interval)


def j6_state(
    ctx: dict, session_id: str, document_id: str | None = None, timeout: float = 60.0
) -> tuple[dict, dict]:
    params: dict = {"session_id": session_id}
    if document_id:
        params["document_id"] = document_id
    return call_script(ctx, _J6_STATE_SCRIPT, params, timeout=timeout)


def wait_j6_state(
    ctx: dict,
    session_id: str,
    document_id: str | None,
    predicate: Callable[[dict], bool],
    deadline_s: float = 90.0,
    interval: float = 2.0,
) -> tuple[dict, float, bool]:
    start = time.monotonic()
    state: dict = {"ok": False}
    while True:
        try:
            _, state = j6_state(ctx, session_id, document_id)
        except Exception as error:  # reported by the caller's check
            state = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        if state.get("ok") and predicate(state):
            return state, round(time.monotonic() - start, 3), True
        if time.monotonic() - start >= deadline_s:
            return state, round(time.monotonic() - start, 3), False
        time.sleep(interval)


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
    except Exception as error:  # recorded as a failed check below
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
    except Exception as error:
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


def run_j6(ctx: dict) -> dict:
    record = new_record("j6")
    scratch = ctx["scratch_root"] / "j6"
    scratch.mkdir(parents=True, exist_ok=True)
    if not uvicorn_available(ctx):
        return finish(
            record,
            "SKIP",
            "HTTP stack unavailable (uvicorn missing in the venv); W6 cannot be verified",
        )
    marker = f"W6SEED{random.randint(1000, 9999)}"
    record["observed"]["marker"] = marker
    api_key = secrets.token_hex(16)
    env_overrides = {
        "MACHINA_API_KEY": api_key,
        "MACHINA_COMPRESSION_THRESHOLD": E2E_COMPRESSION_THRESHOLD,
        "MACHINA_AUTO_COMPACTION_ENABLED": "true",
        "MACHINA_COMPRESSION_ENABLED": "true",
    }

    # Fixture: a real session plus seeded chunks through the production context
    # API (6 x assistant_message > compression_preserve_recent, 1 document).
    raw, fixture = call_script(
        ctx,
        _SEED_COMPACTION_SCRIPT,
        {
            "title": "e2e-gate j6",
            "goal": "e2e gate: compaction triggers via SSE, gateway and explicit compress",
            "agent_id": "harness",
            "marker": marker,
            "seed_count": 6,
            "document_source": f"e2e://j6/{marker}",
        },
        timeout=120,
    )
    if not fixture.get("ok"):
        return finish(
            record,
            "FAIL",
            f"seed fixture failed: exit={raw['exit_code']} "
            f"{fixture.get('error', raw['stderr'][-200:])}",
        )
    session_id = fixture["session_id"]
    seeded_ids = [str(item) for item in (fixture.get("seeded_ids") or [])]
    document_id = fixture.get("document_id")
    record["observed"]["session_id"] = session_id
    record["observed"]["seeded_chunk_count"] = len(seeded_ids)
    record["observed"]["document_id"] = document_id
    record["commands"].append(
        f'{ctx["venv_python"]} -c "<context_manager.add_chunks_batch: '
        f'{len(seeded_ids)} seeds + 1 document>"'
    )

    server, server_meta = start_api_server(ctx, "j6", env_overrides)
    record["observed"]["server"] = server_meta
    if server is None:
        return finish(record, "FAIL", f"api server did not become ready: {server_meta}")
    record["commands"].append(server.command_display())

    try:
        # Phase A: post-stream auto-compaction on the real SSE route.
        token_a = f"OK{random.randint(100, 999)}A"
        sse_a = sse_prompt(server, api_key, session_id, f"Reply with exactly: {token_a}")
        record["observed"]["sse_turn_a"] = sse_observed(sse_a)
        record["commands"].append(
            f"POST {server.base_url}{SSE_PROMPT_PATH.format(session_id=session_id)} "
            "(SSE, X-API-Key set)"
        )
        check(
            record,
            "sse_turn_a_stream",
            sse_a.get("status") == 200 and bool(sse_a.get("done_seen")),
            "HTTP 200 text/event-stream terminated by [DONE]",
            {
                "status": sse_a.get("status"),
                "media_type": sse_a.get("media_type"),
                "done_seen": sse_a.get("done_seen"),
                "error": sse_a.get("error", ""),
            },
        )
        check(
            record,
            "sse_turn_a_text",
            token_a.casefold()
            in (str(sse_a.get("text_concat", "")) + str(sse_a.get("done_content", ""))).casefold(),
            f"streamed text contains the requested reply '{token_a}'",
            _bounded_tail(
                str(sse_a.get("text_concat", "")) + str(sse_a.get("done_content", "")), 200
            ),
        )

        state_a, waited_a, ok_a = wait_j6_state(
            ctx,
            session_id,
            document_id,
            lambda state: (
                int(state.get("summary_count") or 0) >= 1
                and int(state.get("archive_count") or 0) >= 1
                and isinstance(state.get("last_auto_compact_tokens"), int)
                and int(state.get("last_auto_compact_tokens")) > 0
                and state.get("claim_owner") is None
            ),
            deadline_s=120.0,
        )
        record["observed"]["phase_a_compaction"] = state_a
        record["observed"]["phase_a_waited_seconds"] = waited_a
        check(
            record,
            "auto_compaction_after_sse",
            ok_a,
            "SSE post-stream fired maybe_auto_compact: summary + archive + state + claim released",
            state_a,
        )
        archived = {str(item) for item in (state_a.get("archived_chunk_ids") or [])}
        check(
            record,
            "archived_ids_are_seeded",
            bool(archived & set(seeded_ids)) and document_id not in archived,
            "archived compressed rows include seeded conversational chunks and never the document",
            {
                "archived": sorted(archived)[:10],
                "seeded_count": len(seeded_ids),
                "seeded_intersection": len(archived & set(seeded_ids)),
            },
        )
        check(
            record,
            "document_survives_compaction",
            state_a.get("document_present") is True and not state_a.get("document_archived"),
            "document chunk stays active and is never archived by compaction",
            {
                "document_present": state_a.get("document_present"),
                "document_archived": state_a.get("document_archived"),
            },
        )

        # Phase B: the no-progress guard must skip a nearly-idle second turn.
        token_b = f"OK{random.randint(100, 999)}B"
        sse_b = sse_prompt(server, api_key, session_id, f"Reply with exactly: {token_b}")
        record["observed"]["sse_turn_b"] = sse_observed(sse_b)
        check(
            record,
            "sse_turn_b_stream",
            sse_b.get("status") == 200 and bool(sse_b.get("done_seen")),
            "second SSE turn completes normally",
            {
                "status": sse_b.get("status"),
                "done_seen": sse_b.get("done_seen"),
                "error": sse_b.get("error", ""),
            },
        )
        _, transcript_b = call_script(
            ctx,
            _J7_TRANSCRIPT_SCRIPT,
            {"session_id": session_id, "tokens": [token_b]},
            timeout=60,
        )
        check(
            record,
            "sse_turn_b_persisted",
            bool((transcript_b.get("user_tokens") or {}).get(token_b)),
            "second user message is durably stored before the no-progress window",
            {"user_tokens": transcript_b.get("user_tokens")},
        )

        # Snapshots require the claim to be free: a still-running compaction
        # holds the mutation claim, so the stability window cannot race it.
        def settled(state: dict) -> bool:
            return bool(state.get("ok") and state.get("claim_owner") is None)

        state_b1, _, ok_b1 = wait_j6_state(ctx, session_id, document_id, settled, deadline_s=60.0)
        time.sleep(20.0)
        state_b2, waited_b2, ok_b2 = wait_j6_state(
            ctx, session_id, document_id, settled, deadline_s=60.0
        )
        window_before = {
            key: state_b1.get(key)
            for key in (
                "summary_count",
                "latest_summary_id",
                "archive_count",
                "last_auto_compact_tokens",
            )
        }
        window_after = {
            key: state_b2.get(key)
            for key in (
                "summary_count",
                "latest_summary_id",
                "archive_count",
                "last_auto_compact_tokens",
            )
        }
        record["observed"]["phase_b_stable_window"] = {
            "before": window_before,
            "after": window_after,
            "waited_seconds": round(20.0 + waited_b2, 3),
        }
        check(
            record,
            "no_second_compaction_on_idle_turn",
            bool(ok_b1 and ok_b2 and window_before == window_after),
            "no-progress guard: summary/archive/state unchanged across a 20s window after turn B",
            record["observed"]["phase_b_stable_window"],
        )

        # Phase C: the explicit path (shared core) still archives-before-replace.
        compress = run_cmd(
            [str(ctx["machina_bin"]), "compress", session_id],
            cwd=scratch,
            env=child_env(),
            timeout=ctx["timeout"],
        )
        record["commands"].append(f"cd {scratch} && machina compress {session_id}")
        record["observed"]["cli_compress_excerpt"] = _bounded_tail(compress["stdout"], 400)
        check(
            record,
            "cli_compress_exit_zero",
            compress["exit_code"] == 0 and not compress["timed_out"],
            "explicit machina compress exits 0",
            {"exit_code": compress["exit_code"], "timed_out": compress["timed_out"]},
        )
        check(
            record,
            "cli_compress_reports_result",
            "Context compressed:" in compress["stdout"],
            "CLI reports a 'Context compressed: N chunks' result line",
            _bounded_tail(compress["stdout"], 400),
        )
        state_c, waited_c, ok_c = wait_j6_state(
            ctx,
            session_id,
            document_id,
            lambda state: (
                int(state.get("archive_count") or 0) > int(state_b2.get("archive_count") or 0)
                and state.get("latest_summary_id") is not None
                and state.get("latest_summary_id") != state_b2.get("latest_summary_id")
                and state.get("claim_owner") is None
            ),
            deadline_s=120.0,
        )
        record["observed"]["phase_c_explicit_compress"] = state_c
        record["observed"]["phase_c_waited_seconds"] = waited_c
        check(
            record,
            "explicit_compress_shares_core",
            ok_c,
            "explicit path archives-before-replace and writes a new summary chunk",
            state_c,
        )
    finally:
        record["observed"]["server_stop"] = server.stop()

    # Phase D: the gateway transport's post-turn trigger (message.complete is
    # emitted before the background maybe_auto_compact task runs).
    raw, fixture_d = call_script(
        ctx,
        _SEED_COMPACTION_SCRIPT,
        {
            "title": "e2e-gate j6 gateway",
            "goal": "e2e gate: gateway post-turn compaction trigger",
            "agent_id": "harness",
            "marker": f"{marker}G",
            "seed_count": 6,
            "document_source": f"e2e://j6/{marker}G",
        },
        timeout=120,
    )
    if not fixture_d.get("ok"):
        check(record, "gateway_seed_fixture", False, "second seeded session", fixture_d)
    else:
        session_d = fixture_d["session_id"]
        record["observed"]["gateway_session_id"] = session_d
        record["commands"].append(
            f'{ctx["venv_python"]} -c "<context_manager.add_chunks_batch: gateway session>"'
        )
        gateway = GatewayStdio(
            ctx,
            "j6",
            scratch,
            extra_env={
                "MACHINA_AUTO_COMPACTION_ENABLED": "true",
                "MACHINA_COMPRESSION_ENABLED": "true",
                "MACHINA_COMPRESSION_THRESHOLD": E2E_COMPRESSION_THRESHOLD,
                "MACHINA_GATEWAY_NO_SCHEDULER": "1",
            },
        )
        record["commands"].append(gateway.command_display())
        try:
            init = gateway.call("initialize", {"protocolVersion": 1}, timeout=60)
            check(
                record,
                "gateway_initialize",
                "result" in init,
                "gateway initialize returns a result",
                {"error": init.get("error")},
            )
            token_d = f"OK{random.randint(100, 999)}D"
            submit = gateway.call(
                "prompt.submit",
                {"sessionId": session_d, "text": f"Reply with exactly: {token_d}"},
                timeout=60,
            )
            turn_id = str((submit.get("result") or {}).get("turnId") or "")
            check(
                record,
                "gateway_turn_accepted",
                bool(turn_id) and "error" not in submit,
                "gateway accepts the prompt and returns a turnId",
                {"result": submit.get("result"), "error": submit.get("error")},
            )
            complete = (
                gateway.wait_event("message.complete", turn_id=turn_id, timeout=180)
                if turn_id
                else None
            )
            check(
                record,
                "gateway_message_complete",
                complete is not None,
                "gateway emits message.complete",
                bool(complete),
            )
            gateway_text = str((complete or {}).get("text", ""))
            check(
                record,
                "gateway_reply_fixture",
                token_d.casefold() in gateway_text.casefold(),
                f"message.complete.text contains '{token_d}'",
                _bounded_tail(gateway_text, 200),
            )
            state_d, waited_d, ok_d = wait_j6_state(
                ctx,
                session_d,
                fixture_d.get("document_id"),
                lambda state: (
                    int(state.get("summary_count") or 0) >= 1
                    and int(state.get("archive_count") or 0) >= 1
                    and state.get("claim_owner") is None
                ),
                deadline_s=120.0,
            )
            record["observed"]["phase_d_gateway_compaction"] = state_d
            record["observed"]["phase_d_waited_seconds"] = waited_d
            check(
                record,
                "auto_compaction_after_gateway_turn",
                ok_d,
                "gateway post-turn fired maybe_auto_compact: summary + archive + claim released",
                state_d,
            )
        finally:
            record["observed"]["gateway_stop"] = gateway.stop()
            revoke_session(ctx, session_d, record)

    revoke_session(ctx, session_id, record)
    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def run_j7(ctx: dict) -> dict:
    record = new_record("j7")
    scratch = ctx["scratch_root"] / "j7"
    scratch.mkdir(parents=True, exist_ok=True)
    if not uvicorn_available(ctx):
        return finish(
            record,
            "SKIP",
            "HTTP stack unavailable (uvicorn missing in the venv); W2 cannot be verified",
        )
    api_key = secrets.token_hex(16)
    server, server_meta = start_api_server(ctx, "j7", {"MACHINA_API_KEY": api_key})
    record["observed"]["server"] = server_meta
    if server is None:
        return finish(record, "FAIL", f"api server did not become ready: {server_meta}")
    record["commands"].append(server.command_display())

    gateway: GatewayStdio | None = None
    session_id = ""
    try:
        _, cfg = call_script(ctx, _CONFIG_PROBE_SCRIPT, timeout=60)
        provider = str(cfg.get("provider") or "")
        model = str(cfg.get("model") or "")
        record["observed"]["configured_route"] = {"provider": provider, "model": model}

        bad_status, bad_body = http_json(
            server.base_url,
            "POST",
            "/api/v1/sessions",
            api_key=api_key,
            body={"title": "e2e-gate j7 invalid", "provider": "not-a-provider"},
        )
        record["commands"].append(
            f"POST {server.base_url}/api/v1/sessions (provider=not-a-provider, X-API-Key set)"
        )
        check(
            record,
            "invalid_provider_rejected",
            bad_status == 400 and "unknown provider" in json.dumps(bad_body).lower(),
            "unknown provider is rejected with 400 (PROVIDERS facade reachable from the API)",
            {"status": bad_status, "body_excerpt": _bounded_tail(json.dumps(bad_body), 200)},
        )

        created_status, created_body = http_json(
            server.base_url,
            "POST",
            "/api/v1/sessions",
            api_key=api_key,
            body={"title": "e2e-gate j7", "provider": provider or None},
        )
        session_id = str((created_body.get("session") or {}).get("id") or "")
        record["observed"]["session_id"] = session_id
        record["commands"].append(
            f"POST {server.base_url}/api/v1/sessions (X-API-Key set, "
            f"provider={provider or 'default'})"
        )
        check(
            record,
            "session_created_via_http",
            created_status == 200 and bool(session_id),
            "real HTTP session create returns a session id",
            {"status": created_status, "session_id": session_id},
        )
        check(
            record,
            "session_provider_roundtrip",
            (created_body.get("session") or {}).get("provider") == provider,
            "created session stores the configured provider",
            (created_body.get("session") or {}).get("provider"),
        )

        if session_id:
            token_a = f"PARITY{random.randint(1000, 9999)}A"
            token_b = token_a[:-1] + "B"
            token_c = token_a[:-1] + "C"
            record["observed"]["tokens"] = {"a": token_a, "b": token_b, "c": token_c}

            # Phase A: SSE turn 1 (the SSE route's agent loop).
            sse_a = sse_prompt(server, api_key, session_id, f"Reply with exactly: {token_a}")
            record["observed"]["sse_turn_a"] = sse_observed(sse_a)
            record["commands"].append(
                f"POST {server.base_url}{SSE_PROMPT_PATH.format(session_id=session_id)} "
                "(SSE, X-API-Key set)"
            )
            check(
                record,
                "sse_turn_a_stream",
                sse_a.get("status") == 200 and bool(sse_a.get("done_seen")),
                "SSE turn completes with [DONE]",
                {
                    "status": sse_a.get("status"),
                    "done_seen": sse_a.get("done_seen"),
                    "error": sse_a.get("error", ""),
                },
            )
            check(
                record,
                "sse_turn_a_text",
                token_a.casefold()
                in (
                    str(sse_a.get("text_concat", "")) + str(sse_a.get("done_content", ""))
                ).casefold(),
                f"SSE streamed text contains '{token_a}'",
                _bounded_tail(
                    str(sse_a.get("text_concat", "")) + str(sse_a.get("done_content", "")), 200
                ),
            )

            # Phase B: stdio gateway turn on the SSE-created session (cross-
            # process claim release + the gateway loop's event contract).
            gateway = GatewayStdio(
                ctx, "j7", scratch, extra_env={"MACHINA_GATEWAY_NO_SCHEDULER": "1"}
            )
            record["commands"].append(gateway.command_display())
            init = gateway.call("initialize", {"protocolVersion": 1}, timeout=60)
            check(
                record,
                "gateway_initialize",
                "result" in init,
                "gateway initialize returns a result",
                {"error": init.get("error")},
            )
            submit = gateway.call(
                "prompt.submit",
                {"sessionId": session_id, "text": f"Reply with exactly: {token_b}"},
                timeout=60,
            )
            turn_id = str((submit.get("result") or {}).get("turnId") or "")
            record["observed"]["gateway_turn_id"] = turn_id
            check(
                record,
                "gateway_turn_accepted_after_sse",
                bool(turn_id) and "error" not in submit,
                "gateway prompt.submit accepted on the SSE-created session "
                "(cross-process claim release)",
                {"result": submit.get("result"), "error": submit.get("error")},
            )
            complete = (
                gateway.wait_event("message.complete", turn_id=turn_id, timeout=180)
                if turn_id
                else None
            )
            counts = gateway.event_type_counts()
            record["observed"]["gateway_event_type_counts"] = counts
            gateway_text = str((complete or {}).get("text", ""))
            check(
                record,
                "gateway_message_complete",
                complete is not None and token_b.casefold() in gateway_text.casefold(),
                f"gateway emits message.complete carrying '{token_b}'",
                {"text_excerpt": _bounded_tail(gateway_text, 200), "counts": counts},
            )
            check(
                record,
                "gateway_delta_stream",
                counts.get("message.delta", 0) >= 1,
                "gateway streamed >=1 message.delta",
                counts,
            )
            check(
                record,
                "gateway_no_error_event",
                counts.get("error", 0) == 0,
                "no error event frames on the gateway turn",
                counts,
            )

            # Claim released after the gateway turn (bounded poll).
            claim_results, claim_waited, claim_free = wait_db(
                ctx,
                [
                    {
                        "name": "claim",
                        "sql": "SELECT claim_owner, claim_kind FROM sessions WHERE id = $1::uuid",
                        "args": [session_id],
                    }
                ],
                lambda results: (
                    bool(results.get("claim")) and results["claim"][0].get("claim_owner") is None
                ),
                deadline_s=30.0,
            )
            record["observed"]["gateway_claim"] = {
                "results": claim_results,
                "waited_seconds": claim_waited,
            }
            check(
                record,
                "gateway_claim_released",
                claim_free,
                "sessions.claim_owner is NULL after message.complete",
                claim_results,
            )

            # Phase C: SSE re-acquisition after the gateway turn.
            sse_c = sse_prompt(server, api_key, session_id, f"Reply with exactly: {token_c}")
            record["observed"]["sse_turn_c"] = sse_observed(sse_c)
            check(
                record,
                "sse_turn_c_after_gateway",
                sse_c.get("status") == 200
                and bool(sse_c.get("done_seen"))
                and token_c.casefold()
                in (
                    str(sse_c.get("text_concat", "")) + str(sse_c.get("done_content", ""))
                ).casefold(),
                "SSE accepts a fresh turn on the gateway-touched session (claim reacquired)",
                {
                    "status": sse_c.get("status"),
                    "done_seen": sse_c.get("done_seen"),
                    "error": sse_c.get("error", ""),
                },
            )

            # Transcript parity: all three turns are durably persisted.
            _, transcript = call_script(
                ctx,
                _J7_TRANSCRIPT_SCRIPT,
                {"session_id": session_id, "tokens": [token_a, token_b, token_c]},
                timeout=60,
            )
            record["observed"]["transcript"] = transcript
            user_tokens = transcript.get("user_tokens") or {}
            check(
                record,
                "transcript_user_messages",
                int(transcript.get("user_message_count") or 0) >= 3
                and all(user_tokens.get(item) for item in (token_a, token_b, token_c)),
                "3 user_message rows persisted with each unique turn token",
                transcript,
            )
            check(
                record,
                "transcript_assistant_messages",
                int(transcript.get("assistant_message_count") or 0) >= 3,
                ">=3 assistant_message rows persisted across both transports",
                {"assistant_message_count": transcript.get("assistant_message_count")},
            )
    finally:
        if gateway is not None:
            record["observed"]["gateway_stop"] = gateway.stop()
        record["observed"]["server_stop"] = server.stop()
        if session_id:
            revoke_session(ctx, session_id, record)

    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def run_j8(ctx: dict) -> dict:
    record = new_record("j8")
    _, cfg = call_script(ctx, _CONFIG_PROBE_SCRIPT, timeout=60)
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    record["observed"]["configured_route"] = {"provider": provider, "model": model}
    if provider.lower() == "ollama" and not _port_open("127.0.0.1", 11434):
        return finish(
            record,
            "SKIP",
            "selected provider is ollama and the daemon is unreachable (optional journey)",
        )

    session_id, _, setup_payload = setup_session(
        ctx, "j8", "e2e gate: provider facade -> real call -> durable llm_usage row"
    )
    if not session_id:
        return finish(record, "FAIL", f"session setup failed: {setup_payload}")
    record["observed"]["session_id"] = session_id

    token = f"PROV{random.randint(1000, 9999)}"
    record["observed"]["fixture_token"] = token
    raw, payload = call_script(
        ctx,
        _J8_PROVIDER_SCRIPT,
        {"session_id": session_id, "agent_id": "harness", "token": token},
        timeout=ctx["timeout"],
    )
    record["commands"].append(
        f'{ctx["venv_python"]} -c "<get_provider + usage_store.complete_call>" session={session_id}'
    )
    record["observed"]["provider_call"] = {
        "exit_code": raw["exit_code"],
        "selected": payload.get("selected"),
        "model": payload.get("model"),
        "chat_provider": payload.get("chat_provider"),
        "provider_class": payload.get("provider_class"),
        "contains_token": payload.get("contains_token"),
        "total_tokens": payload.get("total_tokens"),
        "error": payload.get("error", ""),
    }
    check(
        record,
        "provider_call_ok",
        raw["exit_code"] == 0 and bool(payload.get("ok")),
        "real configured provider completes through usage_store.complete_call",
        {"ok": bool(payload.get("ok")), "error": payload.get("error", "")},
    )
    check(
        record,
        "provider_facade_consistent",
        bool(payload.get("facade_consistent")),
        "machina.core.provider.__all__ exports intact; PROVIDERS == PROVIDER_SPECS + specials",
        {
            "facade_missing": payload.get("facade_missing"),
            "consistency": payload.get("facade_consistent"),
        },
    )
    check(
        record,
        "provider_capability_ready",
        payload.get("chat_provider") == "ready"
        and payload.get("chat_provider_selected") == payload.get("selected"),
        "runtime _detect_providers reports the selected provider ready",
        {
            "chat_provider": payload.get("chat_provider"),
            "selected": payload.get("selected"),
            "expected_key_env": payload.get("expected_key_env"),
        },
    )
    check(
        record,
        "provider_resolved",
        bool(payload.get("provider_is_llm")) and payload.get("provider_model") == model,
        f"get_provider returns an LLMProvider bound to the configured model '{model}'",
        {
            "provider_is_llm": payload.get("provider_is_llm"),
            "provider_model": payload.get("provider_model"),
        },
    )
    check(
        record,
        "reply_fixture",
        bool(payload.get("contains_token")),
        f"completion content contains '{token}'",
        payload.get("content_excerpt", ""),
    )
    check(
        record,
        "usage_known",
        isinstance(payload.get("total_tokens"), int) and payload["total_tokens"] > 0,
        "provider returned positive total_tokens",
        payload.get("total_tokens"),
    )

    try:
        usage = db_query(
            ctx,
            [
                {
                    "name": "usage",
                    "sql": (
                        "SELECT status, provider, model, prompt_tokens, completion_tokens, "
                        "accounted_tokens FROM llm_usage WHERE session_id = $1::uuid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    "args": [session_id],
                }
            ],
        )
        usage_rows = usage.get("usage") or []
        row = usage_rows[0] if usage_rows else {}
        record["observed"]["llm_usage_row"] = row
        check(
            record,
            "llm_usage_row_complete",
            row.get("status") == "complete",
            "durable llm_usage row is complete",
            row,
        )
        check(
            record,
            "llm_usage_known_tokens",
            row.get("prompt_tokens") is not None
            and row.get("completion_tokens") is not None
            and int(row.get("accounted_tokens") or 0) > 0,
            "llm_usage row carries non-null prompt/completion tokens and accounted tokens",
            row,
        )
        served_model = str(payload.get("response_model") or "")
        check(
            record,
            "llm_usage_model",
            bool(served_model) and row.get("model") == served_model,
            "llm_usage.model records the response-reported (served) model",
            {"row_model": row.get("model"), "response_model": served_model},
        )
    except Exception as error:  # recorded as a failed check below
        check(record, "llm_usage_query", False, "database query succeeds", str(error))

    status_run = run_cmd(
        [str(ctx["machina_bin"]), "status"],
        cwd=REPO_ROOT,
        env=child_env(),
        timeout=120,
    )
    record["commands"].append(f"cd {REPO_ROOT} && machina status")
    record["observed"]["cli_status_excerpt"] = _bounded_tail(status_run["stdout"], 400)
    check(
        record,
        "cli_status_exit_zero",
        status_run["exit_code"] == 0 and not status_run["timed_out"],
        "machina status exits 0",
        {"exit_code": status_run["exit_code"], "timed_out": status_run["timed_out"]},
    )
    check(
        record,
        "cli_status_chat_provider_line",
        "Chat provider" in status_run["stdout"],
        "status output includes the 'Chat provider' capability line",
        _bounded_tail(status_run["stdout"], 300),
    )

    revoke_session(ctx, session_id, record)
    failed = [item for item in record["checks"] if not item["ok"]]
    if failed:
        return finish(record, "FAIL", "failed checks: " + ", ".join(c["name"] for c in failed))
    return finish(record, "PASS")


def run_j9(ctx: dict) -> dict:
    """OpenCode -> machina MCP shim -> Machina session, end to end.

    Skipped unless the `opencode` CLI exists and lists a registered `machina`
    MCP server for this project (registration lives in opencode.json, added by
    the interop proof step: `opencode mcp add machina -- <venv-python> -m
    machina.mcp_server`). Writes are asserted in a real Machina session row,
    never against OpenCode's own storage.
    """
    record = new_record("j9")
    opencode_bin = shutil.which("opencode")
    record["observed"]["opencode_bin"] = opencode_bin or ""
    if not opencode_bin:
        return finish(record, "SKIP", "opencode CLI not installed (optional journey)")

    listed = run_cmd(
        [opencode_bin, "mcp", "list"], cwd=ctx["repo_root"], env=child_env(), timeout=120
    )
    record["commands"].append("opencode mcp list")
    record["observed"]["mcp_list_exit_code"] = listed["exit_code"]
    mcp_list_text = (listed["stdout"] + listed["stderr"]).lower()
    record["observed"]["mcp_list_has_machina"] = "machina" in mcp_list_text
    if "machina" not in mcp_list_text:
        return finish(
            record,
            "SKIP",
            "machina MCP server not registered for this project; run: "
            f"opencode mcp add machina -- {ctx['venv_python']} -m machina.mcp_server",
        )

    token = f"MCPJ9-{random.randint(100000, 999999)}"
    record["observed"]["token"] = token
    message = (
        "Use the machina_chat tool exactly once. Pass it this prompt: "
        f"Reply with exactly {token} and nothing else. "
        "Then return the machina_chat tool output."
    )
    run = run_cmd(
        [opencode_bin, "run", "--standalone", "--auto", "--format", "json", message],
        cwd=ctx["repo_root"],
        env=child_env(),
        timeout=ctx["timeout"],
    )
    record["commands"].append(
        "opencode run --standalone --auto --format json "
        f'"Use the machina_chat tool exactly once. Pass it this prompt: '
        f'Reply with exactly {token} and nothing else. Then return the machina_chat tool output."'
    )
    record["observed"]["opencode_exit_code"] = run["exit_code"]
    record["observed"]["opencode_timed_out"] = run["timed_out"]
    record["observed"]["opencode_wall_seconds"] = run["wall_seconds"]
    record["observed"]["opencode_output_excerpt"] = _bounded_tail(run["stdout"], 500)
    check(
        record,
        "opencode_run_exit_zero",
        run["exit_code"] == 0 and not run["timed_out"],
        "opencode run exits 0 without timeout",
        {"exit_code": run["exit_code"], "timed_out": run["timed_out"]},
    )
    # The JSON transcript must name the namespaced machina chat tool (any
    # delimiter convention: machina.machina_chat / machina_machina_chat).
    tool_markers = ("machina.machina_chat", "machina_machina_chat", "machina:machina_chat")
    tool_present = any(marker in run["stdout"] for marker in tool_markers)
    record["observed"]["machina_chat_marker_present"] = tool_present
    check(
        record,
        "machina_tool_call_present",
        tool_present,
        "opencode JSON transcript names the machina_chat MCP tool",
        {"markers": tool_markers, "matched": tool_present},
    )
    check(
        record,
        "opencode_output_contains_token",
        token in run["stdout"],
        "opencode output carries the token returned through machina_chat",
        {"token": token, "matched": token in run["stdout"]},
    )

    # Machina-side proof: the token must exist in a real session row (chunk
    # payloads), not merely in OpenCode's transcript.
    raw, payload = call_script(ctx, _J9_ASSERT_SCRIPT, {"token": token}, timeout=120)
    matches = payload.get("matches") or []
    record["observed"]["db_assert_exit_code"] = raw["exit_code"]
    record["observed"]["db_scanned_chunks"] = payload.get("scanned")
    record["observed"]["db_sessions_with_token"] = matches[:3]
    check(
        record,
        "session_row_contains_token",
        bool(matches),
        ">=1 Machina context chunk (fresh session) contains the token",
        {"matches": matches[:3], "scanned": payload.get("scanned")},
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
    "j6": run_j6,
    "j7": run_j7,
    "j8": run_j8,
    "j9": run_j9,
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
        except Exception as error:  # a crashed journey is a FAIL, not a traceback dump
            record = new_record(jid)
            record["reason"] = f"unhandled {type(error).__name__}: {error}"
            finish(record, "FAIL", record["reason"])
        record["duration_seconds"] = round(time.monotonic() - start, 3)
        record.setdefault("notes", []).append(f"scratch: {scratch_root}")
        budget = JOURNEY_TIMEOUT_BUDGET_SECONDS.get(jid)
        if budget:
            record["notes"].append(f"budget: {int(budget)}s wall-clock (bounded steps sum)")
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
