"""S5.1 STRIDE-lite regression tests (info-disclosure boundary).

The audit found no CRITICAL/HIGH items (see
docs/verification/OAUTH_RISK_REGISTER.md). These tests lock the two
load-bearing info-disclosure properties that previously had no direct
regression coverage: secret material must never appear in object reprs
(logs, tracebacks, diagnostics) nor in the public identity views served to
the control plane.
"""

from __future__ import annotations


def test_secret_material_excluded_from_reprs():
    from machina.connectors.oauth_models import OAuthCallbackResult, OAuthCredentialRecord

    record = OAuthCredentialRecord(
        credential_key="k",
        connector_id="c",
        access_token="access-SENSITIVE",
        refresh_token="refresh-SENSITIVE",
    )
    blob = repr(record)
    assert "access-SENSITIVE" not in blob
    assert "refresh-SENSITIVE" not in blob

    result = OAuthCallbackResult(ok=True, code="code-SENSITIVE", state="state-SENSITIVE")
    blob = repr(result)
    assert "code-SENSITIVE" not in blob
    assert "state-SENSITIVE" not in blob


async def test_public_identity_exposes_no_token_material(tmp_path):
    from machina.connectors.oauth_models import OAuthBinding, OAuthCredentialRecord
    from machina.connectors.oauth_store import OAuthCredentialStore

    binding = OAuthBinding(
        connector_id="c", credential_key="a" * 64, resource="r", issuer="i", client_id="cl"
    )
    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    await store.save(
        OAuthCredentialRecord(
            credential_key=binding.credential_key,
            connector_id="c",
            resource="r",
            issuer="i",
            client_id="cl",
            generation=1,
            grant_id="g",
            access_token="access-SENSITIVE",
            refresh_token="refresh-SENSITIVE",
        ),
        expected_generation=0,
    )
    import json

    identity = await store.public_identity(binding)
    assert identity is not None
    blob = json.dumps(identity)
    assert "access-SENSITIVE" not in blob
    assert "refresh-SENSITIVE" not in blob
    assert set(identity) == {
        "credentialKey",
        "grantId",
        "generation",
        "resource",
        "issuer",
        "clientId",
        "grantedScopes",
    }
