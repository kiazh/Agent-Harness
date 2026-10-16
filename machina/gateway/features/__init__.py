"""Gateway feature handlers — split into domain modules.

This package re-exports all feature methods from their domain modules
and provides the ``register`` function that wires them into a Gateway.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from machina.gateway.features._common import (
    MEMORY_CATEGORIES,
    coerce_config_value,
)
from machina.gateway.features.agents import (
    agents_delete,
    agents_history,
    agents_list,
    agents_run,
    agents_save,
)
from machina.gateway.features.auth_chatgpt import (
    auth_chatgpt_import_codex,
    auth_chatgpt_login,
    auth_chatgpt_logout,
    auth_chatgpt_status,
)
from machina.gateway.features.config import (
    config_get,
    profile_get,
    profile_list,
    profile_set,
    status,
)
from machina.gateway.features.connector_oauth import (
    mcp_oauth_cancel,
    mcp_oauth_forget,
    mcp_oauth_open,
    mcp_oauth_preview,
    mcp_oauth_start,
    mcp_oauth_status,
    mcp_oauth_url,
)
from machina.gateway.features.connectors import (
    mcp_configure,
    mcp_connect,
    mcp_disconnect,
    mcp_list,
    mcp_preview,
    mcp_remove,
    mcp_status,
    mcp_tools,
)
from machina.gateway.features.growth import (
    growth_feedback,
    growth_forget,
    growth_get,
    growth_install_policy,
    growth_rollback,
    growth_teach,
)
from machina.gateway.features.interop import (
    interop_disable,
    interop_enable,
    interop_list,
    interop_run,
    interop_status,
)
from machina.gateway.features.jobs import (
    jobs_create,
    jobs_delete,
    jobs_list,
    jobs_resume,
    jobs_set_enabled,
)
from machina.gateway.features.memory import (
    memory_add,
    memory_approve,
    memory_approve_all,
    memory_forget,
    memory_list,
    memory_pending,
    memory_reject,
    memory_reject_all,
    memory_search,
    memory_share,
    memory_stats,
)
from machina.gateway.features.mode import (
    approvals_list,
    approvals_resolve,
    mode_get,
    mode_revoke,
    mode_set,
)
from machina.gateway.features.providers_feature import (
    models_list,
    models_set_effort,
    providers_add,
    providers_list,
    providers_remove,
)
from machina.gateway.features.secrets import secrets_clear, secrets_list, secrets_set
from machina.gateway.features.sessions import (
    context_compress,
    context_get,
    session_delete,
    session_export,
    session_fork,
    session_recall,
    session_recall_window,
    session_rename,
    session_search,
    session_set_goal,
    session_set_model,
)
from machina.gateway.features.skills import (
    learning_approve,
    learning_list,
    learning_reject,
    learning_revert,
    skills_curator,
    skills_delete,
    skills_learn,
    skills_list,
    skills_show,
)
from machina.gateway.features.workspace import (
    workspace_close,
    workspace_document_change,
    workspace_document_close,
    workspace_document_open,
    workspace_intelligence,
    workspace_list,
    workspace_lsp_control,
    workspace_lsp_status,
    workspace_open,
    workspace_read,
    workspace_save,
)

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

__all__ = [
    "METHODS",
    "register",
    "coerce_config_value",
    "MEMORY_CATEGORIES",
    "Handler",
]

Handler = Callable[["Gateway", dict[str, Any]], Awaitable[dict[str, Any]]]

METHODS: dict[str, Handler] = {
    "auth.chatgpt.status": auth_chatgpt_status,
    "auth.chatgpt.login": auth_chatgpt_login,
    "auth.chatgpt.logout": auth_chatgpt_logout,
    "auth.chatgpt.importCodex": auth_chatgpt_import_codex,
    "mcp.list": mcp_list,
    "mcp.status": mcp_status,
    "mcp.configure": mcp_configure,
    "mcp.remove": mcp_remove,
    "mcp.preview": mcp_preview,
    "mcp.connect": mcp_connect,
    "mcp.disconnect": mcp_disconnect,
    "mcp.tools": mcp_tools,
    "mcp.oauth.preview": mcp_oauth_preview,
    "mcp.oauth.start": mcp_oauth_start,
    "mcp.oauth.status": mcp_oauth_status,
    "mcp.oauth.url": mcp_oauth_url,
    "mcp.oauth.open": mcp_oauth_open,
    "mcp.oauth.cancel": mcp_oauth_cancel,
    "mcp.oauth.forget": mcp_oauth_forget,
    "growth.get": growth_get,
    "growth.teach": growth_teach,
    "growth.feedback": growth_feedback,
    "growth.forget": growth_forget,
    "growth.installPolicy": growth_install_policy,
    "growth.rollback": growth_rollback,
    "interop.status": interop_status,
    "interop.enable": interop_enable,
    "interop.disable": interop_disable,
    "interop.list": interop_list,
    "interop.run": interop_run,
    "session.fork": session_fork,
    "session.delete": session_delete,
    "session.rename": session_rename,
    "session.setGoal": session_set_goal,
    "session.setModel": session_set_model,
    "session.search": session_search,
    "session.recall": session_recall,
    "session.recall.window": session_recall_window,
    "session.export": session_export,
    "context.get": context_get,
    "context.compress": context_compress,
    "memory.list": memory_list,
    "memory.search": memory_search,
    "memory.share": memory_share,
    "memory.add": memory_add,
    "memory.forget": memory_forget,
    "memory.pending": memory_pending,
    "memory.approve": memory_approve,
    "memory.reject": memory_reject,
    "memory.approveAll": memory_approve_all,
    "memory.rejectAll": memory_reject_all,
    "memory.stats": memory_stats,
    "skills.list": skills_list,
    "skills.show": skills_show,
    "skills.learn": skills_learn,
    "skills.delete": skills_delete,
    "skills.curator": skills_curator,
    "learning.list": learning_list,
    "learning.approve": learning_approve,
    "learning.reject": learning_reject,
    "learning.revert": learning_revert,
    "config.get": config_get,
    "secrets.list": secrets_list,
    "secrets.set": secrets_set,
    "secrets.clear": secrets_clear,
    "models.list": models_list,
    "models.setEffort": models_set_effort,
    "providers.list": providers_list,
    "providers.add": providers_add,
    "providers.remove": providers_remove,
    "profile.get": profile_get,
    "profile.set": profile_set,
    "profile.list": profile_list,
    "status": status,
    "agents.list": agents_list,
    "agents.save": agents_save,
    "agents.delete": agents_delete,
    "agents.run": agents_run,
    "agents.history": agents_history,
    "jobs.create": jobs_create,
    "jobs.list": jobs_list,
    "jobs.setEnabled": jobs_set_enabled,
    "jobs.resume": jobs_resume,
    "jobs.delete": jobs_delete,
    "mode.get": mode_get,
    "mode.set": mode_set,
    "mode.revoke": mode_revoke,
    "approvals.list": approvals_list,
    "approvals.resolve": approvals_resolve,
    "workspace.open": workspace_open,
    "workspace.list": workspace_list,
    "workspace.read": workspace_read,
    "workspace.save": workspace_save,
    "workspace.documentOpen": workspace_document_open,
    "workspace.documentChange": workspace_document_change,
    "workspace.documentClose": workspace_document_close,
    "workspace.intelligence": workspace_intelligence,
    "workspace.lspStatus": workspace_lsp_status,
    "workspace.lspControl": workspace_lsp_control,
    "workspace.close": workspace_close,
}


def register(gateway: Gateway) -> None:
    """Add every feature method to *gateway*'s dispatch table."""
    for name, handler in METHODS.items():
        gateway.add_method(name, lambda params, h=handler: h(gateway, params))
