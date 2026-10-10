"""Authorization and backends share a frozen workspace, mode, and backend."""

import pytest

from ah.core.config import config
from ah.permissions.broker import clear_execution_context, permission_broker
from ah.permissions.policy import build_request


async def test_approved_file_root_cannot_change_before_execution(tmp_path, monkeypatch):
    from ah.tools import file

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(first))
    request = build_request(
        operation="file.write", targets=["result.txt"], content="reviewed", mode="workspace"
    )
    try:
        await permission_broker.guard(request)
        monkeypatch.setattr(config, "workspace_root", str(second))
        assert file.resolve_path("result.txt") == first / "result.txt"
        await file.write_file("result.txt", "reviewed")
        assert (first / "result.txt").read_text() == "reviewed"
        assert not (second / "result.txt").exists()
    finally:
        clear_execution_context()


def test_absolute_action_binds_workspace_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "workspace_root", str(tmp_path / "first"))
    first = build_request(
        operation="file.write",
        targets=[str(tmp_path / "result")],
        cwd=str(tmp_path),
        content="value",
        mode="ask",
    )
    monkeypatch.setattr(config, "workspace_root", str(tmp_path / "second"))
    second = build_request(
        operation="file.write",
        targets=[str(tmp_path / "result")],
        cwd=str(tmp_path),
        content="value",
        mode="ask",
    )
    assert first.digest != second.digest


def test_file_and_terminal_share_approved_mode(monkeypatch):
    from ah.permissions.broker import _execution_context
    from ah.tools import file, terminal

    token = _execution_context.set({"mode": "sandbox", "backend": "sandbox", "workspace_root": "."})
    try:
        monkeypatch.setattr(config, "execution_mode", "full")
        assert terminal._call_mode() == "sandbox"
        assert not file._full_mode()
    finally:
        _execution_context.reset(token)
