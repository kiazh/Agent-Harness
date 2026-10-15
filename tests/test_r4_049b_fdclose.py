"""R4-049b — chmod-failure fd-close helper (remaining sites).

Adversarial proof: force ``os.chmod`` to fail (fault injection) while the
real store writers run against real files (real flock where the API takes
one). Pre-fix, the mkstemp fd leaks: ``os.chmod(tmp)`` raises before
``os.fdopen`` owns the descriptor and the shared ``except`` only unlinks
the temp name. Post-fix, the shared ``store_fence.chmod_temp_file`` helper
closes the descriptor and the error still propagates with the temp removed.

The seam is never mocked: the tests call the real ``save_tokens`` /
``invalidate_credentials`` / ``save_models`` / ``publish_device_token``.
Only the ``os.chmod`` syscall is fault-injected and ``tempfile.mkstemp``
is spied on (stdlib instrumentation, to learn the fd under test).
"""

import os
import tempfile

import pytest

from machina.security import store_fence

# ---------------------------------------------------------------------------
# Fault-injection harness (stdlib only — never the store seam)
# ---------------------------------------------------------------------------


class _ChmodFault:
    """Make ``os.chmod`` raise; record mkstemp fds so tests can probe them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[int] = []
        self._real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):  # type: ignore[no-untyped-def]
            fd, name = self._real_mkstemp(*args, **kwargs)
            self.opened.append(fd)
            return fd, name

        def boom_chmod(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError(13, "R4-049b injected chmod failure")

        monkeypatch.setattr(tempfile, "mkstemp", spy_mkstemp)
        monkeypatch.setattr(os, "chmod", boom_chmod)

    def assert_all_closed(self) -> None:
        assert self.opened, "harness never saw an mkstemp fd (seam not reached?)"
        for fd in self.opened:
            try:
                os.fstat(fd)
            except OSError:
                continue  # EBADF — closed, as required
            raise AssertionError(f"mkstemp fd {fd} leaked: still open after chmod failure")

    def assert_no_tmps(self, directory: os.PathLike[str] | str) -> None:
        leftovers = [p for p in os.listdir(directory) if p.endswith(".tmp")]
        assert leftovers == [], f"temp files left behind: {leftovers}"


@pytest.fixture
def chmod_fault(monkeypatch: pytest.MonkeyPatch) -> _ChmodFault:
    return _ChmodFault(monkeypatch)


# ---------------------------------------------------------------------------
# chatgpt_auth — save_tokens (real file; real flock held by the caller)
# ---------------------------------------------------------------------------


def test_chatgpt_save_tokens_chmod_failure_closes_fd(
    tmp_path, monkeypatch: pytest.MonkeyPatch, chmod_fault: _ChmodFault
) -> None:
    from machina.security import chatgpt_auth

    target = tmp_path / "chatgpt.json"
    monkeypatch.setenv(chatgpt_auth.AUTH_FILE_ENV, str(target))
    tokens = {"access_token": "a" * 32, "refresh_token": "r" * 32}
    with pytest.raises(OSError, match="injected chmod failure"):
        with chatgpt_auth._store_lock(target):
            chatgpt_auth.save_tokens(tokens, target)
    chmod_fault.assert_all_closed()
    chmod_fault.assert_no_tmps(tmp_path)
    assert not target.exists()


def test_chatgpt_invalidate_credentials_chmod_failure_closes_fd(
    tmp_path, monkeypatch: pytest.MonkeyPatch, chmod_fault: _ChmodFault
) -> None:
    from machina.security import chatgpt_auth

    target = tmp_path / "chatgpt.json"
    monkeypatch.setenv(chatgpt_auth.AUTH_FILE_ENV, str(target))
    # NOTE: invalidate_credentials fail-softs a lost durable counter into the
    # in-process generation (by design — logout must never fail), so the chmod
    # fault does NOT propagate here. The observable is fd hygiene only.
    generation = chatgpt_auth.invalidate_credentials(target)
    assert isinstance(generation, int)
    chmod_fault.assert_all_closed()
    chmod_fault.assert_no_tmps(tmp_path)


# ---------------------------------------------------------------------------
# copilot_auth — save_tokens via the real fenced publish path (real flock)
# ---------------------------------------------------------------------------


def test_copilot_publish_device_token_chmod_failure_closes_fd(
    tmp_path, monkeypatch: pytest.MonkeyPatch, chmod_fault: _ChmodFault
) -> None:
    from machina.security import copilot_auth

    target = tmp_path / "copilot.json"
    monkeypatch.setenv(copilot_auth.AUTH_FILE_ENV, str(target))
    generation = copilot_auth.credential_generation(target)
    with pytest.raises(OSError, match="injected chmod failure"):
        copilot_auth.publish_device_token(
            "gho_" + "t" * 28, account="octocat", generation=generation, path=target
        )
    chmod_fault.assert_all_closed()
    chmod_fault.assert_no_tmps(tmp_path)
    assert not target.exists()


# ---------------------------------------------------------------------------
# providers — _save_store via the real locked API (real flock)
# ---------------------------------------------------------------------------


def test_providers_save_models_chmod_failure_closes_fd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from machina.security import providers

    store = tmp_path / "providers.json"
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(store))
    # Register first with a healthy chmod so the failing call is the save.
    providers.add_provider("r4test", "http://127.0.0.1:11434", api_key=None)
    assert store.exists()
    fault = _ChmodFault(monkeypatch)
    with pytest.raises(OSError, match="injected chmod failure"):
        providers.save_models("r4test", [{"id": "m1"}])
    fault.assert_all_closed()
    fault.assert_no_tmps(tmp_path)
    # The pre-existing store survives untouched (failed replace, no clobber).
    assert store.exists()


# ---------------------------------------------------------------------------
# Helper contract + success-path regression (helper passes chmod through)
# ---------------------------------------------------------------------------


def test_chmod_temp_file_helper_closes_fd_on_failure(tmp_path, monkeypatch) -> None:
    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(13, "injected")

    monkeypatch.setattr(os, "chmod", boom)
    fd, tmp_name = tempfile.mkstemp(dir=str(tmp_path))
    with pytest.raises(OSError, match="injected"):
        store_fence.chmod_temp_file(fd, tmp_name, 0o600)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_stores_still_land_0600_after_fix(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Success path: the helper is a pass-through, files stay 0600."""
    import stat

    from machina.security import chatgpt_auth, copilot_auth, providers

    chatgpt_target = tmp_path / "chatgpt.json"
    copilot_target = tmp_path / "copilot.json"
    store = tmp_path / "providers.json"
    monkeypatch.setenv(chatgpt_auth.AUTH_FILE_ENV, str(chatgpt_target))
    monkeypatch.setenv(copilot_auth.AUTH_FILE_ENV, str(copilot_target))
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(store))

    chatgpt_auth.save_tokens({"access_token": "a" * 32}, chatgpt_target)
    copilot_auth.save_tokens({"token": "gho_" + "t" * 28}, copilot_target)
    providers.add_provider("r4test", "http://127.0.0.1:11434", api_key=None)
    providers.save_models("r4test", [{"id": "m1"}])

    for path in (chatgpt_target, copilot_target, store):
        assert path.exists()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
