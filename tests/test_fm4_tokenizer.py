"""FM4 m2: literal special-token spellings must not crash the tokenizer.

RED before fix: tiktoken's ``encode`` raises ValueError on text containing
``<|endoftext|>`` and similar disallowed special tokens; TokenCounter.count
and TokenCounter.truncate propagated it, crashing prompt assembly and
compression for legitimate user text.
"""

from __future__ import annotations

import uuid

from machina.core.assembler import (
    PromptAssembler,
    TokenCounter,
    get_token_count,
    truncate_to_tokens,
)
from machina.core.models import ContextChunk

SPECIAL = "prefix <|endoftext|> suffix"
LONG_SPECIAL = "hello <|endoftext|> " * 40


def test_count_special_token_literal_falls_back():
    counter = TokenCounter()
    assert counter.count(SPECIAL) > 0
    assert get_token_count(SPECIAL) > 0


def test_truncate_special_token_literal_falls_back():
    counter = TokenCounter()
    out = counter.truncate(LONG_SPECIAL, 10)
    assert isinstance(out, str)
    assert out
    assert not out.startswith(LONG_SPECIAL), "must actually truncate"
    assert truncate_to_tokens(LONG_SPECIAL, 10)


def test_count_special_never_raises_for_any_special_word():
    for text in ("<|endoftext|>", "<|fim_prefix|>x", "a<|endofprompt|>b"):
        assert get_token_count(text) >= 0
        assert isinstance(truncate_to_tokens(text, 2), str)


def test_prompt_assembler_special_token_literal_does_not_raise():
    assembler = PromptAssembler(session_budget=200)
    text = assembler.assemble(
        "system prompt with <|endoftext|>",
        None,
        [
            {
                "type": "user_message",
                "payload": {"content": SPECIAL},
                "tokens": 5,
            }
        ],
        [],
        SPECIAL,
    )
    assert isinstance(text, str) and text


def test_compression_truncate_special_token_literal():
    from machina.core.compression import ContextCompressor

    compressor = ContextCompressor()
    chunk = ContextChunk(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_id="harness",
        chunk_type="result",
        payload={"content": LONG_SPECIAL},
        token_count=100,
    )
    truncated = compressor._truncate_chunk(chunk, 50)
    assert truncated is not None
    assert truncated.token_count <= 50
