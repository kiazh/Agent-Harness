# Security Architecture Proposal for AgentHarness

**Author:** Security Advocate  
**Date:** 2026-09-30  
**Status:** Proposal  
**Scope:** Architectural security redesign of the `ah/` package

---

## Executive Summary

The existing security critique (`critique-security.md`) correctly identifies surface-level vulnerabilities — `shell=True`, missing path traversal guards, no auth, etc. — and proposes point fixes. This document argues those fixes are **necessary but insufficient**. The architecture itself is insecure by design: it has no trust boundaries, no capability model, no data-flow control, and no defense-in-depth. Patching individual vulnerabilities without addressing the architectural flaws is like locking the front door while leaving the windows open.

This proposal defines a **secure architecture** with five pillars:

1. **Input Validation at Boundaries** — validate all data crossing trust boundaries
2. **Output Encoding** — encode all data at the point of use
3. **Audit Logging** — tamper-proof, structured audit trail
4. **Rate Limiting** — resource protection at every layer
5. **Sandboxing** — isolation of all untrusted execution

---

## Part I: Why the Current Architecture Is Fundamentally Insecure

### 1.1 No Trust Boundaries

The system treats all data as equally trusted. The prompt assembler in `ah/core/context.py` concatenates system prompts, user messages, tool results, and retrieved context into a single string with no distinction between trusted and untrusted content:

```python
# ah/core/context.py:231-293
def assemble(self, system_prompt, goal, recent_chunks, retrieved_chunks, query):
    parts = []
    parts.append(system_prompt)        # Trusted
    parts.append(goal)                # Semi-trusted (user-set)
    parts.append(query)               # Untrusted (user input)
    parts.append(recent_text)         # Semi-trusted (tool results)
    parts.append(retrieved_text)      # Semi-trusted (DB content)
    return "\n".join(parts)           # All mixed together
```

**The flaw:** There is no concept of a trust level. The LLM cannot distinguish between "system instruction" and "user data" because they are concatenated into a single flat string. This enables prompt injection — an attacker crafts input that appears to be a system instruction.

**The fix:** Implement a typed message model where each segment carries a trust level, and the LLM API call uses structured messages (system/user/assistant/tool roles) rather than a single concatenated string.

### 1.2 No Capability Model

Tools are registered globally (`ah/tools/base.py:121` — `registry = ToolRegistry()`) and any tool can be called by any agent in any session. There is no concept of:

- "This session may only use read-only tools"
- "This agent may only access files in /workspace"
- "This user may only run git commands"

**The flaw:** A compromised LLM can call any tool with any arguments. The `terminal` tool can run `rm -rf /`, the `write_file` tool can overwrite `~/.ssh/authorized_keys`, and the `web_extract` tool can access internal services.

**The fix:** Implement a capability-based access control system where each session/agent is granted a specific set of capabilities, and the tool registry enforces them at execution time.

### 1.3 No Data-Flow Control

Sensitive data flows freely through the system:

```
User Input → LLM → Tool → Database → Context → LLM → User
```

There is no tracking of where data originates, where it flows, or whether it contains secrets. API keys from environment variables, file contents from `~/.ssh/`, and database credentials can all end up in:

- LLM prompts (sent to external APIs)
- Database context chunks (stored indefinitely)
- Tool results (logged, displayed)
- Error messages (printed to console)

**The flaw:** A single `read_file` of `~/.ssh/id_rsa` followed by a `terminal` command that echoes the result will exfiltrate the private key to the LLM provider and store it in the database.

**The fix:** Implement data-flow tracking with taint labels. Data read from sensitive sources is tagged, and the system can block or warn when tainted data crosses a trust boundary (e.g., being sent to an external API).

### 1.4 No Defense in Depth

Every security control is a single point of failure:

| Control | Single Point of Failure |
|---------|------------------------|
| Command allowlist | If bypassed, arbitrary command execution |
| Path validation | If bypassed, arbitrary file access |
| SSRF protection | If bypassed, internal network access |
| MessagePack unpack | If bypassed, potential code execution |

**The flaw:** There are no redundant controls. If the allowlist check in `terminal.py` is bypassed (e.g., via a Unicode normalization attack or a logic error), there is nothing stopping arbitrary command execution.

**The fix:** Layer multiple independent controls. For example: allowlist + sandbox + resource limits + audit logging. If any one control fails, the others still provide protection.

