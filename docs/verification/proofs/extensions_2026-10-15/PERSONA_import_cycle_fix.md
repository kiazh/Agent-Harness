# persona_description import-cycle fix (2026-10-15)

## Incident

`from machina.core.provider.copilot import CopilotProvider` as the **first** machina
import in a fresh process logged:

```
Ignoring unreadable config persona_description in /Users/shadow/.machina/config.yaml: ImportError
ImportError
  machina/core/config.py:616 (load)
  machina/core/config.py:282 (validate_value)
  machina/memory/__init__.py:15 (<module>)
  machina/memory/approval.py:23 (<module>)
```

A user-configured `persona_description` was **silently dropped** (per-key
isolation keeps the rest of the file) whenever the process entry order made
the provider package the first heavily-imported package.

Recorded occurrences (unmodified): `docs/verification/proofs/extensions_2026-10-15/CAT_red_before_fix.log`,
`docs/verification/proofs/codebase_review_round5_2026-10-15/logs/R5-N*_2026-10-15.log`.

## Root cause chain

1. `provider/__init__.py:26` imports `.anthropic`; `anthropic.py:14`
   (`from machina.core.config import config`) resolves the lazy module
   attribute → `get_config()` → **`Config.load()` starts while
   `machina.core.provider` is only partially initialized** (`audit_log` is
   bound two `__init__` lines later, at line 27).
2. `Config.load` → `validate_value("persona_description")` →
   `from machina.memory.redaction import redact_secrets` → executing the
   **eager** `machina/memory/__init__.py` body → line 15 imports
   `machina.memory.approval`.
3. `approval.py:23` does `from machina.core.provider import audit_log` → the
   provider package is partial → `ImportError` → the loader logs
   `Ignoring unreadable config …` and the persona value is discarded.

The same class had previously been patched at two other seams (lazy `db` in
nine cycle members; lazy `config` re-export in `machina.core`), but config
load still dragged the whole memory/provider tree in through the memory
package's eager re-exports.

## Fix

`machina/memory/__init__.py` now resolves its public surface **lazily**
(PEP 562 `__getattr__`, mirroring the `machina.core` Bug-15 pattern):

* the package body imports nothing beyond `importlib`/`typing`;
* `_LAZY_EXPORTS` maps every `__all__` name to its `(submodule, attribute)`;
* `_SUBMODULES` keeps `machina.memory.<submodule>` attribute access working;
* `__dir__` exposes the same surface for introspection.

Config validation therefore imports only `machina.memory.redaction` — a
stdlib-only leaf — and never touches `approval → provider` during a load.

## Proof (RED first)

| Stage | Command | Result |
|---|---|---|
| RED | `pytest tests/test_config_import_cycle.py -k "provider_first or pulls_no_provider"` on the pre-fix tree | **2 failed** (`persona dropped`; `provider stack pulled`) — `logs/persona_red.log` |
| GREEN | full `tests/test_config_import_cycle.py` on the fixed tree | **7 passed** — `logs/persona_green.log` |
| Focused | config + memory + soulspec + rsi + persona + growth + architecture + leaf suites | **418 passed** |
| Package-attr consumers | `test_review_round2_fixes`, `test_delegation_boundaries`, `test_production_readiness_p1` | **91 passed** |
| Import cost | `python -X importtime -c "import machina.memory"` | **203,894 µs → 13,236 µs** (stack no longer pulled) |

New regression pins (`tests/test_config_import_cycle.py`):

* `test_provider_first_import_order_keeps_file_config` — provider-first order
  keeps `persona_description`, no `Ignoring unreadable config` on stderr.
* `test_config_load_pulls_no_provider_stack` — `Config.load` must not put
  `machina.core.provider` **or** `machina.memory.approval` into `sys.modules`.

## Pre-existing, unrelated (recorded, not fixed here)

`tests/test_leaf_independence.py::test_imports_with_core_blocked[machina.tools.repo]`
fails when the file is run in isolation — on the pre-fix tree too
(`git checkout` the old `machina/memory/__init__.py`, same failure; see
`logs/leaf_independence_isolated.log`). `machina.tools.repo` transitively
imports `machina.tools.file`, whose module-level
`from machina.core.config import config` trips the test's core blocker. It
passes inside the full suite only because `machina.tools.file` is already
cached by then — an order-dependence in that test's "leaf-local deps are
converted-or-clean" premise, independent of this change.
