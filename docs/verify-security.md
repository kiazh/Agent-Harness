# Security Fix Verification Report

**Date:** 2026-10-02  
**Verifier:** Automated security audit  
**Test suite:** `tests/test_security.py` — 33/33 passed  

---

## Summary

| # | Vulnerability | Status | Notes |
|---|--------------|--------|-------|
| 1 | SSRF in `ah learn` | ✅ **FIXED** | `_is_safe_url()` applied before URL fetch |
| 2 | Path traversal in `ah export` | ✅ **FIXED** | `_resolve_path()` applied to output filename |
| 3 | Path traversal in `ah learn` | ⚠️ **PARTIAL** | Weak `..` check; `_resolve_path` imported but unused |
| 4 | Hardcoded DB password | ✅ **FIXED** | No `DEFAULT_DSN`; uses `config.get("database_url")` |
| 5 | PostgreSQL port exposed | ✅ **FIXED** | No `5432:5432` mapping in docker-compose.yml |
| 6 | SQL injection in memory store | ✅ **FIXED** | All queries use `$1, $2, ...` parameterized placeholders |
| 7 | Secret redaction | ✅ **FIXED** | `SecretRedactor` applied in `MemoryStore.add()` |
| 8 | Audit log sanitization | ✅ **FIXED** | `_sanitize_value()` redacts secrets from all audit kwargs |

**Overall: 7/8 fully fixed, 1 partial.**

---

## Detailed Findings

### 1. SSRF in `ah learn` — ✅ FIXED

**File:** `ah/cli/__init__.py:610`  
**Mechanism:** `_is_safe_url(source)` from `ah/tools/builtins.py` is called before any HTTP request.

**`_is_safe_url` checks:**
- Scheme must be `http` or `https` (rejects `file://`, `ftp://`, `gopher://`)
- Hostname must not be `localhost` or `localhost.localdomain`
- Resolves hostname via `socket.getaddrinfo()` and rejects:
  - Private IPs (`10.x`, `172.16-31.x`, `192.168.x`)
  - Loopback (`127.x`)
  - Link-local (`169.254.x`)
  - Reserved ranges
- Fails safe: unresolvable hostnames are rejected

**Tests:** 5 SSRF-specific tests pass (private IPs, localhost, non-HTTP, public URLs, unresolvable).

---

### 2. Path Traversal in `ah export` — ✅ FIXED

**File:** `ah/cli/__init__.py:342-347`  
**Mechanism:** `_resolve_path(filename)` from `ah/tools/file.py` is called before writing.

**`_resolve_path` logic:**
```python
candidate = (_BASE_DIR / path).resolve()
if not candidate.is_relative_to(_BASE_DIR):
    raise ValueError(...)
```

This correctly handles:
- `../../../etc/passwd` → resolved path escapes base dir → rejected
- `/tmp/evil_export.md` → absolute path outside base → rejected
- `../../etc/shadow` → relative escape → rejected

**Tests:** 2 export-specific tests pass (absolute escape, relative escape).

---

### 3. Path Traversal in `ah learn` — ⚠️ PARTIAL FIX

**File:** `ah/cli/__init__.py:596-604`  
**Current code:**
```python
source_path = Path(source)
if source_path.exists() and source_path.is_file():
    resolved = source_path.resolve()
    if ".." in source:
        console.print(f"[red]Path traversal blocked:[/red] '{source}' contains '..'")
        raise typer.Exit(1)
    content = resolved.read_text(encoding="utf-8")
```

**Issues:**
1. **Weak check:** Only blocks literal `..` in the path string. Does not use `_resolve_path()` which is already imported (line 591) but unused for this code path.
2. **TOCTOU race:** `source_path.exists()` check followed by `resolved.read_text()` is not atomic — a symlink could be swapped between check and read.
3. **Absolute paths not blocked:** An absolute path like `/etc/passwd` would pass the `..` check and be read if it exists.

**Recommendation:** Replace the `..` string check with `_resolve_path(source)` to match the pattern used in `ah export` and `ah/tools/file.py`.

**Tests:** The existing test only verifies `_is_safe_url` is callable — it does not test the file path traversal logic in `ah learn`.

---

### 4. Hardcoded DB Password Removed — ✅ FIXED

**File:** `ah/db/connection.py`  
**Verification:**
- No `DEFAULT_DSN` class attribute or module-level constant
- `Database.__init__` uses `dsn or config.get("database_url") or ""`
- Raises `ValueError` if no DSN configured
- No hardcoded credentials anywhere in the file

**Tests:** 3 tests pass (no `DEFAULT_DSN`, DSN from config, explicit DSN overrides config).

---

### 5. PostgreSQL Port Not Exposed — ✅ FIXED

**File:** `docker-compose.yml`  
**Verification:**
- No `ports:` section on the `db` service
- No `5432:5432` mapping anywhere in the file
- DB is only accessible via internal Docker network (`db:5432`)

**Test:** 1 test passes (asserts `"5432:5432" not in content`).

---

### 6. SQL Injection in Memory Store — ✅ FIXED

**File:** `ah/memory/store.py`  
**Verification:** All SQL queries use asyncpg parameterized placeholders (`$1, $2, ...`):

- `add()` — `$1..$8` for INSERT
- `get()` — `$1` for WHERE id
- `search()` — `$1, $2` for filters, `$3, $4` for LIMIT/OFFSET
- `search_by_embedding()` — `$1, $2, $3...` for embedding, limit, filters
- `delete()` — `$1`
- `update_access()` — `$1`
- `batch_update_access()` — `$1` with `ANY($1::uuid[])`
- `update_importance()` — `$1, $2`
- `count()` — `$1`
- `delete_by_session()` — `$1`
- `evict_weak_memories()` — `$1, $2` / `$1`
- `get_weak_memories()` — `$1, $2, $3` / `$1, $2`

