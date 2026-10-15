# R4-049b — chmod-failure fd-close helper (remaining sites)

- **Task:** `FIX_PLAN_ROUND4_TASKS.md` R4-049b. Lock-timeout portions belong to
  other lanes (R4-040_041_049_chatgpt, R4-045_047_049_providers); this lane is
  the chmod-fd-close helper + remaining sites only.
- **Owned files:** `machina/security/store_fence.py` (additive export only),
  `machina/security/chatgpt_auth.py`, `machina/security/copilot_auth.py`,
  `machina/security/providers.py` (fd-close sites only),
  `tests/test_r4_049b_fdclose.py` (new). Untouched: `env_file.py`
  (R4-093 lane, read-only), deleted `claude_auth.py`/`gemini_auth.py`.
- **Date:** 2026-10-14 · **No commit** (per lane instructions).

## 1. Root cause

Every store writer does `mkstemp` → `os.chmod(tmp, 0o600)` → `os.fdopen(fd)`.
When the path-based chmod raises, `fdopen` never takes ownership and the
shared `except` only unlinks the temp *name*: the descriptor leaks. 4 live
sites: `chatgpt_auth.invalidate_credentials`, `chatgpt_auth.save_tokens`,
`copilot_auth.save_tokens`, `providers._save_store`. (`store_fence`'s own
`invalidate_credentials` keeps its shape — additive-export-only constraint;
`env_file._write_env_lines` already uses the fd-based `fchmod` + sentinel
idiom and is another lane's file.)

## 2. Fix (+23/−4 across 4 files)

- `store_fence.chmod_temp_file(fd, tmp_name, mode=0o600)` (new, +18): chmods
  the path; on any failure closes `fd` (suppressed) and reraises unchanged.
  Temp-name unlink stays with callers. No existing helper altered.
- Call sites (one-line swaps): `chatgpt_auth.py:153,319`,
  `copilot_auth.py:167`, `providers.py:296` (+1 import of `store_fence`;
  cycle-free: `store_fence` imports no machina modules).
- Behavior on success is byte-identical (same `os.chmod` syscall).

## 3. Proof — adversarial, RED-first

`tests/test_r4_049b_fdclose.py` (new, 6 tests): fault-injects the
`os.chmod` syscall (stdlib, never the seam) while calling the real writers
against real files in `tmp_path` — chatgpt `save_tokens` under a real
`flock`, chatgpt `invalidate_credentials`, copilot `publish_device_token`
(real `store_fence.store_lock`), providers `save_models` (real
`_store_lock`). `tempfile.mkstemp` is spied (stdlib instrumentation) to
learn the fd; the assertion is `os.fstat(fd)` → `OSError` (EBADF) plus
no `*.tmp` leftovers and store untouched/absent. Success-path test pins
0600 + round-trip on all three stores.

| Run | Result | Log |
|---|---|---|
| RED (fix stashed, final test text) | **5 failed, 1 passed** — 4× `mkstemp fd N leaked: still open after chmod failure` (named reason) + helper `AttributeError` (absent); 0600 pin green | `R4-049b_fdclose_red.txt` |
| GREEN | **6 passed** | `R4-049b_fdclose_green.txt` |
| Stability | **5× `6 passed`** `--noconftest` (see §5) | shell history |

Note: `invalidate_credentials` fail-softs a lost durable counter into the
in-process generation by design (logout must never fail), so the chmod fault
does not propagate there — the test asserts fd hygiene + int return only.

## 4. Regression checks

- `test_chatgpt_auth.py` + `test_copilot_auth.py` + `test_provider_store.py`
  + `test_chatgpt_auth_adversarial.py` + `test_r4_045_047_049_providers.py`:
  **99 passed** (`R4-049b_fdclose_suites.txt`).
- `ruff format --check` + `ruff check` on all 5 touched files: clean.
- No god-file / package ceilings cover `machina/security/*`; provider
  package untouched.

## 5. Caveats

- Concurrent lanes are churning the tree mid-run: during verification
  `tests/conftest.py` broke twice (first `machina/tools/base.py` lost
  `import logging`, then `context_manager._pending` vanished) — both outside
  this lane. `--noconftest` was used for this file's runs (it needs no
  conftest fixtures: `tmp_path` + `monkeypatch` only); related suites also
  pass under it. Re-run canonically once the tree settles.
- `store_fence.invalidate_credentials` retains the same latent leak; unifying
  it onto the helper is a one-line follow-up for whichever lane owns that
  file next (kept out by the additive-only constraint).
