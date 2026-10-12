# Permission policy review — 2026-10-10

The default `ask` mode now lets routine project tools proceed and reserves
approval for risky or unclassified effects. Existing `ask` sessions receive
this behavior after restarting the harness; no mode change or database
migration is needed.

## Review coverage

Traced permission decisions across CLI/TUI, HTTP, gateway, agent tool execution,
scheduled agent/script jobs, delegation, registered tools/plugins, session-mode
persistence, durable grants, and approval resolution/resume. Also checked the
shared runtime/factory, prompt/skill integration, memory/RAG ownership boundaries,
filesystem and process backends, credential handling, and documented modes.
This is a permission-focused harness review, not a claim of comprehensive
security certification or a line-by-line audit of every unrelated algorithm.

## Causes and corrections

1. Sensitive checks searched arbitrary substrings in complete paths. `shadow`
   in `/home/user/...` classified ordinary project files as credentials.
   Checks now use credential components/locations and canonical command operands.
   Source/documentation names such as `secrets.py` and `credentials.md` are ordinary
   files; `.env.example` remains readable.
2. Default mode required approval for every file write, process execution, and
   network fetch. Routine project edits, known local commands/workflows, and
   built-in web reads now proceed. Local document retrieval is a context read.
3. Terminal validation rejected broker-authorized test/build executables and
   resolved relative working directories against process CWD. It now verifies
   the authorized argv/cwd snapshot and runs in the configured project directory.
4. Review caught compact option and symlink paths that could bypass risk checks.
   File operands are inspected, ambiguous compact options require review, and
   security-settings protection covers known terminal mutators as well as writes.
5. Extension approvals did not bind arguments. Canonical argument JSON now
   contributes to the approval digest and a redacted preview; changed arguments
   cannot reuse the original approval.
6. Docker's project mount was still read-only for approved default-mode work.
   Broker-authorized processes now receive a writable project mount, retaining
   the read-only container root, network restriction, dropped capabilities, and
   other container limits. Actual writes still require the container user's
   filesystem permissions; no live Docker execution was verified here.

## Effective default policy

| Action | Approval |
| --- | --- |
| Ordinary workspace reads, searches, and edits | No |
| Built-in web search/extraction and document retrieval | No |
| Local Git inspection, add, and commit | No |
| Common project test/build/lint/typecheck/format workflows | No |
| Credential access, elevation, security-setting writes, deletion/discard | Yes |
| Publishing/deploy, installation, arbitrary scripts or shell execution | Yes |
| Access outside the configured workspace | Yes |
| Unknown extension effects | Yes |

Project test/build/lint workflows execute operator-trusted project code and
dependencies. Automatic approval is not host isolation. Full mode still requires
its explicit session grant, and credential/elevation/destructive/security-setting
capabilities still need exact consent. Backend validation remains independent:
approval cannot make an invalid path or unavailable backend executable.

## Verification

New regressions were observed failing before their fixes. Coverage includes a
real write/read through the agent loop, a real terminal test runner, configured
working directories, credential filenames and symlinks, workspace boundaries,
command-option paths, security-setting mutations, full-grant exclusions, extension
argument binding, and Docker mount arguments. Approval recovery is tested with
an extension effect, since ordinary built-in file writes no longer pause.

The existing child-process termination regression needed `start_new_session=True`:
its child shared pytest's process group, so the tree-kill assertion killed the
entire test runner. The test now models the production subprocess setup.

Python lint and formatting of changed production files pass. UI typechecking
passes. Focused permission/backend/approval/scheduler/security run:
**226 passed, 1 skipped**. Final full Python run: **1,764 passed, 4 failed, 5 skipped**. UI run:
**95 passed, 1 failed, 3 skipped**. The full Python suite has four failures reproduced on an unchanged HEAD
snapshot, and the UI suite has one failure reproduced on the same snapshot:

- `test_features_split.py::test_all_feature_methods_accessible_after_split`:
  expected method set omits `session.setModel`.
- `test_memory.py::TestImportanceScorer::test_score_deterministic`:
  time-dependent floating-point difference.
- `test_research_phase2.py::test_labeled_identity_sequence_evaluation_cleans_up`:
  missing `research/identity_scenarios_example.json`.
- `test_schema_conformance.py::test_static_python_sql_compiles_against_schema`:
  research workflow table absent in the checked schema.
- UI `selected rows paint an accent background pill`: missing expected fill.

The repository-wide format check also reports existing formatting in
`machina/gateway/server.py` and `machina/memory/user_profile.py`; these files were not changed.
