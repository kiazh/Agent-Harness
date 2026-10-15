# Round 3 review evidence

Applies to commit `d3adcf9`. These are review reproductions, not additions to the application test suite. They use synthetic credentials, fake completions, mocked HTTP, and temporary files. `auth-lifecycle.py` uses a real local loopback listener; `runner.py` launches a temporary marker-writing child. `skill-cap.py` uses only the hard-coded `machina_test` database and removes its own rows. Run database reproductions separately from other schema/setup work.

For Python scripts, run from the repository root with `PYTHONPATH` pointing to that root and the repository virtualenv Python. Isolate `HOME` and set `MACHINA_ENV_FILE` to an existing empty file in a scratch directory before importing application config. Do not use production credentials or databases. The auth-security script also establishes its own isolated home/store before application imports.

For the UI reproduction, create a scratch directory with `src` symlinked to the repository's `ui/src`, `node_modules` symlinked to `ui/node_modules`, and a `test` subdirectory containing `ui-tail.ts`. Run that file with Node. It uses a fake terminal/client, performs no database or network calls, and prints the duplicate-text/stale-tool results.

The backend suite log records two unresolved failures; see the report for scope and the user's request to stop broad test investigation. UI check records 404 passing tests and 6 skipped integrations. The main report records observations from all targeted reproductions; additional broad test execution was not performed after the user's request.
