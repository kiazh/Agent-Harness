"""Bounded, inline user images and provider-specific multimodal payloads."""

from __future__ import annotations

import base64
import binascii
from typing import Any

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
# Conservative context reservation. Actual vendor usage replaces estimates.
IMAGE_TOKEN_RESERVE = 8192


def image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_images(value: Any) -> list[dict[str, str]]:
    """Validate wire attachments without fetching paths or remote URLs."""
    if not isinstance(value, list) or len(value) > MAX_IMAGES:
        raise ValueError(f"images must be an array of at most {MAX_IMAGES} images")
    images: list[dict[str, str]] = []
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) - {"mediaType", "data", "name"}:
            raise ValueError("each image must contain mediaType, data and an optional name")
        media_type, encoded, name = (
            item.get("mediaType"),
            item.get("data"),
            item.get("name", "image"),
        )
        if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError("supported image formats: PNG, JPEG, GIF and WebP")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("image exceeds the 5 MiB attachment limit")
        if not isinstance(name, str) or len(name) > 255 or any(ord(c) < 32 for c in name):
            raise ValueError("invalid image name")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image data must be valid base64") from exc
        if not data or len(data) > MAX_IMAGE_BYTES or image_media_type(data) != media_type:
            raise ValueError("image data does not match its format or exceeds 5 MiB")
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("images exceed the 20 MiB total attachment limit")
        images.append({"mediaType": media_type, "data": encoded, "name": name})
    return images


def image_content(text: str, images: list[dict[str, str]]) -> str | list[dict[str, Any]]:
    if not images:
        return text
    return [{"type": "text", "text": text}] + [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image['mediaType']};base64,{image['data']}"},
        }
        for image in images
    ]


def token_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove image encodings from tokenization and reserve visual tokens."""
    out = []
    image_count = 0
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"image_url", "image"}:
                    image_count += 1
                    blocks.append({"type": "text", "text": "[image]"})
                else:
                    blocks.append(block)
            copied["content"] = blocks
        if copied.get("images"):
            image_count += len(copied.pop("images"))
        out.append(copied)
    return out, image_count * IMAGE_TOKEN_RESERVE


def anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return str(content or "")
    out = []
    for block in content:
        if block.get("type") == "image_url":
            url = block["image_url"]["url"]
            prefix, data = url.split(",", 1)
            if not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
                raise ValueError("Anthropic images must use inline base64 data")
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": prefix[5:-7],
                        "data": data,
                    },
                }
            )
        else:
            out.append(block)
    return out


def ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if isinstance(content, list):
            text, images = [], []
            for block in content:
                if block.get("type") == "text":
                    text.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    prefix, data = block["image_url"]["url"].split(",", 1)
                    if not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
                        raise ValueError("Ollama images must use inline base64 data")
                    images.append(data)
            copied["content"] = "\n".join(text)
            if images:
                copied["images"] = images
        out.append(copied)
    return out
