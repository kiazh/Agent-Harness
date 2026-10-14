"""FM3-F7/F9/F11: request-bound roots, sensitive dotfiles, session-cap semantics."""

from __future__ import annotations

import pytest

from machina.core.config import config
from machina.permissions.policy import (
    ActionRequest,
    build_request,
    decide,
    normalize_request,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "live"
    root.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(root))
    monkeypatch.setattr(config, "execution_mode", "ask")
    return root


# ─── F7: _inside_workspace uses the request-bound root ─────────────────────


def test_inside_workspace_uses_request_bound_root(tmp_path, workspace):
    bound = tmp_path / "bound"
    (bound / "sub").mkdir(parents=True)
    target = bound / "sub" / "notes.txt"
    req = normalize_request(
        ActionRequest(
            operation="file.read",
            targets=[str(target)],
            mode="ask",
            workspace_root=str(bound),
        )
    )
    assert decide(req).verdict == "allowed"

    # A target under the LIVE global root but outside the request-bound root
    # is not covered by request-bound containment.
    outside = normalize_request(
        ActionRequest(
            operation="file.read",
            targets=[str(workspace / "x.txt")],
            mode="ask",
            workspace_root=str(bound),
        )
    )
    assert decide(outside).verdict == "pending"


# ─── F9: extended sensitive-dotfile coverage ───────────────────────────────


@pytest.mark.parametrize(
    "path",
    [".envrc", ".git-credentials", ".netrc", "prod.env", "config/service.env", ".ENV"],
)
def test_additional_sensitive_dotfiles_require_review(workspace, path):
    req = build_request(operation="file.read", targets=[path], mode="ask")
    assert decide(req).verdict == "pending", path


def test_env_example_stays_exempt(workspace):
    req = build_request(operation="file.read", targets=[".env.example"], mode="ask")
    assert decide(req).verdict == "allowed"


def test_inside_workspace_records_containment_failure(monkeypatch):
    """A path-resolution failure fails closed and reports the boundary."""
    from pathlib import Path

    from machina.permissions import policy

    recorded = []
    monkeypatch.setattr(policy, "record_failure", lambda op, e: recorded.append((op, e)))

    class BrokenPath(Path):
        def resolve(self, strict=False):
            raise OSError("resolve boom")

    monkeypatch.setattr(policy, "Path", BrokenPath)
    assert policy._inside_workspace("x", Path("/tmp")) is False
    assert [op for op, _ in recorded] == ["policy.inside_workspace"]


# ─── F11: session-capability grants need live-full-grant semantics ─────────


def _scope_less_grant(**overrides):
    base = {
        "id": "g1",
        "session_id": "s",
        "agent_id": "h",
        "mode": "ask",
        "capability": "session",
        "scope_path": None,
        "scope_type": "file",
        "grant_kind": "session",
        "digest": "not-the-request-digest",
        "revoked": False,
        "expires_at": None,
    }
    base.update(overrides)
    return base


def test_scope_less_session_grant_needs_full_semantics_for_reads(workspace):
    target = str(workspace.parent / "outside.txt")
    req = build_request(
        operation="file.read", targets=[target], mode="ask", session_id="s", agent_id="h"
    )
    assert decide(req, [_scope_less_grant()]).verdict == "pending"
    full = _scope_less_grant(mode="full", digest="full-mode-session")
    assert decide(req, [full]).verdict == "allowed"


def test_scoped_operation_grant_still_covers_non_mutating(workspace):
    target = str(workspace.parent / "shared" / "notes.txt")
    req = build_request(
        operation="file.read", targets=[target], mode="ask", session_id="s", agent_id="h"
    )
    grant = _scope_less_grant(capability="file.read", scope_path=target)
    assert decide(req, [grant]).verdict == "allowed"
