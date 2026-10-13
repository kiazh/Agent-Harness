"""FM3-RF6-F4: credential generation must advance by exactly one (or rotate)."""

from __future__ import annotations

import pytest

from machina.connectors.oauth_store import OAuthCredentialStore
from tests.test_connectors_oauth_store import _binding, _key_provider, _record


@pytest.mark.asyncio
async def test_forward_generation_skips_are_rejected(tmp_path):
    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    binding = _binding()
    await store.save(_record(binding), expected_generation=0)
    for skipped in (3, 4, 99):
        with pytest.raises(ValueError):
            await store.save(_record(binding, generation=skipped), expected_generation=1)
    assert (await store.load(binding)).generation == 1


@pytest.mark.asyncio
async def test_same_generation_rotation_still_allowed(tmp_path):
    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    binding = _binding()
    await store.save(_record(binding), expected_generation=0)
    rotated = await store.save(
        _record(binding, access_token="rotated-value", refresh_token="refreshed-value"),
        expected_generation=1,
    )
    assert rotated.generation == 1
    assert (await store.load(binding)).access_token == "rotated-value"


@pytest.mark.asyncio
async def test_exact_next_generation_still_advances(tmp_path):
    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    binding = _binding()
    await store.save(_record(binding), expected_generation=0)
    advanced = await store.save(
        _record(binding, generation=2, grant_id="grant-2"), expected_generation=1
    )
    assert advanced.generation == 2
    assert (await store.load(binding)).grant_id == "grant-2"
