# AgentHarness Security Audit — Final Report

**Date:** 2026-10-01  
**Scope:** Full codebase security review  
**Methodology:** Manual code review, static analysis, threat modeling  

---

## Executive Summary

AgentHarness is a **local-first, single-user CLI tool** with no network-facing API surface. This fundamentally limits the attack surface — there are no HTTP endpoints, no authentication system, and no multi-tenant isolation to audit. However, the codebase contains **several high-severity vulnerabilities** that could be exploited by a malicious LLM prompt, a compromised dependency, or a local attacker. The most critical issues are:

1. **Command injection via overly permissive terminal tool** — `python`, `pip`, `npm`, `node` are allowlisted
2. **Path traversal in `search_files` tool** — no base directory restriction
3. **SSRF bypass via DNS rebinding** in `web_extract`
4. **Weak default database credentials** hardcoded in source
5. **Sensitive data leakage via audit logging**
6. **No rate limiting on tool execution** — only LLM calls are rate-limited
7. **Prompt injection via memory consolidation and re-ranking**

---

## 1. SQL Injection

### Status: ✅ PASS (with minor notes)

**Finding:** All database queries use asyncpg's parameterized query interface (`$1, $2, ...`). No string concatenation or f-string interpolation was found in SQL queries.

**Evidence:**
- `ah/core/context.py` — All queries use `$1, $2, ...` placeholders
- `ah/core/session.py` — All queries use parameterized queries
- `ah/memory/store.py` — Dynamic WHERE clauses built with `f"agent_id = ${param_idx}"` — safe because values are still parameterized
- `ah/memory/retriever.py` — ILIKE conditions use `f"content ILIKE ${param_idx}"` with `params.append(f"%{kw}%")` — safe
- `ah/rag/search.py` — All queries parameterized

**Minor Note:** The `MemoryStore.search()` method builds WHERE clauses dynamically:
```python
conditions.append(f"agent_id = ${param_idx}")
```
This is safe because the values are still passed as parameters, but the pattern is fragile — a future developer might accidentally introduce concatenation.

---

## 2. Command Injection

### Status: 🔴 FAIL — CRITICAL

### 2.1 Overly Permissive Command Allowlist

**Severity:** CRITICAL  
**Location:** `ah/tools/terminal.py:15-19`

```python
ALLOWED_COMMANDS = frozenset({
    "git", "ls", "cat", "grep", "find", "pytest", "python", "pip",
    "npm", "node", "echo", "pwd", "cd", "mkdir", "cp", "mv", "rm",
    "touch", "head", "tail", "wc", "diff", "curl", "wget",
})
```

**Issues:**
- **`python`** — Can execute arbitrary Python code: `python -c "import os; os.system('rm -rf /')"`
- **`pip`** — Can install malicious packages: `pip install malicious-package`
- **`npm`** / **`node`** — Can execute arbitrary JavaScript: `node -e "require('child_process').exec('...')"`
- **`curl`** / **`wget`** — Can be used for SSRF, data exfiltration
- **`rm`** — Can delete arbitrary files
- **`cp`** / **`mv`** — Can overwrite critical files
- **`mkdir`** / **`touch`** — Can create files in sensitive locations

**Impact:** A malicious LLM prompt (prompt injection) could instruct the agent to execute arbitrary system commands via the terminal tool.

**Recommendation:** Remove `python`, `pip`, `npm`, `node`, `curl`, `wget`, `rm`, `cp`, `mv` from the allowlist. Consider a much narrower allowlist or a sandboxed execution environment.

### 2.2 Dangerous Characters Check is Bypassable

**Severity:** HIGH  
**Location:** `ah/tools/terminal.py:22`

