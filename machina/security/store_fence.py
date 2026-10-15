"""Shared per-store fencing for subscription credential stores.

One authoritative implementation of the mechanics idiom established by
``chatgpt_auth`` and reused by the copilot store:

* ``store_lock`` — an exclusive OS file lock on a sibling ``.lock`` file
  (``flock`` on POSIX, ``msvcrt`` on Windows; same idiom as
  ``machina/interop.py``) that serializes whole read -> provider call ->
  publish sections across threads AND processes, so two concurrent refreshes
  can never replay the same single-use rotating token. Acquisition is bounded
  (``STORE_LOCK_TIMEOUT_SECONDS``): a lock held that long raises a typed
  ``StoreLockTimeout`` instead of blocking the caller forever.
* ``credential_generation`` / ``invalidate_credentials`` — a small durable
  counter next to the store; a logout bumps it and any refresh or import
  that captured an older value refuses to publish.
* ``store_fingerprint`` — a raw-content digest so a publish can detect an
  out-of-band store change (e.g. a removal) that did not go through the
  generation counter.
* ``fenced_logout`` — bump-then-unlink. Deliberately does NOT take
  ``store_lock``: refreshes hold that lock across the provider round trip,
  and a logout must not block behind a slow (or hung) token endpoint. The
  atomic bump lands first, so a refresh finishing afterwards observes it and
  leaves the store removed.

Store formats, token normalization and typed errors stay in each provider
module (``chatgpt_auth``, ``copilot_auth``); this module holds only the
fencing mechanics.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - platform branch
    import msvcrt
else:
    import fcntl

#: Bound on acquiring a store lock. A refresh holds the lock across one token
#: round trip (the auth modules' HTTP budget is at most 10 s connect + 20 s
#: read), so the bound waits a full round trip plus headroom before failing
#: typed. Never unbounded.
STORE_LOCK_TIMEOUT_SECONDS = 45.0
#: Delay between non-blocking acquisition attempts while a peer holds the lock.
_STORE_LOCK_POLL_SECONDS = 0.05


class StoreLockTimeout(TimeoutError):
    """A per-store lock was not acquired within ``STORE_LOCK_TIMEOUT_SECONDS``.

    The message names only the lock file, never credential material.
    """


_generation_lock = threading.Lock()
#: In-process fallback bumps for stores whose generation file is unwritable.
_generations: dict[str, int] = {}


def _store_target(path: str | Path) -> Path:
    """Resolved store identity used for lock files and generation counters."""
    target = Path(path)
    try:
        return target.expanduser().resolve()
    except (OSError, ValueError):
        return target.expanduser()


def _generation_path(target: Path) -> Path:
    return target.with_name(target.name + ".generation")


def _read_generation(target: Path) -> int:
    try:
        return int(_generation_path(target).read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        return 0


def credential_generation(path: str | Path) -> int:
    """Current authentication generation for a store.

    A login flow or refresh captures this before doing network work and
    refuses to publish when it changed: any logout in between invalidates the
    work. The durable component (a small file next to the store) fences work
    that spans OTHER processes; the in-process fallback covers stores whose
    directory is not writable when logout runs.
    """
    target = _store_target(path)
    with _generation_lock:
        local = _generations.get(str(target), 0)
    return _read_generation(target) + local


def invalidate_credentials(path: str | Path) -> int:
    """Bump a store's authentication generation; returns the new value.

    Called by logout paths before removing the store. Failing to persist the
    durable counter still fences every in-process caller.
    """
    target = _store_target(path)
    file = _generation_path(target)
    durable = _read_generation(target) + 1
    persisted = False
    try:
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=file.name + ".", suffix=".tmp", dir=str(file.parent))
        try:
            chmod_temp_file(fd, tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{durable}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, file)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
        persisted = True
    except OSError:
        durable = _read_generation(target)
    with _generation_lock:
        if not persisted:
            _generations[str(target)] = _generations.get(str(target), 0) + 1
        local = _generations.get(str(target), 0)
    return durable + local


@contextmanager
def store_lock(target: str | Path) -> Iterator[None]:
    """Exclusive per-store lock (same flock idiom as ``machina/interop.py``).

    Serializes whole read -> provider call -> publish sections across threads
    AND processes. The lock file holds no state and the atomic store replace
    keeps the store itself always readable. Acquisition is bounded by
    :data:`STORE_LOCK_TIMEOUT_SECONDS`: a lock held longer than that (a wedged
    or crashed-slow peer) raises :class:`StoreLockTimeout` instead of blocking
    forever.
    """
    lock_path = Path(f"{_store_target(target)}.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if _WINDOWS:  # pragma: no cover - platform branch
            # msvcrt locks a byte range from the current file position.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
        deadline = time.monotonic() + STORE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if _WINDOWS:  # pragma: no cover - platform branch
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreLockTimeout(
                        f"timed out after {STORE_LOCK_TIMEOUT_SECONDS:g}s waiting for the "
                        f"credential store lock ({lock_path}); another process is holding it"
                    ) from None
                time.sleep(min(_STORE_LOCK_POLL_SECONDS, remaining))
                continue
            acquired = True
            break
        yield
    finally:
        try:
            if acquired:
                if _WINDOWS:  # pragma: no cover - platform branch
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def store_fingerprint(target: str | Path) -> tuple[bool, bytes]:
    """(present, content digest) of the raw store; absent -> (False, b"").

    Used to detect an out-of-band change (e.g. logout removing the store
    while a refresh response is in flight) before publishing. Unreadable
    stores report present with an empty digest, so a change in readability
    still invalidates the snapshot instead of silently publishing over it.
    """
    try:
        return True, hashlib.sha256(Path(target).read_bytes()).digest()
    except FileNotFoundError:
        return False, b""
    except OSError:
        return True, b""


def chmod_temp_file(fd: int, tmp_name: str | Path, mode: int = 0o600) -> None:
    """chmod a fresh mkstemp path; close *fd* when chmod fails before fdopen.

    ``mkstemp`` hands back an open descriptor plus a path, and every store
    writer chmods the path before ``fdopen`` takes ownership of the
    descriptor. When that chmod raises, the descriptor would leak (``fdopen``
    never runs and the caller's ``except`` only unlinks the temp name), so
    close it here and reraise unchanged. The temp-name unlink stays with
    the caller. (R4-049b.)
    """
    try:
        os.chmod(tmp_name, mode)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        raise


def fenced_logout(path: str | Path) -> bool:
    """Remove a store and fence in-flight authentication work.

    The generation is bumped BEFORE the unlink, durably and atomically, so a
    refresh or import that captured the previous generation can no longer
    publish. Returns True when a store existed and was removed; ``OSError``
    from the unlink propagates (callers translate it to their own error
    surface).

    Deliberately does NOT acquire ``store_lock``: refreshes hold that lock
    across their token-endpoint call, so blocking here would wedge a logout
    behind a slow (or hung) provider round trip. Ordering the bump first is
    what fences; a refresh that raced the bump between its publish gate and
    its save re-checks afterwards and undoes its own write.
    """
    target = Path(path)
    invalidate_credentials(target)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