### 1.5 No Secure Defaults

The system defaults to permissive:

- No authentication required by default
- No sandboxing enabled by default
- No rate limiting configured by default
- No audit logging configured by default
- Database connects as superuser by default
- All tools available to all agents by default

**The flaw:** A user who runs `ah chat "hello"` for the first time gets a system with zero security controls active. The secure path requires explicit configuration.

**The fix:** Secure by default. Authentication required, sandboxing enabled, rate limiting active, audit logging on. Users must explicitly opt out of security controls, not opt in.

### 1.6 No Multi-Tenancy Isolation

All sessions share the same database, the same tool registry, the same file system, and the same network. There is no isolation between:

- Different users
- Different agents
- Different sessions
- Different trust levels

**The flaw:** A malicious agent in one session can read the context of another session, access files created by another session, or consume resources needed by another session.

**The fix:** Implement tenant isolation with separate database schemas, separate file system roots, and separate resource quotas per tenant.

### 1.7 No Security Monitoring

There is no:

- Anomaly detection (e.g., "this agent is reading an unusual number of files")
- Alerting (e.g., "command execution failed 50 times in 1 minute")
- Intrusion detection (e.g., "this session is attempting to access /etc/shadow")
- Forensic readiness (e.g., "can we reconstruct what happened after a breach?")

**The flaw:** Attacks go undetected. By the time a human notices something is wrong, the attacker has already exfiltrated data and covered their tracks.

**The fix:** Implement real-time security monitoring with anomaly detection and alerting.

---

## Part II: The Five Pillars

### Pillar 1: Input Validation at Boundaries

#### Principle

**All data crossing a trust boundary must be validated.** A trust boundary exists whenever data moves from a less-trusted context to a more-trusted context:

- User input → LLM prompt
- LLM output → tool arguments
- Tool output → LLM prompt
- File content → LLM prompt
- Web content → LLM prompt
- Database content → LLM prompt

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Trust Boundary                        │
│                                                          │
│  Untrusted ──→ [Validator] ──→ Trusted                  │
│                                                          │
│  Validators:                                             │
│  - Schema validation (type, length, format)             │
│  - Semantic validation (range, allowed values)          │
│  - Contextual validation (safe for this use case)       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Implementation

**1.1 Define a validation layer**

```python
# ah/security/validation.py
from dataclasses import dataclass
from typing import Any, Callable, Optional
import re

@dataclass
class ValidationResult:
    valid: bool
    value: Any = None
    error: str = ""

class Validator:
    """Base validator — chainable."""
    def __init__(self, *checks: Callable[[Any], Optional[str]]):
        self.checks = checks
    
    def validate(self, value: Any) -> ValidationResult:
        for check in self.checks:
            error = check(value)
            if error:
                return ValidationResult(valid=False, error=error)
        return ValidationResult(valid=True, value=value)

# Common validators
def max_length(n: int) -> Callable:
    def check(v: Any) -> Optional[str]:
        if isinstance(v, str) and len(v) > n:
            return f"Exceeds maximum length of {n}"
        return None
    return check

def pattern(regex: str) -> Callable:
    def check(v: Any) -> Optional[str]:
        if isinstance(v, str) and not re.match(regex, v):
            return f"Does not match required pattern"
        return None
    return check

def safe_path(base: str) -> Callable:
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return "Path must be a string"
        from pathlib import Path
        try:
            resolved = (Path(base) / v).resolve()
            if not resolved.is_relative_to(Path(base).resolve()):
                return "Path escapes base directory"
        except (OSError, ValueError):
            return "Invalid path"
        return None
    return check

def safe_command(allowed: frozenset) -> Callable:
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, str):
            return "Command must be a string"
        import shlex
        try:
            parts = shlex.split(v)
        except ValueError:
            return "Invalid command syntax"
        if not parts:
            return "Empty command"
        if parts[0] not in allowed:
            return f"Command '{parts[0]}' not allowed"
        return None
    return check
```

**1.2 Validate at every boundary**

