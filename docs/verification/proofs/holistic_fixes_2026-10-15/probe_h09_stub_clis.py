"""H09 reproduction probe: delegated prompts that become command options.

Creates one stub CLI per run-capable harness (its argv parser emulates the
documented/verified surface of the real binary: opencode v2.0.26 Effect-CLI
`--` split, codex-cli 0.162.1 clap `exec [OPTIONS] [PROMPT]` + stdin `-`,
Hermes v0.21.6 argparse `-z/--oneshot PROMPT`), puts the stub directory first
on PATH, and delegates the prompt ``--help`` through ``machina.interop.run_task``.

Expected while H09 is present:
  * opencode / codex: the prompt is consumed as the help flag; the CLI prints
    its usage text, exits 0, and ``run_task`` reports ``ok=True`` although no
    task ran (the finding's false-success signature).
  * hermes: argparse refuses the option-like value after ``-z``; the run fails
    with a usage error although the prompt was legitimate.

No real harness process is ever spawned; the stubs are shell scripts that
only read their own argv/stdin and print a marker line.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


def _load_interop(module_path: str | None):
    """The repo's ``machina.interop``, or a specific file (e.g. HEAD revision)."""
    if module_path is None:
        import machina.interop as module

        return module
    spec = importlib.util.spec_from_file_location("h09_probe_interop", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their defining module
    spec.loader.exec_module(module)
    return module


interop = _load_interop(None)  # rebound in main() when --module is given

OPENCODE_STUB = """#!/bin/sh
# opencode v2.0.26 argv emulation: `--` ends flag parsing; an option-like
# token before `--` is a flag (--help prints usage, exit 0).
parse=1
prompt=""
for a in "$@"; do
  if [ "$parse" = 1 ] && [ "$a" = "--" ]; then parse=0; continue; fi
  if [ "$parse" = 1 ] && [ "$a" = "run" ]; then continue; fi
  if [ "$parse" = 1 ]; then
    case "$a" in
      --help|-h) printf 'OPENCODE HELP (usage text, exit 0)\\n'; exit 0 ;;
      -*) printf 'unknown option %s\\n' "$a" >&2; exit 1 ;;
    esac
  fi
  prompt="$a"
done
printf 'TASK: %s\\n' "$prompt"
"""

CODEX_STUB = """#!/bin/sh
# codex-cli 0.162.1 argv emulation: `exec [OPTIONS] [PROMPT]`; `-` reads the
# prompt from stdin; an option-like token is a flag (--help prints usage).
prompt=""
stdin_prompt=0
for a in "$@"; do
  case "$a" in
    exec) continue ;;
    -) stdin_prompt=1; continue ;;
    --help|-h) printf 'CODEX HELP (usage text, exit 0)\\n'; exit 0 ;;
    -*) printf 'error: unexpected argument %s\\n' "$a" >&2; exit 2 ;;
  esac
  prompt="$a"
done
if [ "$stdin_prompt" = 1 ]; then
  IFS= read -r prompt
fi
printf 'TASK: %s\\n' "$prompt"
"""

HERMES_STUB = """#!/bin/sh
# Hermes v0.21.6 argparse emulation: `-z/--oneshot PROMPT`; an option-like
# value after a separate flag is refused (`expected one argument`, exit 2);
# `--oneshot=<value>` binds the value literally.
for a in "$@"; do
  case "$a" in
    --oneshot=*) printf 'TASK: %s\\n' "${a#--oneshot=}"; exit 0 ;;
  esac
done
if [ "$1" = "-z" ] || [ "$1" = "--oneshot" ]; then
  case "${2:-}" in
    ""|-*) printf 'usage: hermes: error: argument -z/--oneshot: expected one argument\\n' >&2; exit 2 ;;
    *) printf 'TASK: %s\\n' "$2"; exit 0 ;;
  esac
fi
printf 'no oneshot flag\\n' >&2
exit 2
"""

STUBS = {"opencode": OPENCODE_STUB, "codex": CODEX_STUB, "hermes": HERMES_STUB}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--module",
        default=None,
        help="interop.py to probe (default: the working tree's machina.interop)",
    )
    args = parser.parse_args()
    global interop
    interop = _load_interop(args.module)
    print(f"probed module: {interop.__file__}")

    with tempfile.TemporaryDirectory(prefix="h09-stubs-") as tmp:
        stub_dir = Path(tmp)
        for name, body in STUBS.items():
            path = stub_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        # Prove the stub is what run_task resolves (never the real binary).
        resolved = {name: shutil.which(name) for name in STUBS}
        print("stub resolution:", resolved)
        print()

        prompt = "--help"
        for platform in ("opencode", "codex", "hermes"):
            result = interop.run_task(platform, prompt, timeout=10.0)
            print(f"platform={platform} prompt={prompt!r}")
            print(f"  command    = {result.command}")
            print(f"  exit_code  = {result.exit_code}")
            print(f"  ok         = {result.ok}")
            print(f"  stdout     = {result.stdout!r}")
            print(f"  stderr     = {result.stderr!r}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
