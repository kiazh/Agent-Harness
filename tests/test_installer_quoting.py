"""Installer path-quoting round-trip regressions (R5-E, finding 9).

Drives the quoting helpers VERBATIM from install.sh (extracted by function
name — the test fails loudly if they disappear) and proves hostile install
paths round-trip through the generated artifacts:

  * the `machina` shim executes the exact venv binary (no reinterpretation,
    no command execution) for paths containing `$`, backticks, `$(...)`,
    double/single quotes, backslashes, and spaces;
  * `ensure_path_in_rc` (sh + fish flavors) produces an rc line that, when
    sourced, puts the exact hostile dir on PATH;
  * `register_windows_path` builds a PowerShell `-Command` string in which
    the path (e.g. an O'Connor profile dir) stays a single `-quoted token.

Normal paths must be byte-identical to the pre-fix output (no behavior
change). Fixture-level only: no network, no real install, isolated HOME/tmp.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"

# Helpers owned by install.sh for R5-9. Extracted verbatim so the test
# exercises the shipped code, never a copy.
HELPER_FUNCS = ("sh_dquote_escape", "fish_quote", "ps_squote_escape")

HOSTILE_DIRS = [
    "dollar",
    "backtick",
    "cmdsubst",
    "dquote",
    "squote",
    "backslash",
    "trailback",
    "space",
]

HOSTILE_SUFFIX = {
    "dollar": "evil$a",
    "backtick": "evil`touch PWNED`x",
    "cmdsubst": "evil$(touch PWNED)x",
    "dquote": 'evil"q',
    "squote": "evil'q",
    "backslash": r"evil\q",
    "trailback": "evilslash\\",
    "space": "evil dir",
}


def _install_source() -> str:
    return INSTALL_SH.read_text()


def _extract_funcs(source: str, names) -> str:
    """Slice `name() { ... }` blocks verbatim out of install.sh."""
    blocks = []
    for name in names:
        m = re.search(rf"(?m)^{re.escape(name)}\(\) \{{$", source)
        assert m, f"{name}() not found in install.sh — quoting helper missing?"
        start = m.start()
        # function ends at the first line containing only `}`
        end = re.search(r"(?m)^\}$", source[m.end():])
        assert end, f"end of {name}() not found"
        blocks.append(source[start : m.end() + end.end()])
    return "\n".join(blocks) + "\n"


@pytest.fixture()
def helpers_sh() -> str:
    return _extract_funcs(_install_source(), HELPER_FUNCS)


def run_bash(script: str, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _bash_str(s: str) -> str:
    """Single-quote a Python string for safe embedding in a bash script."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─── shim round-trip ──────────────────────────────────────────────────────

SHIM_TEMPLATE = (
    'SHIM_TARGET="$(sh_dquote_escape "$VENV_MACHINA")"\n'
    + 'cat > "$SHIM_OUT" <<EOF2\n'
    + '#!/usr/bin/env sh\n'
    + 'exec "$SHIM_TARGET" "\\$@"\n'
    + 'EOF2\n'
)


def test_helpers_present_in_installer():
    source = _install_source()
    for name in HELPER_FUNCS:
        assert f"{name}() {{" in source


@pytest.mark.parametrize("case", HOSTILE_DIRS)
def test_shim_executes_exact_binary(tmp_path: Path, helpers_sh: str, case: str):
    work = tmp_path / "work"
    work.mkdir()
    target_dir = work / HOSTILE_SUFFIX[case] / ".venv" / "bin"
    target_dir.mkdir(parents=True)
    target = target_dir / "machina"
    target.write_text('#!/usr/bin/env sh\nprintf \'EXEC faux=%s argv=%s\\n\' "$0" "$*"\n')
    target.chmod(0o755)
    shim = work / "shim"
    sentinel = work / "PWNED"

    # Same shape as the install.sh shim block: escape, then interpolate into
    # the double-quoted sh word. (Template placeholder replaced in Python so
    # the test never re-implements the escaping.)
    template = SHIM_TEMPLATE
    script = (
        "set -u\n" + helpers_sh
        + f"VENV_MACHINA={_bash_str(str(target))}\n"
        + f"SHIM_OUT={_bash_str(str(shim))}\n"
        + template
        + f"chmod +x {_bash_str(str(shim))}\n"
        + f"rm -f {_bash_str(str(sentinel))}\n"
        + f"out=$(sh {_bash_str(str(shim))} hello 2>&1); rc=$?\n"
        + "printf 'RC=%s OUT=%s\\n' \"$rc\" \"$out\"\n"
        + f"test ! -e {_bash_str(str(sentinel))}\n"
    )
    proc = run_bash(script, tmp_path)
    assert proc.returncode == 0, f"hostile={case} failed:\n{proc.stdout}\n{proc.stderr}"
    assert f"RC=0 OUT=EXEC faux={target} argv=hello" in proc.stdout, (
        f"hostile={case} shim ran the wrong code:\n{proc.stdout}\n{proc.stderr}"
    )
    assert not sentinel.exists(), f"hostile={case} executed embedded commands"