```python
# ah/core/agent.py — validate LLM output before tool execution
async def run(self, session_id, user_message, verbose=True):
    # Validate user input
    user_val = self.user_input_validator.validate(user_message)
    if not user_val.valid:
        return AgentResponse(content=f"Invalid input: {user_val.error}")
    
    # ... agent loop ...
    
    for tc in response.tool_calls:
        tool_name = tc.get("function", {}).get("name")
        
        # Validate tool name
        if not self.tool_name_validator.validate(tool_name).valid:
            continue
        
        # Parse and validate tool arguments
        try:
            tool_args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError:
            continue
        
        # Validate arguments against tool schema
        tool = registry.get_tool(tool_name)
        if tool:
            args_val = self._validate_tool_args(tool, tool_args)
            if not args_val.valid:
                continue
        
        # Execute
        result = await registry.execute(tool_name, **tool_args)
```

**1.3 Validate tool outputs before storing**

```python
# ah/tools/base.py — wrap tool execution with output validation
async def execute(self, name: str, **kwargs) -> Any:
    if name not in self._tools:
        raise ValueError(f"Tool '{name}' not registered")
    tool = self._tools[name]
    
    # Validate inputs
    input_val = self._validate_inputs(tool, kwargs)
    if not input_val.valid:
        return f"Error: {input_val.error}"
    
    # Execute
    if tool.is_async:
        result = await tool.func(**kwargs)
    else:
        result = tool.func(**kwargs)
    
    # Validate output
    output_val = self._validate_output(tool, result)
    if not output_val.valid:
        return f"Error: {output_val.error}"
    
    return result
```

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Validate at every boundary, not just at entry | Defense in depth — if one validator is bypassed, others still protect |
| Use schema-based validation | Declarative, testable, and can be auto-generated from tool definitions |
| Fail closed (reject invalid input) | Safer than fail-open — better to reject valid input than accept invalid input |
| Return structured errors | Enables programmatic handling and logging |

---

### Pillar 2: Output Encoding

#### Principle

**All data must be encoded at the point of use, based on the output context.** The same data may need different encoding depending on where it is being sent:

- HTML context → HTML entity encoding
- JSON context → JSON encoding
- Shell context → Shell escaping
- SQL context → Parameterized queries (already done)
- LLM prompt context → Structured message format
- Log context → Structured logging format

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Output Contexts                        │
│                                                          │
│  Data ──→ [Encoder] ──→ HTML / JSON / Shell / LLM / Log │
│                                                          │
│  Encoders:                                               │
│  - HTML: html.escape()                                   │
│  - JSON: json.dumps()                                   │
│  - Shell: shlex.quote()                                 │
│  - LLM: structured message with trust labels            │
│  - Log: structured JSON with sanitization               │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Implementation

**2.1 Define output encoders**

```python
# ah/security/encoding.py
import html
import json
import shlex
from typing import Any

class OutputEncoder:
    """Encode data for specific output contexts."""
    
    @staticmethod
    def for_html(text: str) -> str:
        """Encode for HTML context (prevents XSS)."""
        return html.escape(text, quote=True)
    
    @staticmethod
    def for_json(data: Any) -> str:
        """Encode for JSON context."""
        return json.dumps(data, ensure_ascii=True, default=str)
    
    @staticmethod
    def for_shell(text: str) -> str:
        """Encode for shell context."""
        return shlex.quote(text)
    
    @staticmethod
    def for_llm(text: str, trust_level: str = "untrusted") -> dict:
        """Encode for LLM context — returns structured message."""
        return {
            "role": "user" if trust_level == "untrusted" else "system",
            "content": text,
            "metadata": {
                "trust_level": trust_level,
                "source": "user_input",  # or "tool_result", "file_content", etc.
            }
        }
    
    @staticmethod
    def for_log(data: dict) -> str:
        """Encode for log context — structured, sanitized."""
        # Remove sensitive fields
        sanitized = {k: v for k, v in data.items() 
                     if k not in SENSITIVE_FIELDS}
        return json.dumps(sanitized, default=str)

SENSITIVE_FIELDS = frozenset({
    "password", "api_key", "token", "secret", "credential",
    "private_key", "auth", "authorization", "cookie",
})
```

**2.2 Encode at every output point**

```python
# ah/cli.py — encode output for console
from ah.security.encoding import OutputEncoder

@app.command()
def chat(message: str, ...):
    # ...
    response_text = event.response.content
    # Encode for console output (Rich handles HTML, but be explicit)
    console.print(OutputEncoder.for_html(response_text))
```