```python
DANGEROUS_CHARS = frozenset(";|&$()`<>\\\n")
```

**Issues:**
- The check is redundant because `shell=False` is used (no shell interpretation)
- However, it provides a false sense of security
- The check doesn't cover all injection vectors (e.g., newline injection, argument injection)
- Since `shell=False` is used, this is not directly exploitable, but the allowlist bypass (2.1) is the real issue

### 2.3 Workdir Validation is Weak

**Severity:** MEDIUM  
**Location:** `ah/tools/terminal.py:25-41`

```python
ALLOWED_WORKDIR_PREFIXES = (
    str(Path.home()),
    "/tmp",
    os.environ.get("TMPDIR", ""),
    os.environ.get("TEMP", ""),
)
```

**Issues:**
- Allows any path under `$HOME` — including `.ssh`, `.aws`, `.config`
- Allows `/tmp` — world-writable directory
- No check for symlinks that could escape the allowed prefix

---

## 3. Path Traversal

### Status: 🔴 FAIL — HIGH

### 3.1 `search_files` Tool Has No Path Restriction

**Severity:** HIGH  
**Location:** `ah/tools/builtins.py:157-182`

```python
@registry.register(description="Search file contents with regex")
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    dir_path = Path(path)  # ← No base directory check!
    if not dir_path.exists():
        return f"Error: Path not found: {path}"
    # ...
    for f in dir_path.glob(glob_pattern):  # ← Can traverse anywhere
```

**Issues:**
- No `_resolve_path()` call — unlike `read_file`, `write_file`, `list_files`
- An LLM could search `/etc/shadow`, `~/.ssh/`, `/proc/self/environ`
- The `path` parameter accepts absolute paths and `..` traversal
- The `file_glob` parameter could be used to match sensitive files

**Impact:** Information disclosure — sensitive file contents could be read and fed back to the LLM.

**Recommendation:** Apply the same `_resolve_path()` pattern used in `ah/tools/file.py`.

### 3.2 File Tools Path Traversal Protection is Adequate

**Status:** ✅ PASS  
**Location:** `ah/tools/file.py:17-32`

The `_resolve_path()` function correctly:
1. Resolves the candidate path with `.resolve()` (handles `..`, symlinks)
2. Checks `is_relative_to(_BASE_DIR)` to prevent escape
3. Raises `ValueError` if the path escapes

**Minor Note:** The `_BASE_DIR` defaults to `os.getcwd()` which could be anywhere. Consider restricting to a project-specific directory.

### 3.3 RAG FileLoader Path Traversal Protection is Adequate

**Status:** ✅ PASS  
**Location:** `ah/rag/loaders.py:164-171`

Same `_resolve_path()` pattern as file tools.

---

## 4. SSRF (Server-Side Request Forgery)

### Status: 🔴 FAIL — HIGH

### 4.1 DNS Rebinding Vulnerability in `web_extract`

**Severity:** HIGH  
**Location:** `ah/tools/builtins.py:20-58`

```python
def _is_safe_url(url: str) -> bool:
    # ...
    try:
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False
    except (socket.gaierror, ValueError):
        return False
    return True
```

**Issues:**
- **DNS Rebinding:** The function resolves the hostname once and checks the IP. An attacker can control DNS to return a public IP for the check, then a private IP for the actual request.
- **TOCTOU:** Time-of-check to time-of-use — the DNS resolution happens at check time, but the actual HTTP request happens later and may resolve differently.
- **IPv6 not handled:** `ipaddress.ip_address()` handles IPv6, but `socket.gethostbyname()` only returns IPv4. A hostname could resolve to an IPv6 private address.
- **No redirect following:** `follow_redirects=False` is good, but the initial request could still hit a private IP if DNS rebinding is used.

**Impact:** An attacker (or malicious LLM prompt) could make the agent fetch internal services (e.g., `http://169.254.169.254/` for cloud metadata, `http://localhost:5432/` for database).

**Recommendation:**
1. Resolve the hostname and pin the IP for the actual request
2. Use a custom HTTP adapter that validates the resolved IP
3. Consider using a whitelist of allowed domains instead of a blacklist

### 4.2 `web_search` Uses Local SearXNG Instance

**Severity:** LOW  
**Location:** `ah/tools/builtins.py:68`

```python
searxng_url = os.environ.get("SEARXNG_URL", "http://localhost:8080")
```

**Note:** This is a local service, not SSRF. However, if `SEARXNG_URL` is set to an external URL, it could be used for SSRF. The default is safe.

---

## 5. XSS (Cross-Site Scripting)

### Status: ✅ PASS (N/A)

**Finding:** AgentHarness is a CLI tool with no web UI. The Rich library escapes HTML by default. The `Markdown` class in Rich does not render raw HTML by default.

