# R5-O · Skill-update atomicity (finding 19) — evidence

## Verdict: FIXED (RED reproduced, then GREEN)

## Finding (from `CODEBASE_REVIEW_CURRENT_2026-10-14.md`)

`SkillRegistry.update_skill` mutated the live `Skill` object
attribute-by-attribute **before** validating the remaining fields and
**before** the blocking `Path.write_text`, and `set_enabled` flipped
`skill.enabled` before delegating to the same non-atomic rewrite. A late
validation error or an `OSError` therefore left proposed fields live in
the registry while the disk stayed stale (or truncated) — memory/disk
divergence, plus non-atomic file replacement.

## RED (reproduced on pre-fix code, 2026-10-15)

New adversarial test `tests/test_r5_019_skill_atomicity.py` diffs the
live registry against the on-disk `SKILL.md` before/after failure:

* valid `description` + injection-pattern `content` → `ValueError`, but
  the live description was already mutated (disk untouched);
* valid description/content + 33-item `triggers` → `ValueError`, but the
  live description/content were already mutated;
* `Path.write_text` raising `OSError` mid-`update_skill` → live mutated,
  disk untouched;
* `Path.write_text` raising `OSError` mid-`set_enabled(False)` → live
  `enabled=False` while disk still says `enabled: true`.

Log: `logs/R5-O_RED.txt` — 4 failed, 1 passed (success-path guard),
exactly the named failure mode. No refute: the bug reproduced.

## Fix (`machina/skills/registry.py`, `update_skill`/`set_enabled` only)

Validate-then-publish plus crash-safe write, mirroring the file's
existing telemetry writer idiom (temp + fsync + `os.replace`):

* `update_skill` validates every proposed field into locals first
  (description ceiling + injection scan, content validation, trigger
  scans + `_validate_metadata` against the *proposed* description), then
  `_write_skill_file` replaces `SKILL.md` atomically, and only then are
  the locals published to the live object. Any `ValueError`/`OSError`
  leaves live + disk untouched.
* `set_enabled` no longer pre-flips `skill.enabled`; it rewrites via the
  same atomic helper and publishes only on success. The no-change
  rewrite path performs no content re-validation (an older stored skill
  can still be enabled/disabled if the scanner has since changed).
* `_write_skill_file` cleans up its temp file on `OSError` and lets the
  error propagate (deliberate error, never swallowed).
* Proposed `triggers` are copied (`list(triggers)`) so the live object
  never aliases the caller's list.

No API change: signatures and `None`/guard returns are unchanged.

## GREEN (post-fix, 2026-10-15)

* `logs/R5-O_GREEN.txt` — `tests/test_r5_019_skill_atomicity.py`: 5 passed.
* `logs/R5-O_adjacent.txt` — `test_fm6_registry_hardening`,
  `test_skill_registry_cache`, `test_skill_runtime`, `test_ecc_skills`,
  `test_fm6_skill_read`: 201 passed.
* `tests/test_skill_workflows.py` (minus the 2 heavy agent-loop cases):
  27 passed.

## Files changed (this lane only; no commit per lane rules)

* `machina/skills/registry.py` — `update_skill`/`set_enabled` paths +
  private `_write_skill_file` helper.
* `tests/test_r5_019_skill_atomicity.py` — new (this lane's proof).
* This evidence doc + `logs/R5-O_{RED,GREEN,adjacent}.txt`.