def test_shim_byte_identical_for_normal_path(tmp_path: Path, helpers_sh: str):
    normal = "/home/operator/machina/.venv/bin/machina"
    script = (
        "set -u\n" + helpers_sh
        + f'V={_bash_str(normal)}\n'
        + 'printf \'exec "%s"\\n\' "$(sh_dquote_escape "$V")"\n'
    )
    proc = run_bash(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f'exec "{normal}"', proc.stdout


# ─── shell-rc round-trip ──────────────────────────────────────────────────

def test_rc_line_round_trip_sh(tmp_path: Path, helpers_sh: str):
    funcs = _extract_funcs(_install_source(), ("shell_rc_file", "ensure_path_in_rc"))
    home = tmp_path / "ho$me`x" / 'o"b'
    shim_dir = home / ".local" / "bin"
    shim_dir.mkdir(parents=True)
    script = (
        "set -u\n"
        + helpers_sh + funcs
        + "log() { printf 'LOG %s\\n' \"$*\"; }\n"
        + "warn() { printf 'WARN %s\\n' \"$*\" >&2; }\n"
        + f"HOME={_bash_str(str(home))}\n"
        + "SHELL=/bin/bash\nSHELL_RC=yes\nOS_FAMILY=linux\nPATH_STATE=\nPATH_RC=\n"
        + f"ensure_path_in_rc {_bash_str(str(shim_dir))}\n"
        + 'printf \'RCFILE=%s\\n\' "$PATH_RC"\n'
        + 'export RC_FILE="$PATH_RC"\n'
        + 'probe_out=$(bash -c \'source "$RC_FILE" && printf "%s" "$PATH"\')\n'
        + 'printf \'PROBE=%s\\n\' "$probe_out"\n'
        + f'case ":$probe_out:" in *{_bash_str(":" + str(shim_dir) + ":")}*) ;; *) exit 42;; esac\n'
    )
    # NOTE: the hostile dir is embedded via _bash_str (single-quoted), so the
    # driver itself never interprets it; the rc line under test is built by
    # install.sh code only.
    proc = run_bash(script, tmp_path)
    assert proc.returncode == 0, f"rc round-trip failed:\n{proc.stdout}\n{proc.stderr}"
    assert f"PROBE=" in proc.stdout


def test_rc_lines_byte_identical_for_normal_path(tmp_path: Path, helpers_sh: str):
    normal = "/home/operator/.local/bin"
    script = (
        "set -u\n" + helpers_sh
        + f'D={_bash_str(normal)}\n'
        + 'printf \'export PATH="%s:$PATH"\\n\' "$(sh_dquote_escape "$D")"\n'
        + 'printf \'%s\\n\' "set -gx PATH $(fish_quote "$D") \\$PATH"\n'
    )
    proc = run_bash(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == f'export PATH="{normal}:$PATH"', lines
    assert lines[1] == f"set -gx PATH {normal} $PATH", lines


# ─── PowerShell quoting ───────────────────────────────────────────────────

PS_TOKEN = re.compile(r"'(?:[^']|'')*'")


def test_windows_path_single_token(tmp_path: Path, helpers_sh: str):
    """O'Connor-style profile dir stays one PowerShell string token."""
    windir = "C:\\Users\\O'Connor $x\\machina\\.venv\\Scripts"
    script = (
        "set -u\n" + helpers_sh
        + f'W={_bash_str(windir)}\n'
        + 'E="$(ps_squote_escape "$W")"\n'
        + 'printf \x27%s\x27 "$E"\n'
    )
    proc = run_bash(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    escaped = proc.stdout
    assert escaped == windir.replace("'", "''"), escaped
    # Embed exactly as register_windows_path does, then tokenize.
    cmd = (
        "[Environment]::SetEnvironmentVariable('Path', "
        "(([Environment]::GetEnvironmentVariable('Path','User') -split ';' | "
        f"Where-Object {{ $_ -ne '{escaped}' }}) + '{escaped}') -join ';', 'User')"
    )
    tokens = PS_TOKEN.findall(cmd)
    assert any(escaped in t for t in tokens), f"path split across PS tokens: {tokens}"
    # Full-command quote balance ('' pairs excluded).
    stripped = re.sub(r"''", "", cmd)
    assert stripped.count("'") % 2 == 0, "unbalanced PowerShell quotes"


def test_register_windows_path_uses_ps_escape():
    source = _install_source()
    assert "windir_ps=\"$(ps_squote_escape \"$windir\")\"" in source
    assert "'$windir_ps'" in source
