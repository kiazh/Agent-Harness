"""FM5: redaction formats (RF7-F1), fail-closed redaction (W8),
empty-embedding rejection (N13), and profile create-or-fetch (N8).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.memory.redaction import redact_secrets

# --- RF7-F1: new secret formats ---------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "Ab1" * 8,
        "sk-svcacct-" + "Cd2" * 8,
        "sk-admin-" + "Ef3" * 8,
        "AIza" + "SyD-" * 8,
        "sk_live_" + "a1B2c3D4e5F6g7H8i9J0",
    ],
)
def test_new_secret_formats_are_redacted(secret):
    text = f"configured value: {secret} end"
    out = redact_secrets(text).text
    assert secret not in out
    assert "[REDACTED" in out


@pytest.mark.parametrize(
    "template",
    [
        'api_key = "abcd1234efgh5678"',
        "API_KEY: 'abcd1234efgh5678'",
        'openai_key="abcd1234efgh5678"',
        'secret-key: "abcd1234efgh5678"',
        '{"service_key": "abcd1234efgh5678"}',
    ],
)
def test_generic_quoted_key_values_are_redacted(template):
    out = redact_secrets(template).text
    assert "abcd1234efgh5678" not in out


def test_plain_hex_digest_is_not_an_aws_secret():
    """Tightened bare heuristic: a 40-char lowercase hex digest is not a secret."""
    digest = "0123456789abcdef0123456789abcdef01234567"
    assert digest in redact_secrets(f"commit {digest} merged").text


def test_mixed_bare_40_char_secret_is_still_redacted():
    secret = "aB3dE5fG7hI9jK1lM2nO4pQ6rS8tU0vWxYz01234"
    assert secret not in redact_secrets(f"aws {secret}").text


def test_ordinary_monkey_assignment_is_not_a_key_match():
    text = 'monkey = "bananasbananas"'
    assert text == redact_secrets(text).text


@pytest.mark.parametrize(
    "template",
    [
        '{"api_key": "abcd1234efgh5678"}',
        'service_key: "value with spaces 12345"',
        "vault-key: 'x/y+z=wq8'",
        'Key="12345678"',
        'openai_key="sk-live-abc!@#def"',
        "sk-proj-" + "A1b2" * 6,
        "AIza" + "SyD-" * 8,
        "sk_live_" + "a1B2c3D4e5F6g7H8i9J0",
    ],
)
def test_streaming_equals_whole_for_new_formats(template):
    """Boundary safety: split streaming must equal whole-string redaction."""
    import machina.memory.redaction as redaction_mod

    whole = redaction_mod.redact_secrets(template).text
    for split in range(len(template) + 1):
        redactor = redaction_mod.StreamingSecretRedactor()
        out = redactor.feed(template[:split]) + redactor.feed(template[split:])
        out += redactor.flush()
        assert out == whole, f"split {split}: {out!r} != {whole!r}"
    redactor = redaction_mod.StreamingSecretRedactor()
    char_out = "".join(redactor.feed(char) for char in template) + redactor.flush()
    assert char_out == whole


# --- W8: fail-closed redaction ----------------------------------------------


def test_pipeline_redact_or_failed_returns_marker_never_raw():
    import machina.rag.pipeline as pipeline_mod

    secret = "sk-proj-" + "Zq9" * 8
    with patch.object(pipeline_mod, "redact_secrets", side_effect=RuntimeError("detector down")):
        out = pipeline_mod._redact_or_failed(secret)
    assert secret not in out
    assert out == pipeline_mod.REDACTION_FAILED


async def test_index_document_fail_closed_never_stores_raw():
    import machina.rag.pipeline as pipeline_mod
    from machina.rag.loaders import Document

    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    mock_db.execute = AsyncMock(return_value="DELETE 0")
    from tests.support.database import attach_connection

    attach_connection(mock_db)
    secret = "sk-proj-" + "Qq8" * 8
    pipe = pipeline_mod.RAGPipeline(
        config=pipeline_mod.RAGConfig(enable_embeddings=False, enable_reranking=False)
    )
    doc = Document(content=f"token {secret}", source="notes.txt", doc_type="text")
    with (
        patch.object(pipeline_mod, "db", mock_db),
        patch.object(pipeline_mod, "redact_secrets", side_effect=RuntimeError("detector down")),
    ):
        await pipe.index_document(doc, uuid.uuid4())
    args = mock_db.fetch.call_args[0]
    payloads = args[4]
    search_texts = args[7]
    assert all(secret not in str(p) for p in payloads)
    assert all(secret not in str(t) for t in search_texts)
    assert pipeline_mod.REDACTION_FAILED in str(search_texts[0])


def test_user_profile_clean_text_fails_closed():
    import machina.memory.user_profile as profile_mod

    with patch.object(profile_mod, "redact_secrets", side_effect=RuntimeError("down")):
        out = profile_mod._clean_text("sk-proj-" + "Aa1" * 8)
    assert out == profile_mod.REDACTION_FAILED


def test_user_profile_clean_preferences_fails_closed():
    import machina.memory.user_profile as profile_mod

    broken = MagicMock()
    broken._redact_value = MagicMock(side_effect=RuntimeError("down"))
    with patch.object(profile_mod, "_default_redactor", broken):
        out = profile_mod._clean_preferences({"token": "sk-proj-" + "Aa1" * 8})
    assert out == {"_redaction_failed": profile_mod.REDACTION_FAILED}


def test_user_profile_set_preference_fails_closed():
    import machina.memory.user_profile as profile_mod

    broken = MagicMock()
    broken._redact_value = MagicMock(side_effect=RuntimeError("down"))
    profile = profile_mod.UserProfile(id=uuid.uuid4(), user_id="u")
    with patch.object(profile_mod, "_default_redactor", broken):
        profile.set_preference("token", "sk-proj-" + "Aa1" * 8)
    assert profile.preferences["token"] == profile_mod.REDACTION_FAILED


# --- N13: empty embedding rejection -----------------------------------------


async def test_approval_submit_empty_embedding_is_stored_as_none():
    from machina.memory.approval import MemoryApprovalGate

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value="INSERT 0 1")
    gate = MemoryApprovalGate(enabled=True)
    with patch("machina.memory.approval.db", mock_db):
        await gate.submit(content="fact", category="fact", embedding=[])
    stored = mock_db.execute.await_args.args[-1]
    assert stored is None


# --- N8: get_or_create create-or-fetch --------------------------------------


def _profile_row(user_id="race-user"):
    return {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "display_name": "",
        "preferences": "{}",
        "interaction_count": 0,
        "topics": "{}",
        "last_topics": "[]",
        "created_at": MagicMock(),
        "updated_at": MagicMock(),
    }


async def test_get_or_create_returns_existing_without_insert():
    from machina.memory.user_profile import UserProfileStore

    mock_db = AsyncMock()
    row = _profile_row()
    mock_db.fetchrow = AsyncMock(return_value=row)
    store = UserProfileStore()
    with patch("machina.memory.user_profile.db", mock_db):
        profile = await store.get_or_create("race-user")
    assert profile.id == row["id"]
    assert mock_db.fetchrow.await_count == 1


async def test_get_or_create_on_conflict_returns_winner():
    from machina.memory.user_profile import UserProfileStore

    mock_db = AsyncMock()
    winner = _profile_row()
    # 1) pre-check SELECT misses, 2) INSERT ... ON CONFLICT DO NOTHING
    # returns no row, 3) re-fetch returns the concurrent winner.
    mock_db.fetchrow = AsyncMock(side_effect=[None, None, winner])
    store = UserProfileStore()
    with patch("machina.memory.user_profile.db", mock_db):
        profile = await store.get_or_create("race-user")
    assert profile.id == winner["id"]
    insert_query = mock_db.fetchrow.await_args_list[1].args[0]
    assert "ON CONFLICT" in insert_query
    assert json.dumps({}) in mock_db.fetchrow.await_args_list[1].args
