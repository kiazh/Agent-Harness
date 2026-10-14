"""LIVE proof: real ChatGPT/Codex catalog through Machina's own surfaces.

Runs against this machine's working ChatGPT auth (no tokens are printed or
written): the catalog module's live fetch, per-model effort validation, the
gateway ``models.list`` handler (what the TUI /models surface calls), and the
``models.setEffort`` validation path. Output: this directory's console log
plus ``catalog_normalized_live.json``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

OUT = Path(__file__).resolve().parent


def main() -> int:
    from machina.core.provider import catalog
    from machina.gateway.features import providers_feature

    report: dict = {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # 1. Module-level live discovery (newest client_version first).
    models = catalog.fetch_catalog_for_account(force=True)
    visible = catalog.visible_models(models)
    report["live"] = {
        "entries": len(models),
        "visible": len(visible),
        "models": [model.to_dict() for model in visible],
    }
    print(
        f"LIVE catalog via machina.core.provider.catalog: {len(models)} entries, {len(visible)} visible"
    )
    for model in visible:
        efforts = "/".join(model.supported_efforts)
        print(
            f"  {model.slug:<14} efforts={efforts:<28} "
            f"ctx={model.context_window} max={model.max_context_window} "
            f"default={model.default_effort or '-'}"
        )

    # 2. Per-model effort validation from each entry.
    sol = next(m for m in visible if m.slug == "gpt-6.1-sol")
    accepted = catalog.validate_effort(sol.slug, "xhigh")
    rejected = None
    try:
        catalog.validate_effort(sol.slug, "ultra")
    except Exception as exc:
        rejected = f"{type(exc).__name__}: {exc}"
    report["effort_validation"] = {"accepted_xhigh": accepted, "ultra_rejected": rejected}
    print(f"effort validation: xhigh -> {accepted!r}; ultra -> {rejected}")

    # 3. Gateway models.list (the TUI /models data path), live refresh.
    gw = SimpleNamespace(require_db=lambda: None, provider="chatgpt", model="gpt-6.1-sol")
    payload = asyncio.run(providers_feature.models_list(gw, {"refresh": True}))
    report["gateway_models_list"] = {
        "source": payload["source"],
        "fetchError": payload["fetchError"],
        "model_count": len(payload["models"]),
        "models": payload["models"],
    }
    print(
        f"gateway models.list: source={payload['source']} error={payload['fetchError']} "
        f"models={len(payload['models'])}"
    )

    # 4. models.setEffort through the handler: xhigh applies, ultra is typed.
    applied = asyncio.run(providers_feature.models_set_effort(gw, {"effort": "xhigh"}))
    print(f"gateway models.setEffort xhigh: {applied}")
    ultra_error = None
    try:
        asyncio.run(providers_feature.models_set_effort(gw, {"effort": "ultra"}))
    except Exception as exc:
        ultra_error = str(exc)
    report["gateway_set_effort"] = {"applied": applied, "ultra_error": ultra_error}
    print(f"gateway models.setEffort ultra -> {ultra_error}")
    asyncio.run(providers_feature.models_set_effort(gw, {"effort": "off"}))

    (OUT / "catalog_normalized_live.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT / 'catalog_normalized_live.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
