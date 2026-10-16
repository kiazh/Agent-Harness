# ECC enrichment sidecars

Local, additive routing metadata for **bundled ECC skills** (spec §7.3).
Sidecars improve matching without editing vendored instructions, and they can
never change provenance/trust or grant permissions.

One JSON file per bundle revision: `<revision>.json`, where `<revision>` is the
ECC manifest revision (`machina/skills/bundle.py::PIN` / `manifest.json`). The
file name and the body's `revision` must both equal the revision the
enrichment was written against; a mismatch is reported as `stale-enrichment`
and the loader falls back to baseline metadata (no offsets/sections are ever
applied to unrelated new content).

```json
{
  "schema_version": 1,
  "revision": "<ecc manifest revision>",
  "skills": {
    "<bundled skill id>": {
      "task_families": ["web-build"],
      "capabilities": ["layout"],
      "aliases": ["landing-page"],
      "stack": {"languages": [], "frameworks": [], "independent": true},
      "preconditions": [],
      "required_tools": [],
      "exclusions": []
    }
  }
}
```

Rules enforced by `machina/skills/metadata.py`:

* Entries accept only the closed v2 field set (plus optional `schema_version: 2`
  and `skill_id`, which must match the key). Unknown fields reject the entry
  (`enrichment-invalid`) — sidecars are machine-written, so strictness is cheap.
* Authority-shaped fields (`permission*`, `authority`, `trust*`, `grant*`,
  `privilege*`, `allowed_tool*`, `*escalat*`) reject the entry
  (`enrichment-authority-field`) and are never preserved.
* Entries for unknown skill ids are reported (`enrichment-unknown-skill`).
* Values use the same closed vocabularies and bounds as front-matter schema v2.
* The revision binding is the manifest revision; per-skill content bytes are
  already hash-pinned by the manifest.
* Section anchoring for sidecar-declared sections is resolved against content
  at read time (`metadata.bind_section_anchors`), not eagerly here, so loading
  the bundle stays lazy.

Override the directory with `MACHINA_SKILL_ENRICHMENT_DIR` (used by tests).
