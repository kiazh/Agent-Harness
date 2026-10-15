"""R5-B (finding 3) — atomic-write directory identity.

RED (pre-fix, see evidence ``R5-B_atomic_write.md``): with a real concurrent
actor renaming the validated workspace subdirectory to a symlink pointing
outside, ``write_file`` created its temp file AND performed the final replace
through pathnames, so attacker content landed outside the workspace
(``outside/victim.txt`` overwritten, ``.victim.txt.*.tmp`` litter outside).

GREEN: traversal, temp creation, and replacement are anchored to the
validated parent directory descriptor (openat/renameat), keeping the leaf
``O_NOFOLLOW`` probe. Adversarial FS style: a real renamer thread is active
during N writes; 0 escapes allowed.
"""

import asyncio
import os
import shutil
import threading

import pytest

import machina.tools.file as fmod


def _swap_sub_to_outside(base, sub, parking, outside):
    """Real concurrent-actor primitive: rename dir away, link it outside."""
    shutil.rmtree(parking, ignore_errors=True)
    os.rename(sub, parking)
    os.symlink(outside, sub, target_is_directory=True)


def _restore_sub(base, sub, parking):
    """Gentle restore: never delete a real directory, only fix link/missing."""
    try:
        if os.path.islink(sub):
            os.unlink(sub)
            if os.path.isdir(parking) and not os.path.islink(parking):
                os.rename(parking, sub)
        elif not os.path.exists(sub):
            if os.path.isdir(parking) and not os.path.islink(parking):
                os.rename(parking, sub)
        elif os.path.isdir(parking) and not os.path.islink(parking):
            shutil.rmtree(parking, ignore_errors=True)
    except OSError:
        pass


def _require_symlinks(tmp_path):
    probe = tmp_path / ".symlink_probe"
    target = tmp_path / ".symlink_target"
    target.mkdir(exist_ok=True)
    try:
        probe.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    finally:
        try:
            if os.path.islink(probe):
                os.unlink(probe)
        except OSError:
            pass


def test_concurrent_renamer_zero_escapes(tmp_path, monkeypatch):
    """H-FS style: real renamer active during N writes, 0 escapes."""
    _require_symlinks(tmp_path)
    base = tmp_path / "ws"
    sub = base / "sub"
    parking = base / ".sub_parking"
    outside = tmp_path / "outside"
    (base / "sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "victim.txt").write_text("ORIGINAL")
    (sub / "victim.txt").write_text("inside-original")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    stop = threading.Event()

    def flipper():
        to_link = True
        while not stop.is_set():
            try:
                if to_link:
                    if os.path.islink(sub) or not os.path.exists(sub):
                        to_link = False
                        continue
                    if parking.exists():
                        to_link = False
                        continue
                    _swap_sub_to_outside(base, sub, parking, outside)
                else:
                    _restore_sub(base, sub, parking)
                to_link = not to_link
            except OSError:
                pass

    thread = threading.Thread(target=flipper, daemon=True)
    thread.start()
    ok = 0
    try:
        N = 120
        for i in range(N):
            try:
                asyncio.run(fmod.write_file("sub/victim.txt", f"ATTACKER-{i}"))
                ok += 1
            except Exception:
                pass  # rejections under an active swap are fail-closed, not escapes
    finally:
        stop.set()
        thread.join(timeout=10)
    _restore_sub(base, sub, parking)

    assert ok >= 1, "renamer must not block every write (vacuous test?)"
    # Zero escapes: the outside file is untouched and no temp litter leaked.
    assert (outside / "victim.txt").read_text() == "ORIGINAL"
    assert sorted(os.listdir(outside)) == ["victim.txt"]
    # Post-adversary plain write still lands a complete file (availability).
    assert "Successfully wrote" in asyncio.run(fmod.write_file("sub/victim.txt", "AFTER"))
    assert (sub / "victim.txt").read_text() == "AFTER"


def test_deterministic_swap_at_tmp_creation_stays_inside(tmp_path, monkeypatch):
    """Pin the exact claimed interleaving: swap lands between validation and
    temp creation (fired at the temp-open seam with real rename+symlink
    syscalls). The write must still succeed AND stay inside the workspace."""
    _require_symlinks(tmp_path)
    base = tmp_path / "ws"
    sub = base / "sub"
    parking = base / ".sub_parking"
    outside = tmp_path / "outside"
    (base / "sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "victim.txt").write_text("ORIGINAL")
    (sub / "victim.txt").write_text("inside-original")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    real_open = fmod._os_open
    state = {"swapped": False}

    def hook(path, flags, *args, **kwargs):
        name = path if isinstance(path, str) else getattr(path, "name", "")
        if (
            not state["swapped"]
            and kwargs.get("dir_fd") is not None
            and isinstance(name, str)
            and name.startswith(".victim.txt.")
            and name.endswith(".tmp")
        ):
            state["swapped"] = True
            _swap_sub_to_outside(base, sub, parking, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fmod, "_os_open", hook)
    try:
        result = asyncio.run(fmod.write_file("sub/victim.txt", "ATTACKER"))
    finally:
        _restore_sub(base, sub, parking)

    assert state["swapped"], "swap seam (temp creation) was never reached"
    assert "Successfully wrote" in result
    assert (outside / "victim.txt").read_text() == "ORIGINAL"
    assert sorted(os.listdir(outside)) == ["victim.txt"]
    assert (sub / "victim.txt").read_text() == "ATTACKER"


def test_normal_write_and_mode_preserved(tmp_path, monkeypatch):
    """No-adversary regression: create, overwrite, and mode preservation."""
    import stat as statmod

    base = tmp_path / "ws"
    base.mkdir()
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    assert "Successfully wrote" in asyncio.run(fmod.write_file("a/b/new.txt", "hello"))
    assert (base / "a" / "b" / "new.txt").read_text() == "hello"
    assert "Successfully wrote" in asyncio.run(fmod.write_file("a/b/new.txt", "world"))
    assert (base / "a" / "b" / "new.txt").read_text() == "world"

    os.chmod(base / "a" / "b" / "new.txt", 0o640)
    asyncio.run(fmod.write_file("a/b/new.txt", "again"))
    assert statmod.S_IMODE(os.stat(base / "a" / "b" / "new.txt").st_mode) == 0o640


def test_resolved_inside_symlink_parent_still_allowed(tmp_path, monkeypatch):
    """No behavior change: a pre-existing inside-base symlinked parent is
    transparent (resolve_path returns the resolved path), so the write lands
    inside and succeeds — anchoring must not break this legitimate case."""
    _require_symlinks(tmp_path)
    base = tmp_path / "ws"
    real = base / "real"
    real.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("ORIGINAL")
    try:
        (base / "link").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(fmod, "_BASE_DIR", base.resolve())

    assert "Successfully wrote" in asyncio.run(fmod.write_file("link/note.txt", "hello"))
    assert (real / "note.txt").read_text() == "hello"
    assert (outside / "victim.txt").read_text() == "ORIGINAL"
