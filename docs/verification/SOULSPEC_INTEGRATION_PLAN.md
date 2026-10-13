# Unified SoulSpec persona integration

The requested outcome is one persona lifecycle: SoulSpec identity, `/persona`
style, learned preferences/lessons and RSI guidance reach the same runtime prompt
and survive definition storage and deliberate package export/import.

Implementation:

1. Preserve a structured SoulSpec snapshot in AgentDef and PostgreSQL through an
   additive runtime migration. Legacy definitions get a SoulSpec representation.
2. Resolve the effective persona every turn from stable identity, current human
   style, owned growth notes and RSI guidance. Render structured values, traits,
   voice and declared identity/style/workflow markdown. Preserve the specialist
   role and keep learned notes as fallible data. Learning never grants tools.
3. Preserve persona/config/package resources in serialization. Use a namespaced
   Machina extension for fields beyond Soul Spec's standard markdown package.
   Export ordinary readable SOUL.md plus canonical structured state, without
   exporting ephemeral RSI decisions, raw chat, credentials or observations.
4. Make deny-all explicit and serializable. Permissions intersections and
   adapter/storage/package roundtrips must never turn deny-all into all tools.
5. Add session-scoped `growth export-soul` and `growth import-soul`. Learned
   snapshots require explicit flags; import keeps the current agent ownership
   key and execution authority. Existing `/persona` and `/growth` controls feed
   the unified resolver and preserve their existing reset/rollback meanings.
6. Verify package -> DB -> factory -> prompt, resumed sessions, live style
   edits, teaching, rollback and export/reimport using fake providers and the
   isolated PostgreSQL test database. Run the existing backend checks.

The standard defines SOUL.md identity, IDENTITY.md, STYLE.md and AGENTS.md
companions: https://docs.clawsouls.ai/docs/spec/overview/ . The Machina extension
is explicitly an implementation feature, not a certification claim.

## Implementation evidence (2026-10-13)

Regression checks cover structured identity through adapters and PostgreSQL into
fresh agent instances, shared style/growth compilation, owned note rollback and
forgetting, legacy/provider roundtrips, package resources, explicit deny-all,
malformed/mixed YAML isolation and concurrent permission changes during import.
A real subprocess CLI roundtrip verifies both learned-note opt-in flags and
session ownership against the dedicated PostgreSQL test database.

Independent read-only review found the mixed-YAML restriction loss, invalid YAML
shape propagation, and merged package export failure. Each was reproduced with a
failing regression and fixed; final review found no remaining substantive defects.

The first complete backend run found three outdated assertions: exact legacy
manifest equality (exports now include the structured extension), and two
concurrency tests injecting updates through the replaced prompt builder. The
package test now verifies the extension, original metadata/resources and reload;
the concurrency tests interleave the update after the owned decision snapshot and
still require correct revision attribution and the original note in both loop
modes. All 59 affected integration/RSI/research tests then passed.

Verification uses Python 3.11, synthetic providers and the isolated test database;
no live model requests or production database edits. Packaging verifies the wheel
contains the runtime resolver, lifecycle and additive SQL migration. Ruff lint and
format checks pass. Bandit reports no medium/high findings (99 existing low
findings). These checks establish runtime and persistence wiring; they do not
establish comparative learning quality or guaranteed model persona adherence.

Final backend command:

```sh
MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test \
  /tmp/machina-ci-core-py311/bin/python -m pytest \
  -m 'not ui_integration and not workflow_trial' -q --cov=machina --cov-fail-under=60
```

Result: **2,581 passed, 1 skipped, 9 deselected**, 133.06 seconds; **82.67% coverage**.
UI integration and optional workflow research checks were excluded because this
change touches shared backend persona wiring. Full output was saved locally at
`/tmp/machina-soul-core-final.log`. Working tree changes remain uncommitted.