```python
# ah/core/context.py — encode tool results for LLM
from ah.security.encoding import OutputEncoder

# In PromptAssembler.assemble():
for chunk_data in recent_chunks[:3]:
    compressed = self._compress_chunk(chunk_data)
    # Encode with trust level metadata
    encoded = OutputEncoder.for_llm(compressed, trust_level="tool_result")
    recent_text += encoded["content"] + "\n"
```

**2.3 Prevent stored XSS**

```python
# ah/tools/file.py — encode file content before storing in context
from ah.security.encoding import OutputEncoder

@registry.register(name="read_file", ...)
def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    # ... read file ...
    content = "".join(selected)
    
    # Encode before returning (will be stored in context and sent to LLM)
    return OutputEncoder.for_llm(content, trust_level="file_content")["content"]
```

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Encode at point of use, not at storage | The same data may be used in different contexts |
| Use context-specific encoders | HTML encoding is wrong for JSON, and vice versa |
| Default to most restrictive encoding | If context is unknown, encode for the most sensitive context |
| Sanitize logs | Never log secrets, even in error messages |

---

### Pillar 3: Audit Logging

#### Principle

**Every security-relevant event must be logged to a tamper-proof audit trail.** The audit log must be:

- **Structured** — machine-parseable JSON
- **Complete** — every security event, not just errors
- **Tamper-proof** — append-only, with integrity verification
- **Searchable** — queryable by time, user, session, event type
- **Retained** — kept for a configurable retention period

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Audit Pipeline                         │
│                                                          │
│  Event ──→ [Collector] ──→ [Validator] ──→ [Writer]     │
│                                                          │
│  Collector: captures security events                     │
│  Validator: ensures event is well-formed                 │
│  Writer: writes to append-only store                     │
│                                                          │
│  Stores:                                                 │
│  - PostgreSQL audit_log table (primary)                 │
│  - File-based WORM store (backup)                       │
│  - External SIEM (optional)                             │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Implementation

**3.1 Define audit event schema**

```python
# ah/security/audit.py
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional
import json
import hashlib

class AuditEventType(str, Enum):
    # Authentication events
    AUTH_SUCCESS = "auth.success"
    AUTH_FAILURE = "auth.failure"
    AUTH_LOGOUT = "auth.logout"
    
    # Authorization events
    AUTHZ_DENIED = "authz.denied"
    AUTHZ_GRANTED = "authz.granted"
    
    # Tool execution events
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    
    # Data access events
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    DB_QUERY = "db.query"
    LLM_CALL = "llm.call"
    
    # Session events
    SESSION_CREATE = "session.create"
    SESSION_RESUME = "session.resume"
    SESSION_END = "session.end"
    
    # Security events
    RATE_LIMIT_HIT = "rate_limit.hit"
    SANDBOX_VIOLATION = "sandbox.violation"
    POLICY_VIOLATION = "policy.violation"

@dataclass
class AuditEvent:
    timestamp: datetime
    event_type: AuditEventType
    session_id: Optional[str]
    agent_id: Optional[str]
    user_id: Optional[str]
    details: dict[str, Any]
    integrity_hash: str = ""
    
    def __post_init__(self):
        if not self.integrity_hash:
            self.integrity_hash = self._compute_hash()
    
    def _compute_hash(self) -> str:
        """Compute integrity hash for tamper detection."""
        data = f"{self.timestamp.isoformat()}|{self.event_type.value}|{json.dumps(self.details, sort_keys=True, default=str)}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "details": self.details,
            "integrity_hash": self.integrity_hash,
        }
```

**3.2 Audit logger**

```python
# ah/security/audit.py
import logging
import asyncio
from ah.db.connection import db

class AuditLogger:
    """Tamper-proof audit logger."""
    
    def __init__(self):
        self._logger = logging.getLogger("audit")
        self._queue = asyncio.Queue()
        self._worker_task = None
    
    async def start(self):
        """Start the background audit writer."""
        self._worker_task = asyncio.create_task(self._writer_loop())
    
    async def stop(self):
        """Stop the audit writer."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
    
    async def log(self, event: AuditEvent):
        """Queue an audit event for writing."""
        await self._queue.put(event)
    
    async def _writer_loop(self):
        """Background worker that writes audit events to the database."""
        while True:
            try:
                event = await self._queue.get()
                await self._write_event(event)
            except asyncio.CancelledError:
                # Drain remaining events
                while not self._queue.empty():
                    event = await self._queue.get()
                    await self._write_event(event)
                raise
            except Exception as e:
                # Log to fallback (file) if database write fails
                self._logger.error(f"Audit write failed: {e}")
                self._fallback_write(event)
    
    async def _write_event(self, event: AuditEvent):
        """Write audit event to the database."""
        await db.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, session_id, agent_id, user_id, details, integrity_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            event.timestamp,
            event.event_type.value,
            event.session_id,
            event.agent_id,
            event.user_id,
            json.dumps(event.details, default=str),
            event.integrity_hash,
        )
    
    def _fallback_write(self, event: AuditEvent):
        """Fallback write to file if database is unavailable."""
        with open("audit.log", "a") as f:
            f.write(json.dumps(event.to_dict(), default=str) + "\n")

audit_logger = AuditLogger()
```

