"""R4-027 E2E scratch proof: drive the real ``machina uninstall`` command against a
temp HOME whose shim points at (a) a sibling checkout and (b) this checkout.

Modes:
  old <unfixed_shim.py>  — inject the HEAD (unfixed) ``_shim_points_at``
  live                   — use the fixed function from ``machina.cli``

Scenarios: ``sibling`` (shim execs ``<root>-old/...``) and ``own`` (shim execs
``<root>/...``). Exits 0 when the observed shim outcome matches the expectation
for the mode/scenario pair.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from typer.testing import CliRunner

import machina.cli as cli
import machina.security.env_file as env_file_mod

EXPECT_SURVIVES = {
    ("old", "sibling"): False,  # the BUG: wrong checkout's shim is deleted
    ("old", "own"): False,
    ("live", "sibling"): True,  # fixed: sibling shim left alone
    ("live", "own"): False,
}


def main() -> int:
    mode, scenario = sys.argv[1], sys.argv[2]
    if mode == "old":
        ns: dict = {}
        exec(Path(sys.argv[3]).read_text(encoding="utf-8"), ns)  # noqa: S102
        cli._shim_points_at = ns["_shim_points_at"]

    tmp = Path(tempfile.mkdtemp(prefix="r4-027-e2e-"))
    fake_root = tmp / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    fake_env = tmp / ".env"
    fake_env.write_text("DATABASE_URL=postgres://x\n", encoding="utf-8")
    home = tmp / "home"
    target = fake_root if scenario == "own" else Path(f"{fake_root}-old")
    shim = home / ".local" / "bin" / "machina"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'exec "{target}/.venv/bin/machina" "$@"\n', encoding="utf-8")

    cli._uninstall_app_root = lambda: fake_root
    env_file_mod.find_env_file = lambda: fake_env
    Path.home = classmethod(lambda cls: home)
    cli._remove_windows_user_path_entries = lambda targets: (True, "ok")
    cli._docker_container_exists = lambda: False
    cli._brew_postgres_present = lambda: False

    result = CliRunner().invoke(cli.app, ["uninstall"], input="y\nn\nn\nn\nn\n")
    survives = shim.is_file()
    expected = EXPECT_SURVIVES[(mode, scenario)]
    ok = survives is expected
    print(
        f"mode={mode} scenario={scenario} exit={result.exit_code} "
        f"shim_survives={survives} expected={expected} {'PASS' if ok else 'FAIL'}"
    )
    if not ok:
        print(result.output)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
