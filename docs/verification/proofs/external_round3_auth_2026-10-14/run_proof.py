"""External round-3 auth re-verification (findings 1-3) on the fixed tree.

Runs the ORIGINAL review evidence scripts from
``docs/verification/proofs/codebase_review_round3_2026-10-14/`` UNMODIFIED
(this runner only reads their sources), then isolates their sections:

* ``auth-security.py`` aborts by design at the now-blocked credential read
  (the broker raises ``NeedsApproval`` for the read that used to leak the
  refresh token), so its refresh-race and resurrection sections are executed
  here from the same verbatim source, trailing runner lines stripped.
* The refresh-race fixture's two-party barrier can only be satisfied by the
  PRE-FIX behavior (both callers submitting the same rotating token). With
  serialization in place exactly one request is sent, so the barrier cannot
  complete; the section is re-run with the barrier replaced by an immediate
  response, everything else (production ``refresh``, real store, real lock,
  two real threads) unchanged.

No production credentials, no external network: synthetic tokens and
``httpx.MockTransport`` only, exactly like the original scripts.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[4]
ROUND3 = ROOT / "docs" / "verification" / "proofs" / "codebase_review_round3_2026-10-14"


def _isolated_env(scratch: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["HOME"] = str(scratch / "home")
    env["MACHINA_ENV_FILE"] = str(scratch / "empty.env")
    (scratch / "empty.env").write_text("")
    return env


def _run_original(script: str, scratch: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(ROUND3 / script)],
        cwd=ROOT,
        env=_isolated_env(scratch),
        capture_output=True,
        text=True,
        timeout=300,
    )
    return (result.stdout + result.stderr).strip()


def run_original_auth_security() -> None:
    print("=== A. ORIGINAL auth-security.py, unmodified ===")
    output = _run_original("auth-security.py", Path(tempfile.mkdtemp(prefix="r3ev-asec-")))
    for line in output.splitlines():
        if "AUDIT" in line or "Audit event" in line:
            continue
        print(line)


def load_isolated_sections() -> dict:
    """Exec the original script minus its trailing runner block."""
    source = (ROUND3 / "auth-security.py").read_text()
    drop = (
        "asyncio.run(workspace_read())",
        "concurrent_refresh()",
        "resurrection_after_logout()",
        "print('SCRATCH'",
    )
    body = "".join(line for line in source.splitlines(keepends=True) if not line.startswith(drop))
    namespace: dict = {"__name__": "round3_isolated", "__file__": str(ROUND3)}
    exec(compile(body, str(ROUND3 / "auth-security.py"), "exec"), namespace)
    return namespace


def run_isolated_sections(namespace: dict) -> None:
    auth = namespace["auth"]
    tokens = namespace["tokens"]
    scratch = namespace["scratch"]

    print()
    print("=== B1. workspace_read (verbatim section) ===")
    try:
        asyncio.run(namespace["workspace_read"]())
        print("WORKSPACE_READ_COMPLETED_UNEXPECTEDLY")
    except Exception as error:
        print("WORKSPACE_READ_BLOCKED", type(error).__name__)

    from machina.memory.redaction import redact_secrets

    store = scratch / "workspace" / "auth" / "chatgpt.json"
    raw = store.read_text()
    refresh = tokens()["refresh_token"]
    print("RAW_STORE_CONTAINS_REFRESH", refresh in raw)
    print("REDACTION_CONTAINS_REFRESH", refresh in redact_secrets(raw).text)

    print()
    print("=== B2. concurrent_refresh (verbatim section) ===")
    try:
        namespace["concurrent_refresh"]()
        print("ORIGINAL_FIXTURE_COMPLETED")
    except BaseException as error:  # noqa: BLE001 - evidence of the fixture barrier
        print("ORIGINAL_FIXTURE_RAISED", type(error).__name__)

    print()
    print("=== B2'. refresh race, fixture barrier neutralized ===")
    _race_variant(auth, tokens, scratch)

    print()
    print("=== B3. resurrection_after_logout (verbatim section) ===")
    namespace["resurrection_after_logout"]()


def _race_variant(auth, tokens, scratch) -> None:
    """Same production path as the original section; handler responds at once."""
    target = scratch / "race-variant-concurrent.json"
    auth.save_tokens(tokens(), target)
    lock = threading.Lock()
    requests: list[str] = []
    start = threading.Barrier(2)

    def handler(request):
        supplied = parse_qs(request.content.decode())["refresh_token"][0]
        with lock:
            requests.append(supplied)
        time.sleep(0.2)  # widens the window the serialized peer must wait out
        return __import__("httpx").Response(
            200,
            json={
                "access_token": tokens("fresh")["access_token"],
                "refresh_token": tokens("fresh")["refresh_token"],
            },
        )

    def call():
        import httpx

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            start.wait(timeout=10)
            try:
                return auth.refresh(tokens(), path=target, client=client)["refresh_token"]
            except auth.ChatGPTAuthError as error:
                return f"ERROR:{error.code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))
    print(
        "REFRESH_POST_COUNT",
        len(requests),
        "SAME_OLD_REFRESH",
        requests == [tokens()["refresh_token"]],
    )
    print("REFRESH_RESULTS", results)


def run_original_auth_lifecycle() -> None:
    print()
    print("=== C. ORIGINAL auth-lifecycle.py, unmodified ===")
    output = _run_original("auth-lifecycle.py", Path(tempfile.mkdtemp(prefix="r3ev-alife-")))
    print(output)


def main() -> int:
    run_original_auth_security()
    namespace = load_isolated_sections()
    run_isolated_sections(namespace)
    run_original_auth_lifecycle()
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:  # noqa: BLE001 - evidence runner: print, do not cliff
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)