**Minor Note:** If agent responses contain HTML tags, Rich's Markdown renderer may render them. This is not a security issue in a CLI context but could be if the output is ever rendered in a web interface.

---

## 6. CSRF (Cross-Site Request Forgery)

### Status: ✅ PASS (N/A)

**Finding:** No web UI, no HTTP endpoints, no cookies. CSRF is not applicable.

---

## 7. Authentication & Authorization

### Status: 🔴 FAIL — MEDIUM

### 7.1 No Authentication System

**Severity:** MEDIUM  
**Finding:** The CLI is single-user with no authentication. This is acceptable for a local tool, but:
- The database connection uses a default password (`postgres:***`)
- No access control on who can run the CLI
- No audit trail of who executed what

### 7.2 Hardcoded Default Database Credentials

**Severity:** MEDIUM  
**Location:** `ah/db/connection.py:13`

```python
DEFAULT_DSN = "postgresql://postgres:***@localhost:5432/agentharness"
```

**Issues:**
- Default password is `postgres:***` — trivially guessable
- Hardcoded in source code
- If `DATABASE_URL` is not set, this default is used

**Recommendation:** Remove the default DSN. Require `DATABASE_URL` to be set explicitly.

### 7.3 `.env` File Contains Real Credentials

**Severity:** MEDIUM  
**Location:** `.env`

**Finding:** The `.env` file contains real API keys and database credentials. While it's in `.gitignore`, it:
- Is not encrypted
- Could be accidentally committed if `.gitignore` is modified
- Could be read by any process running as the same user

**Recommendation:** Use a secrets manager or at least restrict file permissions (`chmod 600 .env`).

---

## 8. Rate Limiting

### Status: 🔴 FAIL — MEDIUM

### 8.1 Rate Limiting Only Applied to LLM Calls

**Severity:** MEDIUM  
**Location:** `ah/core/provider.py:47-78`

**Finding:** The `AsyncTokenBucket` rate limiter is only applied to LLM API calls (`complete()` and `stream_complete()`). There is no rate limiting on:
- Tool execution (terminal, file, web)
- Database queries
- Memory operations
- RAG operations

**Impact:** A malicious LLM prompt could:
- Execute thousands of terminal commands in a loop
- Read/write thousands of files
- Make thousands of web requests
- Exhaust database connections

**Recommendation:** Add rate limiting to all tool executions, especially terminal and web tools.

### 8.2 Rate Limiter is Per-Provider Instance

**Severity:** LOW  
**Location:** `ah/core/provider.py:75-78`

**Finding:** The rate limiter is created per-provider instance. If multiple providers are created (e.g., in the REPL), each gets its own rate limiter, effectively bypassing the limit.

---

## 9. Input Validation

### Status: 🔴 FAIL — MEDIUM

### 9.1 Tool Argument Validation is Basic

**Severity:** MEDIUM  
**Location:** `ah/tools/base.py:111-160`

**Finding:** The `_validate_tool_args()` method validates:
- Required parameters are present
- No extra parameters are passed
- Parameter types match the schema

**Missing:**
- No range validation (e.g., `limit` could be 1,000,000)
- No pattern validation (e.g., `path` could contain null bytes)
- No length validation (e.g., `content` could be gigabytes)
- No sanitization of string inputs

**Impact:** A malicious LLM could pass extreme values that cause resource exhaustion.

### 9.2 No Input Size Limits

**Severity:** MEDIUM  
**Finding:** No limits on:
- `read_file` — could read a 10GB file
- `write_file` — could write a 10GB file
- `terminal` command — could be extremely long
- `web_extract` URL — could be extremely long
- `search_files` pattern — could be a catastrophic regex

**Recommendation:** Add size limits to all inputs.

### 9.3 No Prompt Injection Protection

**Severity:** HIGH  
**Finding:** User input is directly included in LLM prompts without any sanitization:
- `ah/core/assembler.py` — User query is directly inserted into the prompt
- `ah/core/agent.py` — User message is directly inserted
- `ah/memory/consolidator.py` — LLM output is parsed and stored without validation
- `ah/memory/retriever.py` — LLM re-ranking prompt includes user query directly

**Impact:** A malicious user could craft a prompt that overrides the system prompt, extracts sensitive data, or causes the agent to perform unauthorized actions.

