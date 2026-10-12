"""Installer env-file, secret-prompt, and DB-provisioning regressions.

Drives tools/install-helpers.sh (and the provisioning functions it now owns)
with mocked commands, isolated HOME/tmp dirs, and an isolated
pseudo-terminal — never the developer's terminal, database, or shell files.
MACHINA_INSTALL_LIB may point the driver at an alternate lib copy (used to show
the brew-URL regression fails against the pre-fix logic).
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import subprocess
import termios
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEFAULT_LIB = REPO / "tools" / "install-helpers.sh"
INSTALL_SH = REPO / "install.sh"

PRELUDE = """set -euo pipefail
LIB_PATH="${MACHINA_INSTALL_LIB:-__LIB__}"
source "$LIB_PATH"
log() { printf 'LOG: %s\\n' "$*"; }
warn() { printf 'WARN: %s\\n' "$*" >&2; }
die() { printf 'DIE: %s\\n' "$*" >&2; exit 99; }
rand_hex() { printf '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'; }
"""


def _base_env(extra=None, path_prefix=()):
    env = dict(os.environ)
    env.pop("MACHINA_ENV_FILE", None)
    env.pop("MACHINA_INSTALL_LIB", None)
    if "MACHINA_INSTALL_LIB" in (extra or {}):
        env["MACHINA_INSTALL_LIB"] = extra["MACHINA_INSTALL_LIB"]
    for key in ("DATABASE_URL", "OPENROUTER_API_KEY"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    if "PATH" not in (extra or {}):
        parts = [str(p) for p in path_prefix] + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env["PATH"] = ":".join(parts)
    return env


def run_bash(script, cwd, env=None, timeout=30, **kwargs):
    """Run a script fragment with the lib sourced; isolated from the developer shell."""
    lib = os.environ.get("MACHINA_INSTALL_LIB", str(DEFAULT_LIB))
    full = PRELUDE.replace("__LIB__", lib) + script
    merged = _base_env(env)
    return subprocess.run(
        ["bash", "-c", full],
        cwd=str(cwd),
        env=merged,
        capture_output=True,
        text=True,
        timeout=timeout,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )


def file_mode(path):
    return oct(os.stat(path).st_mode & 0o777)


# ─── lib hygiene ─────────────────────────────────────────────────────────────


def test_lib_sources_without_side_effects(tmp_path):
    assert DEFAULT_LIB.is_file()
    before = set(os.listdir(tmp_path))
    proc = run_bash("echo sourced-ok", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "sourced-ok" in proc.stdout
    assert set(os.listdir(tmp_path)) == before


def test_lib_never_calls_die():
    text = DEFAULT_LIB.read_text()
    hits = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith("die") and not line.lstrip().startswith("#")
    ]
    assert hits == []


def test_syntax_check_both_files():
    for path in (INSTALL_SH, DEFAULT_LIB):
        proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_install_progress_is_plain_when_output_is_piped(tmp_path):
    proc = run_bash(
        'run_animated "Installing backend" bash -c "printf done\\n"',
        cwd=tmp_path,
        env={"TERM": "xterm", "NO_COLOR": "", "CI": ""},
    )
    assert proc.returncode == 0, proc.stderr
    assert "Installing backend" in proc.stdout
    assert "done" in proc.stdout
    assert "\x1b[" not in proc.stdout


def test_install_progress_preserves_failure_output_and_status(tmp_path):
    proc = run_bash(
        'run_animated "Installing backend" bash -c "echo package-failed >&2; exit 7"',
        cwd=tmp_path,
    )
    assert proc.returncode == 7
    assert "package-failed" in proc.stderr


def test_installer_next_steps_put_setup_before_launch_and_report_init_state(tmp_path):
    ready = run_bash("installer_next_steps ready /checkout/README.md", cwd=tmp_path)
    assert ready.returncode == 0, ready.stderr
    assert "initialized automatically" in ready.stdout
    assert (
        ready.stdout.index("machina setup")
        < ready.stdout.index("machina doctor")
        < ready.stdout.index("machina chat")
    )
    assert "If setup changes DATABASE_URL, run machina init again" in ready.stdout

    failed = run_bash("installer_next_steps failed /checkout/README.md", cwd=tmp_path)
    assert failed.returncode == 0, failed.stderr
    assert (
        failed.stdout.index("machina setup")
        < failed.stdout.index("machina init")
        < failed.stdout.index("machina chat")
    )
    assert "not ready" in failed.stdout


# ─── resolve_env_file ────────────────────────────────────────────────────────


def test_resolve_env_file_unset_uses_checkout(tmp_path):
    proc = run_bash("resolve_env_file /startup", cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"{tmp_path}/.env"


def test_resolve_env_file_absolute_override(tmp_path):
    proc = run_bash("resolve_env_file /startup", cwd=tmp_path, env={"MACHINA_ENV_FILE": "/tmp/x y/.env"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "/tmp/x y/.env"


def test_resolve_env_file_relative_anchors_to_startup(tmp_path):
    proc = run_bash(
        "resolve_env_file /startup; echo; cd / && resolve_env_file /startup; echo",
        cwd=tmp_path,
        env={"MACHINA_ENV_FILE": "rel dir/custom.env"},
    )
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines == ["/startup/rel dir/custom.env", "/startup/rel dir/custom.env"]


def test_resolve_env_file_unicode_name(tmp_path):
    proc = run_bash("resolve_env_file /s", cwd=tmp_path, env={"MACHINA_ENV_FILE": "/tmp/ünïcode.env"})
    assert proc.stdout.strip() == "/tmp/ünïcode.env"


# ─── env_get / env_set ───────────────────────────────────────────────────────


def test_env_round_trip_with_spaces_and_unicode(tmp_path):
    env_file = tmp_path / "my env.env"
    proc = run_bash(
        'env_set "API_KEY" "väl ue with spaces=and=equals" "$F" && env_get "API_KEY" "$F"',
        cwd=tmp_path,
        env={"F": str(env_file)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "väl ue with spaces=and=equals"
    assert file_mode(env_file) == "0o600"


def test_env_set_preserves_unrelated_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=yes\nDATABASE_URL=custom://x\n")
    proc = run_bash(
        'env_set DATABASE_URL "custom://y" "$F"', cwd=tmp_path, env={"F": str(env_file)}
    )
    assert proc.returncode == 0, proc.stderr
    lines = env_file.read_text().splitlines()
    assert "KEEP=yes" in lines
    assert "DATABASE_URL=custom://y" in lines
    assert sum(1 for line in lines if line.startswith("DATABASE_URL=")) == 1
    assert not list(tmp_path.glob(".env.tmp.*"))


def test_env_set_creates_missing_file_mode_600(tmp_path):
    env_file = tmp_path / "sub" / ".env"
    env_file.parent.mkdir()
    proc = run_bash('env_set FOO bar "$F"', cwd=tmp_path, env={"F": str(env_file)})
    assert proc.returncode == 0, proc.stderr
    assert env_file.read_text() == "FOO=bar\n"
    assert file_mode(env_file) == "0o600"


def test_env_set_rejects_bad_keys_and_targets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OK=1\n")
    before = env_file.read_bytes()
    for bad in ["", "9LIVE", "HAS SPACE", "DASH-ED", "a=b"]:
        proc = run_bash(
            f'if env_set {bad!r} v "$F"; then echo ACCEPTED; else echo REJECTED; fi',
            cwd=tmp_path,
            env={"F": str(env_file)},
        )
        assert "REJECTED" in proc.stdout, bad
    assert env_file.read_bytes() == before
    proc = run_bash(
        'if env_set K v "$F"; then echo ACCEPTED; else echo REJECTED; fi',
        cwd=tmp_path,
        env={"F": str(tmp_path / "no-such-dir" / ".env")},
    )
    assert "REJECTED" in proc.stdout
    (tmp_path / "adir").mkdir()
    proc = run_bash(
        'if env_set K v "$F"; then echo ACCEPTED; else echo REJECTED; fi',
        cwd=tmp_path,
        env={"F": str(tmp_path / "adir")},
    )
    assert "REJECTED" in proc.stdout


def test_env_get_missing_file_is_empty(tmp_path):
    proc = run_bash(
        'test -z "$(env_get NOPE "$F")" && echo EMPTY',
        cwd=tmp_path,
        env={"F": str(tmp_path / "missing")},
    )
    assert "EMPTY" in proc.stdout


# ─── DATABASE_URL guard ──────────────────────────────────────────────────────


def test_db_url_guard_cases(tmp_path):
    script = (
        'check() { if db_url_is_installer_default "$1"; then echo "$2 REPLACEABLE";'
        ' else echo "$2 PRESERVED"; fi; };'
        'check "$F_MISSING" missing;'
        'printf "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina\\n" > "$F_DEF";'
        'check "$F_DEF" default;'
        'printf "DATABASE_URL=postgresql://u:p@db.internal:5432/prod\\n" > "$F_CUSTOM";'
        'check "$F_CUSTOM" custom;'
        'printf "DATABASE_URL=\\n" > "$F_EMPTY";'
        'check "$F_EMPTY" empty;'
    )
    proc = run_bash(
        script,
        cwd=tmp_path,
        env={
            "F_MISSING": str(tmp_path / "nope"),
            "F_DEF": str(tmp_path / "d.env"),
            "F_CUSTOM": str(tmp_path / "c.env"),
            "F_EMPTY": str(tmp_path / "e.env"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "missing REPLACEABLE" in proc.stdout
    assert "default REPLACEABLE" in proc.stdout
    assert "custom PRESERVED" in proc.stdout
    assert "empty REPLACEABLE" in proc.stdout


def test_maybe_write_db_url_preserves_custom(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://u:p@db.internal:5432/prod\nOTHER=1\n")
    proc = run_bash(
        'maybe_write_db_url "postgresql://machina:NEW@localhost:5432/machina" "$F"',
        cwd=tmp_path,
        env={"F": str(env_file)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "Reusing existing custom DATABASE_URL" in proc.stderr
    assert (
        "DATABASE_URL=postgresql://u:p@db.internal:5432/prod" in env_file.read_text().splitlines()
    )
    assert "NEW" not in env_file.read_text()


def test_maybe_write_db_url_replaces_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina\n")
    proc = run_bash(
        'maybe_write_db_url "postgresql://machina:NEW@localhost:5432/machina" "$F"',
        cwd=tmp_path,
        env={"F": str(env_file)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "Wrote generated Postgres credentials" in proc.stdout
    assert (
        "DATABASE_URL=postgresql://machina:NEW@localhost:5432/machina"
        in env_file.read_text().splitlines()
    )
    assert file_mode(env_file) == "0o600"


# ─── install.sh wiring (fails on the pre-fix script) ─────────────────────────


def test_install_sh_sources_helpers():
    text = INSTALL_SH.read_text()
    assert "tools/install-helpers.sh" in text


def test_install_sh_delegates_db_url_writes():
    lib = DEFAULT_LIB.read_text()
    # Definition + docker-branch + brew-branch call sites.
    assert lib.count("maybe_write_db_url") >= 3


def test_install_sh_uses_hidden_prompt_without_fallback():
    text = INSTALL_SH.read_text()
    assert "prompt_secret_hidden" in text
    assert "read -r ANSWER < /dev/tty" not in text


def test_install_sh_no_bare_env_file_ops():
    text = INSTALL_SH.read_text()
    for banned in [">> .env", "> .env", '" .env > "', 'mv "$TMP_ENV" .env', "mktemp .env.tmp"]:
        assert banned not in text, banned


# ─── prompt_secret_hidden over an isolated pty ───────────────────────────────


def _spawn_ctty(script, cwd, env):
    """Spawn bash with a fresh controlling terminal; return (proc, master, slave_name)."""
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    merged = _base_env(env)
    merged["TERM"] = (env or {}).get("TERM", "dumb")

    def preexec():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    lib = os.environ.get("MACHINA_INSTALL_LIB", str(DEFAULT_LIB))
    full = PRELUDE.replace("__LIB__", lib) + script
    proc = subprocess.Popen(
        ["bash", "-c", full],
        cwd=str(cwd),
        env=merged,
        stdin=slave,
        stdout=slave,
        stderr=subprocess.STDOUT,
        preexec_fn=preexec,
        close_fds=True,
    )
    os.close(slave)
    return proc, master, slave_name


def _drain(master, proc, timeout=12):
    out = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            try:
                while True:
                    ready, _, _ = select.select([master], [], [], 0.3)
                    if not ready:
                        break
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    out += chunk
            except OSError:
                pass
            break
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                out += os.read(master, 65536)
            except OSError:
                break
    return out


def _echo_enabled(master):
    try:
        return bool(termios.tcgetattr(master)[3] & termios.ECHO)
    except termios.error:
        return None


def test_install_progress_animates_only_on_a_terminal(tmp_path):
    proc, master, _ = _spawn_ctty(
        'run_animated "Installing backend" bash -c "sleep 0.35; echo backend-ready"',
        cwd=tmp_path,
        env={"TERM": "xterm", "NO_COLOR": "", "CI": ""},
    )
    try:
        transcript = _drain(master, proc).decode("utf-8", "replace")
        assert proc.wait(timeout=10) == 0, transcript
        assert "\x1b[2K" in transcript
        assert "Installing backend" in transcript
        assert "backend-ready" in transcript
    finally:
        os.close(master)


def test_install_progress_on_terminal_shows_failure_and_returns_status(tmp_path):
    proc, master, _ = _spawn_ctty(
        'run_animated "Installing backend" bash -c "echo package-failed >&2; exit 7"',
        cwd=tmp_path,
        env={"TERM": "xterm", "NO_COLOR": "", "CI": ""},
    )
    try:
        transcript = _drain(master, proc).decode("utf-8", "replace")
        assert proc.wait(timeout=10) == 7, transcript
        assert "Installing backend" in transcript
        assert "package-failed" in transcript
    finally:
        os.close(master)


def test_prompt_hidden_input_round_trip(tmp_path):
    proc, master, _ = _spawn_ctty(
        'if out=$(prompt_secret_hidden "P: "); then echo "rc=0 val=$out";'
        ' else echo "rc=1 val=$out"; fi',
        cwd=tmp_path,
        env={},
    )
    try:
        time.sleep(0.7)
        os.write(master, b"s3cret\r")
        transcript = _drain(master, proc).decode("utf-8", "replace")
        assert proc.wait(timeout=10) == 0
        assert "rc=0 val=s3cret" in transcript
        assert transcript.count("s3cret") == 1, "typed secret must not echo"
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def test_prompt_fails_without_tty_and_leaves_echo_on(tmp_path):
    proc = run_bash(
        'if out=$(prompt_secret_hidden "P: "); then echo "rc=0 out=[$out]";'
        ' else echo "rc=1 out=[$out]"; fi',
        cwd=tmp_path,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "rc=1 out=[]" in proc.stdout


def test_prompt_stty_state_failure_is_safe(tmp_path, tmp_bin):
    proc = run_bash(
        'if out=$(prompt_secret_hidden "P: "); then echo "rc=0 out=[$out]";'
        ' else echo "rc=1 out=[$out]"; fi',
        cwd=tmp_path,
        env={"PATH": f"{tmp_bin}:/usr/bin:/bin", "FAIL_MODE": "state"},
        timeout=15,
    )
    assert "rc=1 out=[]" in proc.stdout


def test_prompt_echo_disable_failure_is_safe(tmp_path, tmp_bin):
    proc = run_bash(
        'if out=$(prompt_secret_hidden "P: "); then echo "rc=0 out=[$out]";'
        ' else echo "rc=1 out=[$out]"; fi',
        cwd=tmp_path,
        env={"PATH": f"{tmp_bin}:/usr/bin:/bin", "FAIL_MODE": "echo"},
        timeout=15,
    )
    assert "rc=1 out=[]" in proc.stdout


def test_prompt_hangup_runs_restore_trap_and_dies(tmp_path):
    # Direct SIGINT to a pty-spawned bash is swallowed in some spawners, so
    # interruption is delivered as a kernel hangup (SIGHUP) by closing the
    # master side. A bare read would die by SIGHUP (-1); the armed trap
    # restores echo and re-raises as SIGTERM (-15) instead.
    proc, master, _ = _spawn_ctty('prompt_secret_hidden "P:"', cwd=tmp_path, env={})
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _echo_enabled(master) is False:
                break
            time.sleep(0.1)
        assert _echo_enabled(master) is False, "helper never disabled echo"
        transcript = _drain(master, proc, timeout=2).decode("utf-8", "replace")
        assert "P:" in transcript
        os.close(master)
        assert proc.wait(timeout=10) == -signal.SIGTERM
        master = -1
    finally:
        if master != -1:
            try:
                os.close(master)
            except OSError:
                pass


@pytest.fixture
def tmp_bin(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "stty").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-g" ] && [ "$FAIL_MODE" = "state" ]; then exit 1; fi\n'
        'if [ "$1" = "-echo" ] && [ "$FAIL_MODE" = "echo" ]; then exit 1; fi\n'
        'exec /bin/stty "$@"\n'
    )
    (bindir / "stty").chmod(0o755)
    return bindir


# ─── real start_brew_db / start_docker_db with stubbed commands ──────────────


def _brew_harness(tmp_path):
    """Fake brew/pg tree; returns (bindir, pg_bin). git/make fail fast."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    for dead in ("git", "make"):
        (bindir / dead).write_text("#!/usr/bin/env bash\nexit 1\n")
        (bindir / dead).chmod(0o755)
    pgbin = tmp_path / "fakebrew" / "bin"
    pgbin.mkdir(parents=True, exist_ok=True)
    (bindir / "brew").write_text(
        "#!/usr/bin/env bash\n"
        'echo "brew $*" >> "$BREW_LOG"\n'
        'if [ "$1" = "--prefix" ]; then echo "$FAKEBREW"; exit 0; fi\n'
        "exit 0\n"
    )
    (bindir / "brew").chmod(0o755)
    share = tmp_path / "fakebrew" / "share" / "extension"
    share.mkdir(parents=True, exist_ok=True)
    (share / "vector.control").write_text("# fake\n")
    (pgbin / "pg_isready").write_text("#!/usr/bin/env bash\nexit 0\n")
    (pgbin / "pg_config").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--sharedir" ]; then echo "$FAKEBREW/share"; exit 0; fi\n'
        'echo "PostgreSQL 16.0"; exit 0\n'
    )
    (pgbin / "psql").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *pg_roles*) echo " $ROLE_EXISTS";;\n'
        '  *pg_database*) echo " $DB_EXISTS";;\n'
        '  *ALTER\\ USER*) echo "$@" >> "$ALTER_LOG"; exit ${PSQL_ALTER_RC:-0};;\n'
        "  *CREATE\\ EXTENSION*) exit ${PSQL_EXT_RC:-0};;\n"
        "esac\n"
        "exit 0\n"
    )
    (pgbin / "createuser").write_text(
        '#!/usr/bin/env bash\necho "createuser $*" >> "$BREW_LOG"\nexit ${CREATE_RC:-0}\n'
    )
    (pgbin / "createdb").write_text(
        '#!/usr/bin/env bash\necho "createdb $*" >> "$BREW_LOG"\nexit ${CREATE_RC:-0}\n'
    )
    for name in ("pg_isready", "pg_config", "psql", "createuser", "createdb"):
        (pgbin / name).chmod(0o755)
    (bindir / "brew").chmod(0o755)
    return bindir, pgbin


