"""R5-LM (findings 16 + 17): credential publication/removal races.

H-RACE style: real files + real flock, N>=5 repeats each. RED-first: these
tests assert the FIXED behavior, so they must FAIL on the unfixed code for
the named reason (stale import wins / replacement credential deleted) and
pass after the fix.

  L1 (finding 16): ``import_from_gh_cli`` checks the store, then does slow
      subprocess/network work, then publishes unconditionally. A device
      login/logout landing mid-import is overwritten (overwrite) or the
      store is recreated after a logout (resurrection).
  L2 (finding 17): ``remove_provider`` releases the store lock before
      deleting the ``.env`` line + process env var. A concurrent re-add
      landing in that gap gets its fresh credential deleted.
"""

from __future__ import annotations

import threading
import time

import pytest

from machina.security import copilot_auth, providers
from machina.security.env_file import read_env_values

N_REPEATS = 5

STALE_TOKEN = "gho_stale_import_race"
FRESH_TOKEN = "gho_device_fresh_race"


def _run_import_with_blocked_probe(tmp_path, monkeypatch, mid_flight, *, force=False):
    """Run ``import_from_gh_cli`` with its probe blocked; run ``mid_flight``
    while it is in flight, then release the probe and join."""
    target = tmp_path / "copilot.json"
    started = threading.Event()
    release = threading.Event()

    def fake_probe():
        started.set()
        assert release.wait(timeout=15), "probe was never released"
        return STALE_TOKEN

    monkeypatch.setattr(copilot_auth, "_probe_gh_token", fake_probe)
    outcome: dict = {}

    def worker():
        try:
            outcome["tokens"] = copilot_auth.import_from_gh_cli(path=target, force=force)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name="r5-lm-gh-import")
    thread.start()
    try:
        assert started.wait(timeout=15), "import never reached the gh probe"
        mid_flight(target)
    finally:
        release.set()
    thread.join(timeout=20)
    assert not thread.is_alive(), "import worker hung"
    return target, outcome


@pytest.mark.parametrize("round", range(N_REPEATS))
def test_l1_stale_import_must_not_resurrect_after_logout(tmp_path, monkeypatch, round):
    """A logout racing a gh import must not be undone by the stale import."""
    _ = round
    store = tmp_path / f"r{round}" / "copilot.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    holder = tmp_path / f"r{round}"
    target, outcome = _run_import_with_blocked_probe(
        holder, monkeypatch, lambda t: copilot_auth.logout(path=t)
    )
    assert "error" not in outcome, f"import raised: {outcome.get('error')!r}"
    assert outcome.get("tokens") is None, (
        f"stale import published despite an intervening logout: {outcome.get('tokens')!r}"
    )
    assert copilot_auth.load_tokens(target) is None, (
        "logout was undone: the stale import recreated the removed store"
    )


@pytest.mark.parametrize("round", range(N_REPEATS))
def test_l1_stale_import_must_not_overwrite_device_login(tmp_path, monkeypatch, round):
    """A device login completing mid-import must survive the stale import."""
    _ = round
    holder = tmp_path / f"r{round}"
    holder.mkdir(parents=True, exist_ok=True)

    def device_login(target):
        generation = copilot_auth.credential_generation(target)
        published = copilot_auth.publish_device_token(
            FRESH_TOKEN, account=None, generation=generation, path=target
        )
        assert published["token"] == FRESH_TOKEN

    target, outcome = _run_import_with_blocked_probe(holder, monkeypatch, device_login)
    assert "error" not in outcome, f"import raised: {outcome.get('error')!r}"
    assert outcome.get("tokens") is None, (
        f"stale import overwrote a newer device login: {outcome.get('tokens')!r}"
    )
    final = copilot_auth.load_tokens(target)
    assert final is not None and final["token"] == FRESH_TOKEN, (
        f"device login was clobbered by the stale import: {final!r}"
    )


@pytest.mark.parametrize("round", range(N_REPEATS))
def test_l2_remove_must_not_delete_replacement_credential(tmp_path, monkeypatch, round):
    """A re-add racing a removal must keep its fresh credential."""
    workdir = tmp_path / f"r{round}"
    workdir.mkdir(parents=True, exist_ok=True)
    store = workdir / "providers.json"
    env_file = workdir / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(store))
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    env_name = "MACHINA_PROVIDER_RACEPROV_API_KEY"
    monkeypatch.delenv(env_name, raising=False)

    providers.add_provider("raceprov", "http://127.0.0.1:9", api_key="OLDKEY")
    assert env_name in read_env_values()

    entered = threading.Event()
    adder_may_start = threading.Event()
    original_remove = providers.remove_env_values

    def blocking_remove(names):
        entered.set()
        assert adder_may_start.wait(timeout=15), "adder was never started"
        # Widen the store-lock/env-cleanup gap so the re-add deterministically
        # lands inside it on unfixed code. On fixed code the adder blocks on
        # the held store lock instead, so no deadlock: we never wait for it.
        time.sleep(0.5)
        return original_remove(names)

    monkeypatch.setattr(providers, "remove_env_values", blocking_remove)

    remover_done: dict = {}
    adder_done: dict = {}

    def remover():
        try:
            remover_done["report"] = providers.remove_provider("raceprov")
        except Exception as exc:  # noqa: BLE001
            remover_done["error"] = exc

    def adder():
        try:
            providers.add_provider("raceprov", "http://127.0.0.1:9", api_key="NEWKEY")
            adder_done["ok"] = True
        except Exception as exc:  # noqa: BLE001
            adder_done["error"] = exc

    remover_thread = threading.Thread(target=remover, name="r5-lm-remover")
    remover_thread.start()
    try:
        assert entered.wait(timeout=15), "removal never reached .env cleanup"
        adder_thread = threading.Thread(target=adder, name="r5-lm-adder")
        adder_thread.start()
        adder_may_start.set()
        adder_thread.join(timeout=20)
        assert not adder_thread.is_alive(), "adder hung"
    finally:
        remover_thread.join(timeout=20)
    assert not remover_thread.is_alive(), "remover hung"
    assert "error" not in remover_done, f"remover raised: {remover_done['error']!r}"
    assert adder_done.get("ok"), f"adder failed: {adder_done.get('error')!r}"

    entry = providers.get_provider_entry("raceprov")
    assert entry is not None, "re-added provider went missing entirely"
    env_values = read_env_values()
    assert env_values.get(env_name) == "NEWKEY", (
        f"replacement credential was deleted by the racing removal: .env has "
        f"{env_values.get(env_name)!r}, entry={entry!r}"
    )
    import os

    assert os.environ.get(env_name) == "NEWKEY", (
        "replacement process-env credential was deleted by the racing removal"
    )
    assert providers.resolve_provider_key(entry) == "NEWKEY", (
        "secret resolver no longer sees the replacement credential"
    )