**Recommendation:** Implement prompt injection defenses:
- Use delimiters around user input
- Validate LLM output before executing tool calls
- Consider using a separate LLM call to classify user intent

---

## 10. Secret Management

### Status: 🔴 FAIL — HIGH

### 10.1 Audit Logging of Sensitive Data

**Severity:** HIGH  
**Location:** `ah/core/provider.py:34-41`

```python
def audit_log(event_type: str, **kwargs) -> None:
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))
```

**Issues:**
- `tool_args` are logged in plain text — could contain passwords, API keys, file paths
- `result_preview` is logged — could contain sensitive data
- `query` is logged in RAG search — could contain sensitive information
- Logs are written to stdout — could be captured by any process

**Example from `ah/core/agent.py:383-388`:**
```python
audit_log(
    "tool_call_start",
    session_id=str(session_id),
    tool_name=tool_name,
    tool_args=tool_args,  # ← Sensitive data in plain text!
)
```

**Recommendation:** Sanitize audit logs — redact sensitive fields, use allowlists of safe fields to log.

### 10.2 API Keys in Environment Variables

**Status:** ✅ PASS  
**Finding:** API keys are read from environment variables, not hardcoded. This is a good practice.

### 10.3 Database Password in Source Code

**Severity:** MEDIUM  
**Location:** `ah/db/connection.py:13`

See section 7.2.

---

## 11. Dependency Vulnerabilities

### Status: ⚠️ WARNING

### 11.1 No Dependency Pinning

**Severity:** MEDIUM  
**Location:** `pyproject.toml:14-24`

```toml
dependencies = [
    "typer>=0.9.0",
    "rich>=13.0.0",
    "asyncpg>=0.29.0",
    "httpx>=0.27.0",
    "python-dotenv>=1.0.0",
    "msgpack>=1.0.0",
    "pyyaml>=6.0",
    "tiktoken>=0.5.0",
    "prompt-toolkit>=3.0.0",
]
```

**Issues:**
- Uses `>=` which allows any future version, including vulnerable ones
- No upper bounds
- No lock file (e.g., `poetry.lock`, `requirements.txt`)
- No automated security scanning (e.g., Dependabot, Snyk)

**Recommendation:** Pin exact versions or use a lock file. Add Dependabot or similar.

### 11.2 Known Vulnerabilities in Dependencies

**Finding:** As of the audit date, no known critical vulnerabilities were identified in the listed dependencies. However, without pinning, this could change at any time.

---

## 12. Additional Findings

### 12.1 No HTTPS Enforcement

**Severity:** LOW  
**Finding:** The `OpenRouterProvider` and `OllamaProvider` use HTTPS by default, but the `OllamaProvider` defaults to `http://localhost:11434`. This is acceptable for local development but should be documented.

### 12.2 No Resource Limits on Agent Loop

**Severity:** MEDIUM  
**Location:** `ah/core/agent.py:40`

```python
MAX_TOKEN_BUDGET = 50_000
```

**Finding:** The token budget is checked, but there's no limit on:
- Number of tool calls per run
- Total execution time
- Memory usage
- Number of concurrent sessions

**Impact:** A malicious LLM prompt could cause the agent to run for hours, consuming resources.

### 12.3 Memory Consolidator Executes Arbitrary LLM Output

**Severity:** HIGH  
**Location:** `ah/memory/consolidator.py:229-283`

**Finding:** The `_extract_memories()` method parses LLM output as JSON and creates `MemoryEntry` objects. If the LLM is tricked via prompt injection, it could:
- Create memories with arbitrary content
- Set `importance` to 1.0 (making them hard to forget)
- Set `explicitly_important` to True
- Inject malicious content into future prompts

**Recommendation:** Validate LLM output more strictly. Sanitize memory content before storing.

### 12.4 Memory Retriever LLM Re-ranking is Prompt Injection Vector

**Severity:** MEDIUM  
**Location:** `ah/memory/retriever.py:249-314`

**Finding:** The `_rerank()` method sends user query and memory content to an LLM for re-ranking. A malicious memory (injected via 12.3) could influence the re-ranking prompt.

### 12.5 No Sandboxing of Tool Execution

