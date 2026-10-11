"""Tool → ActionRequest mapping (Phase D).

Centralizes how each registered tool maps to operations/capabilities so the
agent loop gates every invocation uniformly. Validation errors (malformed
paths, unknown binaries) stay distinct from authorization: approval can never
make an invalid action valid.
"""

from __future__ import annotations

import json


def action_for_tool(name: str, args: dict) -> dict:
    """Return broker kwargs for tool *name* with *args* (operation/targets/...)."""
    args = args or {}
    if name in ("read_file",):
        return {"operation": "file.read", "targets": [str(args.get("path", ""))]}
    if name in ("write_file",):
        content = args.get("content", "")
        return {
            "operation": "file.write",
            "targets": [str(args.get("path", ""))],
            "content": str(content)[:100000],
        }
    if name in ("list_files", "search_files"):
        return {"operation": "file.read", "targets": [str(args.get("path", "."))]}
    if name in ("terminal",):
        cmd = str(args.get("command", ""))
        from ah.permissions.shell import parse_terminal_command

        parsed = parse_terminal_command(cmd)
        try:
            timeout = int(args.get("timeout", 60))
        except Exception:
            timeout = 60
        return {
            "operation": "process.exec",
            "argv": parsed.argv,
            "shell_payload": parsed.payload,
            "cwd": str(args.get("workdir", ".")),
            "timeout": timeout,
        }
    if name in ("index_document",):
        return {"operation": "file.read", "targets": [str(args.get("path", ""))]}
    if name == "search_documents":
        return {"operation": "context.read", "targets": []}
    if name in ("web_search", "web_extract"):
        return {
            "operation": "network.fetch",
            "network": [str(args.get("url", args.get("query", "")))[:300]],
        }
    if name in ("delegate",):
        return {"operation": "delegate", "targets": [str(args.get("agent", ""))]}
    if name in ("share_memory", "remember", "recall", "learn_personality"):
        return {"operation": "memory", "targets": []}
    if name in ("skill_list", "skill_read", "skill_resource"):
        return {"operation": "skill.read", "targets": [str(args.get("skill_name", ""))]}
    if name in ("session_recall", "session_recall_window", "list_agents", "harness_status"):
        return {"operation": "context.read", "targets": []}
    # Unknown/extensible tools: declared mutating effects become approval
    # capabilities instead of silent in-process execution. Effect-free tools
    # stay in-process work.
    effects = _declared_effects(name)
    if _MUTATING_EFFECTS.intersection(effects):
        return {
            "operation": f"tool.{name}",
            "targets": [],
            "capabilities": ["mutating-tool", *(f"fx:{e}" for e in effects)],
            # Approval must bind the actual extension arguments too.
            "content": json.dumps(args, sort_keys=True, ensure_ascii=False),
        }
    return {"operation": f"tool.{name}", "targets": []}


def _declared_effects(name: str) -> tuple[str, ...]:
    from ah.tools.base import registry

    tool = registry._tools.get(name)
    return tuple(getattr(tool, "effects", None) or ())


_MUTATING_EFFECTS = frozenset({"fs.write", "exec", "net", "delegate"})