**3.3 Instrument the codebase**

```python
# ah/tools/terminal.py — audit log all command executions
from ah.security.audit import audit_logger, AuditEvent, AuditEventType

@registry.register(name="terminal", ...)
def terminal(command: str, timeout: int = 60, workdir: str = ".") -> str:
    # ... validation ...
    
    # Audit log the command
    asyncio.create_task(audit_logger.log(AuditEvent(
        timestamp=datetime.utcnow(),
        event_type=AuditEventType.TOOL_CALL,
        session_id=current_session_id(),
        agent_id=current_agent_id(),
        user_id=current_user_id(),
        details={
            "tool": "terminal",
            "command": command,
            "timeout": timeout,
            "workdir": workdir,
        }
    )))
    
    # ... execute command ...
    
    # Audit log the result
    asyncio.create_task(audit_logger.log(AuditEvent(
        timestamp=datetime.utcnow(),
        event_type=AuditEventType.TOOL_RESULT,
        session_id=current_session_id(),
        agent_id=current_agent_id(),
        user_id=current_user_id(),
        details={
            "tool": "terminal",
            "command": command,
            "exit_code": result.returncode,
            "output_length": len(output),
        }
    )))
    
    return output
```

**3.4 Add audit_log table to schema**

```sql
-- Add to ah/db/schema.sql
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type TEXT NOT NULL,
    session_id UUID,
    agent_id TEXT,
    user_id TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    integrity_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id);

-- Make audit log append-only (no updates, no deletes)
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Audit log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
```

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Async queue-based writing | Don't block the agent loop for audit writes |
| Integrity hashes | Enable tamper detection |
| Append-only database | Prevent log modification |
| Fallback to file | Ensure audit events are never lost |
| Structured JSON | Enable querying and analysis |

---

### Pillar 4: Rate Limiting

#### Principle

**Every resource consumption must be rate-limited.** Rate limiting protects against:

- LLM API credit exhaustion
- Database connection pool exhaustion
- Disk space exhaustion
- CPU/memory exhaustion
- Denial of service

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Rate Limiting Layers                    │
│                                                          │
│  Layer 1: User-level (per user, per minute)             │
│  Layer 2: Session-level (per session, per minute)       │
│  Layer 3: Tool-level (per tool, per minute)             │
│  Layer 4: Global (system-wide, per second)              │
│                                                          │
│  Algorithm: Token bucket                                 │
│  Storage: In-memory (with Redis option for distributed) │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Implementation

**4.1 Token bucket rate limiter**

```python
# ah/security/rate_limit.py
import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional
from collections import defaultdict

@dataclass
class TokenBucket:
    """Token bucket rate limiter."""
    capacity: int      # Maximum tokens
    refill_rate: float # Tokens per second
    tokens: float = field(default=0)
    last_refill: float = field(default_factory=time.time)
    
    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens. Returns True if successful."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

class RateLimiter:
    """Multi-level rate limiter."""
    
    def __init__(self):
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()
    
    async def is_allowed(
        self,
        key: str,
        capacity: int,
        refill_rate: float,
    ) -> bool:
        """Check if an action is allowed under the rate limit."""
        async with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(
                    capacity=capacity,
                    refill_rate=refill_rate,
                    tokens=capacity,  # Start with full bucket
                )
            return self._buckets[key].consume(1)
    
    async def check_rate_limit(
        self,
        key: str,
        capacity: int,
        refill_rate: float,
        error_message: str = "Rate limit exceeded",
    ):
        """Check rate limit and raise exception if exceeded."""
        if not await self.is_allowed(key, capacity, refill_rate):
            raise RateLimitExceeded(error_message)

class RateLimitExceeded(Exception):
    pass

# Global rate limiter instance
rate_limiter = RateLimiter()
```