def _brew_env(tmp_path, bindir, pgbin, env_file, **overrides):
    env = {
        "OS_FAMILY": "macos",
        "USER": "testuser",
        "ENV_FILE": str(env_file),
        "FAKEBREW": str(tmp_path / "fakebrew"),
        "BREW_LOG": str(tmp_path / "brew.log"),
        "ALTER_LOG": str(tmp_path / "alter.log"),
        "ROLE_EXISTS": "0",
        "DB_EXISTS": "1",
        "PATH": f"{bindir}:{pgbin}:/usr/bin:/bin",
    }
    env.update(overrides)
    return env


def _run_brew(tmp_path, env_content, **overrides):
    bindir, pgbin = _brew_harness(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env_file = workdir / ".env"
    env_file.write_text(env_content)
    before = env_file.read_bytes()
    env = _brew_env(tmp_path, bindir, pgbin, env_file, **overrides)
    proc = run_bash(
        "start_brew_db && echo BREW_RC=0 || echo BREW_RC=1",
        cwd=workdir,
        env=env,
        timeout=60,
    )
    alter_log = tmp_path / "alter.log"
    return proc, env_file.read_text(), before, alter_log.read_text() if alter_log.exists() else ""


def test_brew_custom_url_survives_new_role(tmp_path):
    custom = "DATABASE_URL=postgresql://u:p@db.internal:5432/prod\nOTHER=1\n"
    proc, after, _, _ = _run_brew(tmp_path, custom)
    assert proc.returncode == 0, proc.stderr
    assert "BREW_RC=0" in proc.stdout
    assert "DATABASE_URL=postgresql://u:p@db.internal:5432/prod" in after.splitlines()
    assert "OTHER=1" in after.splitlines()
    assert "machina:" not in after


def test_brew_default_url_gets_generated_credentials(tmp_path):
    default = "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina\n"
    proc, after, _, _ = _run_brew(tmp_path, default)
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in after.splitlines() if line.startswith("DATABASE_URL=")]
    assert lines == [
        "DATABASE_URL=postgresql://machina:0123456789abcdef0123456789abcdef@localhost:5432/machina"
    ]


