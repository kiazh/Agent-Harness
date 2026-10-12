"""User images reach providers as bounded image blocks, never base64 prompt text."""

import base64

import pytest

from ah.core.provider import AnthropicProvider
from ah.core.usage import _estimate_tokens

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def attachment():
    return {"mediaType": "image/png", "data": base64.b64encode(PNG).decode(), "name": "test.png"}


def test_image_validation_and_openai_blocks():
    from ah.core.images import image_content, validate_images

    images = validate_images([attachment()])
    content = image_content("Describe [image 1]", images)
    assert content[0] == {"type": "text", "text": "Describe [image 1]"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "bad",
    [
        {},
        "bad",
        [{"mediaType": "image/svg+xml", "data": "AA=="}],
        [{"mediaType": "image/png", "data": "bad"}],
        [{"mediaType": "image/png", "data": base64.b64encode(b"not an image").decode()}],
        [attachment()] * 6,
    ],
)
def test_bad_images_fail_before_dispatch(bad):
    from ah.core.images import validate_images

    with pytest.raises(ValueError):
        validate_images(bad)


def test_anthropic_receives_image_source_instead_of_stringified_blocks():
    content = [
        {"type": "text", "text": "describe"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + attachment()["data"]},
        },
    ]
    _, messages = AnthropicProvider._to_anthropic_messages([{"role": "user", "content": content}])
    assert messages[0]["content"] == [
        {"type": "text", "text": "describe"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": attachment()["data"]},
        },
    ]


def test_image_usage_estimate_counts_visual_tokens_instead_of_base64_characters():
    url = "data:image/png;base64," + ("abcd" * 500_000)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    assert 1000 < _estimate_tokens(messages, [], 0) < 20_000


def test_ollama_image_payload_retains_text_and_decoded_image_data():
    from ah.core.images import ollama_messages

    url = "data:image/png;base64," + attachment()["data"]
    out = ollama_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    )
    assert out == [{"role": "user", "content": "describe", "images": [attachment()["data"]]}]