**4.2 Apply rate limiting at every layer**

```python
# ah/core/agent.py — rate limit LLM calls
from ah.security.rate_limit import rate_limiter, RateLimitExceeded

class ReActAgent:
    async def _call_llm_with_retry(self, messages, tools):
        # Rate limit: 10 LLM calls per minute per session
        try:
            await rate_limiter.check_rate_limit(
                key=f"llm:{self.session_id}",
                capacity=10,
                refill_rate=10/60,  # 10 per minute
                error_message="LLM rate limit exceeded. Please wait before trying again.",
            )
        except RateLimitExceeded as e:
            return LLMResponse(content=str(e), model="error", usage={})
        
        # ... existing retry logic ...
```

```python
# ah/tools/terminal.py — rate limit command execution
from ah.security.rate_limit import rate_limiter, RateLimitExceeded

@registry.register(name="terminal", ...)
def terminal(command: str, timeout: int = 60, workdir: str = ".") -> str:
    # Rate limit: 30 commands per minute per session
    try:
        # Note: this is sync, so we use a sync version
        rate_limiter.check_rate_limit_sync(
            key=f"terminal:{current_session_id()}",
            capacity=30,
            refill_rate=30/60,
        )
    except RateLimitExceeded:
        return "Error: Command rate limit exceeded. Please wait before running more commands."
    
    # ... existing execution logic ...
```

```python
# ah/core/session.py — rate limit session creation
from ah.security.rate_limit import rate_limiter, RateLimitExceeded

class SessionManager:
    async def create(self, ...):
        # Rate limit: 5 sessions per minute per user
        try:
            await rate_limiter.check_rate_limit(
                key=f"session_create:{current_user_id()}",
                capacity=5,
                refill_rate=5/60,
            )
        except RateLimitExceeded:
            raise ValueError("Session creation rate limit exceeded")
        
        # ... existing creation logic ...
```

**4.3 Add rate_limit table for distributed tracking**

```sql
-- Add to ah/db/schema.sql
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key TEXT PRIMARY KEY,
    tokens FLOAT NOT NULL,
    last_refill TIMESTAMPTZ NOT NULL DEFAULT now(),
    capacity INT NOT NULL,
    refill_rate FLOAT NOT NULL
);
```

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Token bucket algorithm | Allows bursts while maintaining average rate |
| Multi-layer limiting | Protects against different attack vectors |
| Per-session keys | Prevents one session from affecting all others |
| Async-compatible | Works with the async agent loop |
| Database-backed option | Enables distributed rate limiting |

---

### Pillar 5: Sandboxing

#### Principle

**All untrusted execution must be isolated in a sandbox.** The sandbox provides:

- **Process isolation** — separate process for each tool execution
- **Filesystem isolation** — only accessible directories are visible
- **Network isolation** — no network access by default
- **Resource limits** — CPU, memory, disk, process count limits
- **Time limits** — hard timeout on execution

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Sandbox Architecture                   │
│                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │  Agent      │    │  Sandbox    │    │  Tool       │ │
│  │  Process    │───→│  Manager    │───→│  Execution  │ │
│  │  (trusted)  │    │  (mediator) │    │  (untrusted)│ │
│  └─────────────┘    └─────────────┘    └─────────────┘ │
│                            │                             │
│                     ┌──────┴──────┐                      │
│                     │  Isolation  │                      │
│                     │  - chroot   │                      │
│                     │  - namespaces│                     │
│                     │  - cgroups  │                      │
│                     │  - seccomp  │                      │
│                     └─────────────┘                      │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

#### Implementation

**5.1 Sandbox manager**