**Severity:** HIGH  
**Finding:** All tools execute with the same permissions as the user running the CLI. There's no sandboxing, no seccomp, no containerization.

**Impact:** A malicious LLM prompt could:
- Read `~/.ssh/id_rsa`
- Modify `.env` to steal API keys
- Install backdoors via `pip install`
- Exfiltrate data via `curl`

### 12.6 `search_files` Regex Denial of Service

**Severity:** MEDIUM  
**Location:** `ah/tools/builtins.py:157-182`

**Finding:** The `search_files` tool uses `re.search()` without any timeout or complexity limits. A malicious regex pattern could cause catastrophic backtracking.

**Example:** `search_files(pattern="(a+)+b", path="/")` — could hang for hours.

**Recommendation:** Use a regex timeout or limit the pattern complexity.

---

## Summary Table

| # | Category | Severity | Status | Issue |
|---|----------|----------|--------|-------|
| 1 | SQL Injection | — | ✅ PASS | All queries parameterized |
| 2 | Command Injection | CRITICAL | 🔴 FAIL | `python`, `pip`, `npm`, `node` allowlisted |
| 3 | Path Traversal | HIGH | 🔴 FAIL | `search_files` has no path restriction |
| 4 | SSRF | HIGH | 🔴 FAIL | DNS rebinding in `web_extract` |
| 5 | XSS | — | ✅ PASS | N/A (CLI tool) |
| 6 | CSRF | — | ✅ PASS | N/A (no web UI) |
| 7 | Auth Bypass | MEDIUM | 🔴 FAIL | Hardcoded DB credentials |
| 8 | Rate Limiting | MEDIUM | 🔴 FAIL | Only LLM calls rate-limited |
| 9 | Input Validation | MEDIUM | 🔴 FAIL | No size limits, no prompt injection protection |
| 10 | Secret Management | HIGH | 🔴 FAIL | Audit logs contain sensitive data |
| 11 | Dependencies | MEDIUM | ⚠️ WARNING | No pinning, no security scanning |
| 12 | Sandboxing | HIGH | 🔴 FAIL | No sandboxing of tool execution |
| 13 | Prompt Injection | HIGH | 🔴 FAIL | No protection against prompt injection |
| 14 | Resource Limits | MEDIUM | 🔴 FAIL | No limits on tool calls, execution time |

---

## Prioritized Recommendations

### Immediate (Critical)

1. **Remove dangerous commands from terminal allowlist** — Remove `python`, `pip`, `npm`, `node`, `curl`, `wget`, `rm`, `cp`, `mv`
2. **Fix `search_files` path traversal** — Apply `_resolve_path()` pattern
3. **Sanitize audit logs** — Redact `tool_args`, `result_preview`, and other sensitive fields
4. **Fix SSRF in `web_extract`** — Pin resolved IP, handle IPv6, prevent DNS rebinding

### Short-term (High)

5. **Add rate limiting to all tools** — Not just LLM calls
6. **Add input size limits** — Prevent resource exhaustion
7. **Remove hardcoded default DSN** — Require explicit `DATABASE_URL`
8. **Add prompt injection defenses** — Delimiters, output validation
9. **Sandbox tool execution** — Consider containerization or seccomp

### Medium-term (Medium)

10. **Pin dependency versions** — Use a lock file
11. **Add resource limits to agent loop** — Max tool calls, max execution time
12. **Validate LLM output in memory consolidator** — Sanitize before storing
13. **Add regex timeout to `search_files`** — Prevent ReDoS
14. **Restrict workdir in terminal tool** — Don't allow `$HOME` subdirectories

---

## Conclusion

AgentHarness has a solid foundation for security — parameterized queries, path traversal protection in file tools, and SSRF protection in web tools. However, the **overly permissive terminal tool** and **lack of input validation** create significant attack surfaces, especially when combined with **prompt injection** vulnerabilities. The tool is designed for local, single-user use, which limits the impact, but a malicious LLM prompt could still cause significant damage.

The most critical fixes are:
1. Narrowing the terminal command allowlist
2. Fixing the `search_files` path traversal
3. Sanitizing audit logs
4. Adding prompt injection defenses

These changes would significantly improve the security posture without requiring a major architectural overhaul.
