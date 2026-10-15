"""Evidence pin — Copilot's default must be a model the account still serves.

Discovered 2026-10-15 from the installed Hermes agent's live-fetched caches
(read-only):

* ``~/.hermes/provider_models_cache.json`` -> key ``"copilot"`` -> ``"models"``
  holds the ids returned by the live ``GET https://api.githubcopilot.com/models``
  for this GitHub account (Hermes ``_copilot_catalog`` -> ``_fetch_github_models``;
  disk cache TTL 1h, stale-while-revalidate 7d). ``gpt-4o`` survives only in the
  trailing legacy block; ``gpt-6.1-sol`` heads the current GPT-6 generation, so
  it is the newest stable, non-preview family the cache lists.

Corroboration — not a dependency of this test:

* Machina's own ChatGPT-account snapshot (``provider/catalog.py``
  ``VERIFIED_FALLBACK_MODELS``, verified live 2026-10-14) lists ``gpt-6.1-sol``
  first, and ``provider/chatgpt.py`` already defaults to it.
* ``opencode models`` prints a flat ``provider/model`` catalogue but carries no
  Copilot rows, so it corroborates the catalogue shape only.
* The named Hermes caches carry no Copilot context length, so no
  ``context_windows`` entry was added for ``gpt-6.1-sol`` (see
  ``docs/verification/proofs/extensions_2026-10-15/CAT_model_catalogues.md``).

RED before the fix: ``CopilotProvider.DEFAULT_MODEL`` was ``"gpt-4o"``.
"""

from machina.core.provider import get_provider
from machina.core.provider.copilot import CopilotProvider

#: The id chosen on 2026-10-15 (newest stable, non-preview family in the cache).
COPILOT_DEFAULT_MODEL_DISCOVERED_2026_10_15 = "gpt-6.1-sol"

#: The account's current menu block from
#: ``~/.hermes/provider_models_cache.json ["copilot"]["models"]`` (2026-10-15),
#: in recorded order, ahead of the trailing legacy ids (gpt-5-mini, gpt-4o, ...).
COPILOT_CURRENT_IDS_2026_10_15: tuple[str, ...] = (
    "claude-fable-5.1",
    "claude-fable-5",
    "claude-haiku-5.5",
    "claude-opus-4.8-fast",
    "claude-opus-4.8",
    "claude-opus-5.5",
    "claude-opus-5",
    "claude-sonnet-5.5",
    "claude-sonnet-5",
    "copilot-search-a",
    "copilot-search-b",
    "copilot-search-c",
    "exec-agent-a",
    "exec-agent-b",
    "exec-agent-c",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gpt-5.4-mini-free-auto",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-luna",
    "gpt-6-sol",
    "kimi-k3-base",
    "kimi-k3-copilot",
    "kimi-k3",
    "mai-code-1.1-flash",
    "mai-code-1-flash-4th",
    "trajectory-compaction",
)


def test_copilot_default_model_is_current():
    assert CopilotProvider.DEFAULT_MODEL == COPILOT_DEFAULT_MODEL_DISCOVERED_2026_10_15


def test_copilot_default_is_served_by_the_recorded_catalog():
    assert CopilotProvider.DEFAULT_MODEL in COPILOT_CURRENT_IDS_2026_10_15
    # The stale id is gone from the current menu block; it survives only as a tail id.
    assert "gpt-4o" not in COPILOT_CURRENT_IDS_2026_10_15


def test_copilot_factory_without_a_model_uses_the_new_default():
    assert get_provider("copilot").model == COPILOT_DEFAULT_MODEL_DISCOVERED_2026_10_15