def test_brew_existing_role_keeps_password_and_url(tmp_path):
    custom = "DATABASE_URL=postgresql://u:p@db.internal:5432/prod\n"
    proc, after, _, alter = _run_brew(tmp_path, custom, ROLE_EXISTS="1")
    assert proc.returncode == 0, proc.stderr
    assert alter == "", "existing role password must never be reset"
    assert "DATABASE_URL=postgresql://u:p@db.internal:5432/prod" in after.splitlines()


def test_brew_failure_leaves_config_untouched(tmp_path):
    custom = "DATABASE_URL=postgresql://u:p@db.internal:5432/prod\n"
    proc, after, before, _ = _run_brew(tmp_path, custom, PSQL_EXT_RC="1", CREATE_RC="1")
    assert proc.returncode == 0  # harness echoes BREW_RC; the function itself failed
    assert "BREW_RC=1" in proc.stdout
    assert after.encode() == before
    assert "Schema ready" not in proc.stdout


def test_brew_refuses_non_macos(tmp_path):
    bindir, pgbin = _brew_harness(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = _brew_env(tmp_path, bindir, pgbin, workdir / ".env", OS_FAMILY="linux")
    proc = run_bash("if start_brew_db; then echo RAN; else echo REFUSED; fi", cwd=workdir, env=env)
    assert "REFUSED" in proc.stdout


def test_docker_custom_url_preserved(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'echo "docker $*" >> "$DOCKER_LOG"\n'
        'if [ "$1" = "ps" ]; then exit 0; fi\n'
        "exit 0\n"
    )
    (bindir / "docker").chmod(0o755)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env_file = workdir / ".env"
    env_file.write_text("DATABASE_URL=postgresql://u:p@db.internal:5432/prod\n")
    env = {
        "OS_FAMILY": "linux",
        "ENV_FILE": str(env_file),
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "PATH": f"{bindir}:/usr/bin:/bin",
    }
    proc = run_bash(
        "start_docker_db && echo DOCKER_RC=0 || echo DOCKER_RC=1",
        cwd=workdir,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DOCKER_RC=0" in proc.stdout
    assert (
        "DATABASE_URL=postgresql://u:p@db.internal:5432/prod" in env_file.read_text().splitlines()
    )
