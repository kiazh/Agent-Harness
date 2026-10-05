"""Config, profile, and status feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah import services
from ah.core.config import SECRET_KEYS
from ah.gateway.features._common import _config_snapshot, _int, _profile, _str

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


async def config_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Get current configuration."""
    gw.require_db()
    snapshot = _config_snapshot()
    snapshot["model"], snapshot["provider"] = gw.model, gw.provider
    return {"config": snapshot, "secrets": sorted(SECRET_KEYS)}


async def profile_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Get a user profile."""
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profile = await user_profile_store.get_or_create(
        user_id=_str(params, "userId", max_len=200),
        display_name=_str(params, "displayName", required=False, max_len=200),
    )
    return {"profile": _profile(profile)}


async def profile_set(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Set a user preference."""
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profile = await user_profile_store.get_or_create(user_id=_str(params, "userId", max_len=200))
    profile.set_preference(_str(params, "key", max_len=100), _str(params, "value", max_len=1000))
    updated = await user_profile_store.update_preferences(profile.id, profile.preferences)
    return {"profile": _profile(updated)}


async def profile_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List all user profiles."""
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profiles = await user_profile_store.list_all(limit=_int(params, "limit", 50, 1, 500))
    return {"profiles": [_profile(p) for p in profiles]}


async def status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Get system status."""
    gw.require_db()
    summary = await services.status_summary()
    summary["model"], summary["provider"] = gw.model, gw.provider
    return summary