```python
# ah/security/sandbox.py
import subprocess
import tempfile
import os
import resource
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

@dataclass
class SandboxConfig:
    """Sandbox configuration."""
    max_memory_mb: int = 512
    max_cpu_percent: int = 50
    max_processes: int = 10
    max_disk_mb: int = 100
    network_access: bool = False
    allowed_paths: tuple = ()
    timeout_seconds: int = 60

class Sandbox:
    """Execute commands in an isolated sandbox."""
    
    def __init__(self, config: SandboxConfig):
        self.config = config
    
    def execute(self, args: list[str], cwd: Optional[str] = None) -> dict:
        """Execute a command in the sandbox."""
        # Create a temporary directory for the sandbox
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up the sandbox environment
            self._setup_sandbox(tmpdir)
            
            # Execute with resource limits
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_seconds,
                    cwd=cwd or tmpdir,
                    preexec_fn=self._set_resource_limits,
                    env=self._sandbox_env(),
                )
                return {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }
            except subprocess.TimeoutExpired:
                return {
                    "stdout": "",
                    "stderr": f"Command timed out after {self.config.timeout_seconds}s",
                    "returncode": -1,
                }
    
    def _set_resource_limits(self):
        """Set resource limits for the child process (Unix only)."""
        # Memory limit
        resource.setrlimit(
            resource.RLIMIT_AS,
            (self.config.max_memory_mb * 1024 * 1024,) * 2
        )
        # CPU limit
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (self.config.timeout_seconds, self.config.timeout_seconds + 1)
        )
        # Process limit
        resource.setrlimit(
            resource.RLIMIT_NPROC,
            (self.config.max_processes, self.config.max_processes)
        )
        # Disk limit
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (self.config.max_disk_mb * 1024 * 1024,) * 2
        )
    
    def _sandbox_env(self) -> dict:
        """Create a minimal environment for the sandbox."""
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            # Remove all other environment variables
        }
    
    def _setup_sandbox(self, tmpdir: str):
        """Set up the sandbox filesystem."""
        # Create allowed directories
        for path in self.config.allowed_paths:
            os.makedirs(path, exist_ok=True)
```

**5.2 Docker-based sandbox (recommended for production)**

```python
# ah/security/sandbox_docker.py
import docker
from typing import Optional

class DockerSandbox:
    """Execute commands in a Docker container."""
    
    def __init__(self, image: str = "alpine:latest"):
        self.image = image
        self.client = docker.from_env()
    
    def execute(self, args: list[str], cwd: Optional[str] = None) -> dict:
        """Execute a command in a Docker container."""
        try:
            container = self.client.containers.run(
                self.image,
                command=args,
                detach=False,
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,  # 50% CPU
                network_mode="none",  # No network
                read_only=True,  # Read-only filesystem
                volumes={cwd: {"bind": "/workspace", "mode": "rw"}} if cwd else {},
                timeout=60,
                remove=True,
            )
            return {
                "stdout": container.decode("utf-8"),
                "stderr": "",
                "returncode": 0,
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }
```

**5.3 Integrate sandbox into tool execution**

```python
# ah/tools/terminal.py — execute in sandbox
from ah.security.sandbox import Sandbox, SandboxConfig

_sandbox = Sandbox(SandboxConfig(
    max_memory_mb=512,
    max_cpu_percent=50,
    max_processes=10,
    network_access=False,
    allowed_paths=("/workspace", "/tmp"),
    timeout_seconds=60,
))

@registry.register(name="terminal", ...)
def terminal(command: str, timeout: int = 60, workdir: str = ".") -> str:
    # ... validation ...
    
    # Execute in sandbox
    result = _sandbox.execute(args, cwd=workdir)
    
    output = ""
    if result["stdout"]:
        output += result["stdout"]
    if result["stderr"]:
        output += ("\n" if output else "") + result["stderr"]
    if not output:
        output = f"(exit code {result['returncode']}, no output)"
    return output
```

#### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Resource limits via `preexec_fn` | Works on Linux without external dependencies |
| Docker for production | Stronger isolation via container namespaces |
| Minimal environment | Remove all environment variables except PATH and HOME |
| Read-only filesystem | Prevent modification of system files |
| No network by default | Prevent data exfiltration and SSRF |

---

## Part III: Implementation Roadmap

### Phase 1: Foundation (Week 1-2)

| Task | Priority | Effort |
|------|----------|--------|
| Add `ah/security/` package with validation, encoding, audit, rate_limit, sandbox modules | P0 | 2 days |
| Add `audit_log` table to schema | P0 | 1 day |
| Instrument `terminal.py` with validation + audit + rate limiting | P0 | 1 day |
| Instrument `file.py` with validation + audit + rate limiting | P0 | 1 day |
| Instrument `builtins.py` with validation + audit + rate limiting | P0 | 1 day |
| Add rate limiting to `agent.py` LLM calls | P0 | 1 day |
| Add rate limiting to `session.py` session creation | P1 | 0.5 day |

