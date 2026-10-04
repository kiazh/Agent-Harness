"""Gateway feature handlers — split into domain modules.

This package re-exports all feature methods from their domain modules
and provides the ``register`` function that wires them into a Gateway.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ah.gateway.features._common import (
    MEMORY_CATEGORIES,
    coerce_config_value,
)
from ah.gateway.features.agents import (
    agents_delete,
    agents_history,
    agents_list,
    agents_run,
    agents_save,
)
from ah.gateway.features.config import (
    config_get,
    profile_get,
    profile_list,
    profile_set,
    status,
)
from ah.gateway.features.jobs import (
    jobs_create,
    jobs_delete,
    jobs_list,
    jobs_set_enabled,
)
from ah.gateway.features.memory import (
    memory_add,
    memory_approve,
    memory_approve_all,
    memory_forget,
    memory_list,
    memory_pending,
    memory_reject,
    memory_reject_all,
    memory_search,
    memory_stats,
)
from ah.gateway.features.sessions import (
    context_compress,
    context_get,
    session_delete,
    session_export,
    session_fork,
    session_rename,
    session_search,
    session_set_goal,
)
from ah.gateway.features.skills import (
    skills_curator,
    skills_delete,
    skills_learn,
    skills_list,
    skills_show,
)

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

__all__ = [
    "METHODS",
    "register",
    "coerce_config_value",
    "MEMORY_CATEGORIES",
    "Handler",
]

Handler = Callable[["Gateway", dict[str, Any]], Awaitable[dict[str, Any]]]

METHODS: dict[str, Handler] = {
    "session.fork": session_fork,
    "session.delete": session_delete,
    "session.rename": session_rename,
    "session.setGoal": session_set_goal,
    "session.search": session_search,
    "session.export": session_export,
    "context.get": context_get,
    "context.compress": context_compress,
    "memory.list": memory_list,
    "memory.search": memory_search,
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
    "config.get": config_get,
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
    "jobs.delete": jobs_delete,
}


def register(gateway: Gateway) -> None:
    """Add every feature method to *gateway*'s dispatch table."""
    for name, handler in METHODS.items():
        gateway.add_method(name, lambda params, h=handler: h(gateway, params))