The `where_clause` in `search()` is built from hardcoded strings (`agent_id = $1`, `category = $2`) — no user input is interpolated into the SQL string itself.

**Tests:** 2 tests pass (search uses `$1`, search_by_embedding uses `$1`).

---

### 7. Secret Redaction Applied — ✅ FIXED

**File:** `ah/memory/redaction.py` + `ah/memory/store.py:43-44`  
**Mechanism:** `MemoryStore.add()` calls `self._redactor.redact(content)` before storing.

**Redaction patterns cover:**
- OpenAI API keys (`sk-...`)
- Anthropic API keys (`sk-ant-...`)
- OpenRouter API keys (`sk-or-...`)
- Cohere API keys
- Bearer tokens
- Password assignments
- Database connection strings with credentials
- AWS access key IDs and secret keys
- GitHub tokens (`ghp_...`, `gho_...`)
- Slack tokens (`xox...`)
- Private key PEM blocks
- Credit card numbers
- SSN patterns
- JWT tokens
- Generic secret assignments

**Tests:** 2 tests pass (API key redaction, password redaction in `MemoryStore.add()`).

---

### 8. Audit Log Sanitization — ✅ FIXED

**File:** `ah/core/provider.py:38-83`  
**Mechanism:** `audit_log()` calls `_sanitize_value()` on every kwarg before writing to the audit log.

**`_sanitize_value` handles:**
- Strings: redacts API keys, passwords, bearer tokens, DB connection strings, AWS keys, private keys
- Dicts: recursively sanitizes all values
- Lists: recursively sanitizes all elements
- Tuples: recursively sanitizes all elements
- Non-strings: passed through unchanged

**Tests:** 8 tests pass (API key, password, bearer token, DB connection, safe strings, non-strings, dicts, audit_log integration).

---

## Additional Security Observations

### Prompt Injection Prevention (Bonus Fix #4)
**File:** `ah/skills/registry.py:48-78`  
`SkillParser._validate_content()` rejects skill content containing:
- "ignore previous instructions"
- "disregard prior instructions"
- "forget previous instructions"
- "system: you are/ignore/disregard/forget/override"
- `<system>` tags
- `[INST]` / `[/INST]` tags
- `<|im_start|>` / `<|im_end|>` tags
- Content exceeding 1 MB

### Path Traversal in RAG Loaders
**File:** `ah/rag/loaders.py:168-175`  
`FileLoader._resolve_path()` uses the same `is_relative_to()` pattern as `ah/tools/file.py`.

### Path Traversal in Built-in Tools
**File:** `ah/tools/builtins.py:182-197`  
`search_files` tool uses `_resolve_path()` to prevent directory escape.

---

## Recommendations

1. **Fix `ah learn` path traversal properly:** Replace the `..` string check with `_resolve_path(source)` to match the pattern used elsewhere. The function is already imported but unused.

2. **Add a test for `ah learn` file path traversal:** The current test only checks that `_is_safe_url` is callable. Add a test that verifies `ah learn /etc/passwd` or `ah learn ../../etc/shadow` is blocked.

3. **Consider DNS rebinding protection in `ah learn`:** The `web_extract` tool pins the resolved IP to prevent DNS rebinding. The `ah learn` URL fetch does not — consider using the same `_get_pinned_ip()` pattern.

---

## Test Results

```
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_is_safe_url_rejects_private_ips PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_is_safe_url_rejects_localhost PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_is_safe_url_rejects_non_http PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_is_safe_url_allows_public_urls PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_is_safe_url_rejects_unresolvable PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_resolve_path_rejects_traversal PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_resolve_path_allows_safe_paths PASSED
tests/test_security.py::TestLearnSSRFAndPathTraversal::test_learn_command_validates_url PASSED
tests/test_security.py::TestExportPathTraversal::test_resolve_path_prevents_absolute_escape PASSED
tests/test_security.py::TestExportPathTraversal::test_resolve_path_prevents_relative_escape PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_rejects_ignore_instructions PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_rejects_disregard_instructions PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_rejects_system_prompt_injection PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_rejects_inst_tags PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_allows_safe_content PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_skill_content_rejects_oversized PASSED
tests/test_security.py::TestPromptInjectionPrevention::test_create_skill_validates_content PASSED
tests/test_security.py::TestNoHardcodedDatabasePassword::test_no_default_dsn_constant PASSED
tests/test_security.py::TestNoHardcodedDatabasePassword::test_dsn_from_config PASSED
tests/test_security.py::TestNoHardcodedDatabasePassword::test_explicit_dsn_overrides_config PASSED
tests/test_security.py::TestNoExposedDatabasePort::test_no_port_mapping_in_compose PASSED
tests/test_security.py::TestParameterizedSQL::test_search_uses_parameterized_queries PASSED
tests/test_security.py::TestParameterizedSQL::test_search_by_embedding_uses_parameterized_queries PASSED
tests/test_security.py::TestSecretRedactionOnDirectWrites::test_store_add_redacts_secrets PASSED
tests/test_security.py::TestSecretRedactionOnDirectWrites::test_store_add_redacts_password PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_redacts_api_key PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_redacts_password PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_redacts_bearer_token PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_redacts_db_connection PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_preserves_safe_strings PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_handles_non_strings PASSED
tests/test_security.py::TestAuditLogSanitization::test_sanitize_value_handles_dicts PASSED
tests/test_security.py::TestAuditLogSanitization::test_audit_log_calls_sanitize PASSED

33 passed in 0.72s
```
