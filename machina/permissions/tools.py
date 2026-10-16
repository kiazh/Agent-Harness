"""Tool → ActionRequest mapping (Phase D).

Centralizes how each registered tool maps to operations/capabilities so the
agent loop gates every invocation uniformly. Validation errors (malformed
paths, unknown binaries) stay distinct from authorization: approval can never
make an invalid action valid.
"""

from __future__ import annotations

import json

from machina.connectors.execution import call_spec
from machina.connectors.management import connect_spec, disconnect_spec


def action_for_tool(name: str, args: dict) -> dict:
    """Return broker kwargs for tool *name* with *args* (operation/targets/...)."""
    args = args or {}
    if name == "mcp_call":
        from machina.core.runtime import runtime_services

        return call_spec(runtime_services.get_connector_service(), args)
    if name in ("mcp_list", "mcp_describe"):
        return {"operation": "context.read", "targets": []}
    if name == "mcp_connect":
        # Adding a server grants new tools + network/process reach: the exact
        # request (transport destination, trust, allowlists) always requires
        # explicit approval (mcp_external_review) in ask/workspace/sandbox.
        return connect_spec(args)
    if name == "mcp_disconnect":
        # Removal closes reach and deletes durable configuration; same review
        # capability as adding, never implicit.
        return disconnect_spec(args)
    if name in ("read_file",):
        return {"operation": "file.read", "targets": [str(args.get("path", ""))]}
    if name == "code_intelligence":
        # T10 least-risk mapping: a read-only query of the workspace's own
        # code intelligence is the same *kind* of action as read_file — a
        # named file inside the request-bound workspace root — so it maps to
        # file.read and inherits the existing "trusted project read" /
        # "routine project file operation" decisions (allowed inside the
        # root; anything outside falls into the sensitive approval path).
        # It declares no mutating effect, and the workspace service
        # independently re-enforces its own authorized root plus the editor
        # buffer/disk distinction, so this mapping widens no authority and
        # needs no new operation or policy change.
        return {"operation": "file.read", "targets": [str(args.get("path", ""))]}
    if name in ("write_file",):
        content = args.get("content", "")
        return {
            "operation": "file.write",
            "targets": [str(args.get("path", ""))],
            # FM3-F4: the FULL content is digested/authorized. Truncation (and
            # its `…(+N chars)` marker) is display-only, applied by the
            # preview/card builders, never before the digest is computed.
            "content": str(content),
        }
    if name in ("list_files", "search_files"):
        return {"operation": "file.read", "targets": [str(args.get("path", "."))]}
    if name in ("terminal",):
        cmd = str(args.get("command", ""))
        from machina.permissions.shell import parse_terminal_command

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
    if name in ("web_search", "web_extract", "web_research", "web_crawl"):
        return {
            "operation": "network.fetch",
            # FM3-F4: the full URL/query is bound and digested; the card
            # truncates for display with an explicit hidden-count marker.
            "network": [str(args.get("url", args.get("query", "")))],
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
    from machina.tools.base import registry

    tool = registry._tools.get(name)
    return tuple(getattr(tool, "effects", None) or ())


_MUTATING_EFFECTS = frozenset({"fs.write", "exec", "net", "delegate"})