### Phase 2: Hardening (Week 3-4)

| Task | Priority | Effort |
|------|----------|--------|
| Implement sandbox for terminal tool | P0 | 2 days |
| Add output encoding to all tool outputs | P1 | 1 day |
| Add data-flow tracking (taint labels) | P1 | 2 days |
| Implement capability-based access control | P1 | 2 days |
| Add security monitoring and alerting | P2 | 2 days |

### Phase 3: Production Readiness (Week 5-6)

| Task | Priority | Effort |
|------|----------|--------|
| Docker-based sandbox | P1 | 2 days |
| Distributed rate limiting (Redis) | P2 | 1 day |
| Multi-tenancy isolation | P2 | 3 days |
| Security test suite | P0 | 2 days |
| Penetration testing | P0 | 2 days |
| Documentation and runbooks | P1 | 1 day |

---

## Part IV: Security Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AgentHarness Security Architecture            │
│                                                                      │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  User    │    │  Auth    │    │  Rate    │    │  Audit   │      │
│  │  Input   │───→│  Layer   │───→│  Limiter │───→│  Logger  │      │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
│       │                                               │             │
│       ▼                                               ▼             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │  Input   │    │  Agent   │    │  Tool    │    │  Output  │      │
│  │  Valid.  │───→│  Loop    │───→│  Registry│───→│  Encoder │      │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘      │
│       │               │               │               │             │
│       │               ▼               ▼               │             │
│       │        ┌──────────┐    ┌──────────┐          │             │
│       │        │  LLM     │    │  Sandbox │          │             │
│       │        │  Provider │    │  (exec)  │          │             │
│       │        └──────────┘    └──────────┘          │             │
│       │               │               │               │             │
│       ▼               ▼               ▼               ▼             │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                    Audit Log (tamper-proof)                │      │
│  └──────────────────────────────────────────────────────────┘      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Part V: Threat Model

| Threat | Likelihood | Impact | Mitigation |
|--------|-----------|--------|------------|
| Prompt injection via user input | High | High | Input validation, structured messages with trust levels |
| Prompt injection via file content | High | High | Input validation, output encoding, sandboxing |
| Prompt injection via web content | Medium | High | SSRF protection, input validation, output encoding |
| Command injection via LLM | High | Critical | Allowlist, sandbox, input validation |
| Path traversal via LLM | High | Critical | Path validation, sandbox filesystem isolation |
| SSRF via web tools | Medium | High | URL validation, network isolation in sandbox |
| Resource exhaustion | Medium | High | Rate limiting, resource limits in sandbox |
| Data exfiltration via LLM | Medium | High | Data-flow tracking, output encoding |
| Audit log tampering | Low | Medium | Append-only database, integrity hashes |
| Credential theft | Medium | High | Secret management, no hardcoded credentials |

---

## Part VI: Conclusion

The current AgentHarness architecture has **deep security flaws** that cannot be fixed with point patches. The lack of trust boundaries, capability model, data-flow control, defense-in-depth, secure defaults, multi-tenancy isolation, and security monitoring creates a system that is fundamentally insecure.

The five pillars proposed in this document — **input validation at boundaries, output encoding, audit logging, rate limiting, and sandboxing** — provide a comprehensive architectural framework for securing AgentHarness. Implementing these pillars will transform the system from a collection of vulnerabilities into a secure, production-ready agent framework.

**The cost of implementing these controls is far less than the cost of a security breach.**

---

## Appendix A: Security Checklist

- [ ] All user input validated at entry point
- [ ] All LLM output validated before tool execution
- [ ] All tool output encoded for target context
- [ ] All security events logged to audit trail
- [ ] All resources rate-limited
- [ ] All command execution sandboxed
- [ ] All file operations path-validated
- [ ] All web requests SSRF-protected
- [ ] All secrets managed securely (no hardcoding)
- [ ] All database connections encrypted
- [ ] All database users least-privilege
- [ ] All error messages sanitized (no secret leakage)
- [ ] All dependencies scanned for vulnerabilities
- [ ] All security controls tested
- [ ] All security documentation complete

## Appendix B: References

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP LLM Top 10](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
- [NIST Cybersecurity Framework](https://www.nist.gov/cyberframework)
- [CWE Top 25](https://cwe.mitre.org/top25/)
- [AgentHarness Security Critique](critique-security.md)
