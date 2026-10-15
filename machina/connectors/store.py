"""Installation-local, non-secret MCP configuration.

Atomic JSON follows config.yaml's installation-local convention. An advisory
lock serializes workers; persisted generations invalidate worker-local catalogs.
Connection health remains local and is never persisted as proof of availability.
The file is installation-administrator owned: edit through management APIs to
bump its generation. Direct manual changes with a reused generation are outside
the cache-invalidation contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from machina.connectors.models import ConfigError, ConnectorConfig

_WINDOWS = os.name == "nt"
if _WINDOWS:
    import msvcrt
else:
    import fcntl

#: Bound on acquiring the connector-inventory write lock. Writes are short
#: (read + atomic replace); a peer holding the lock longer than this is treated
#: as unavailable instead of blocking the caller (or a worker's event loop)
#: without bound. POSIX ``flock`` offers no timeout, so acquisition is a
#: nonblocking poll up to this deadline; Windows ``LK_LOCK`` is already bounded
#: (~10s of 1s retries) and keeps its idiom.
STORE_LOCK_TIMEOUT_SECONDS = 10.0
#: Delay between nonblocking acquisition attempts while a peer holds the lock.
_STORE_LOCK_POLL_SECONDS = 0.05


class ConnectorRevisionChanged(ConfigError):
    """A conditional mutation was superseded by another writer."""


class ConnectorStoreBusy(ConfigError):
    """The connector inventory lock was not acquired within its bound.

    The message names only the lock file, never configuration material.
    """


class ConnectorStore:
    def __init__(
        self, path: Path | None = None, *, lock_timeout: float = STORE_LOCK_TIMEOUT_SECONDS
    ) -> None:
        self.path = Path(path) if path is not None else Path.home() / ".machina" / "mcp.json"
        self._lock_timeout = max(0.0, float(lock_timeout))

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = str(self.path) + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if _WINDOWS:
                # msvcrt locks a byte range from the current file position.
                # Each worker locks byte zero, including a newly created file.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                acquired = True
            else:
                # Nonblocking acquisition under a deadline: a held lock must
                # never park the calling thread (which may be the event loop)
                # indefinitely.
                deadline = time.monotonic() + self._lock_timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ConnectorStoreBusy(
                                "connector inventory lock busy; another process is "
                                f"holding {lock_path}"
                            ) from None
                        time.sleep(min(_STORE_LOCK_POLL_SECONDS, remaining))
                        continue
                    acquired = True
                    break
            yield
        finally:
            try:
                if acquired:
                    if _WINDOWS:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    async def configure_async(
        self, fields: dict[str, Any], *, expected_revision: int | None = None
    ) -> ConnectorConfig:
        """Persist a configuration without parking the event loop on the lock."""
        from machina.core.background_tasks import await_joined

        return await await_joined(
            asyncio.to_thread(self.configure, fields, expected_revision=expected_revision),
            capture_result=True,
        )

    async def remove_async(
        self, connector_id: str, *, expected_revision: int | None = None
    ) -> bool:
        """Remove a configuration without parking the event loop on the lock."""
        from machina.core.background_tasks import await_joined

        return await await_joined(
            asyncio.to_thread(self.remove, connector_id, expected_revision=expected_revision),
            capture_result=True,
        )

    def load(self) -> tuple[int, list[ConnectorConfig]]:
        # Atomic replacement makes read-only loading safe without creating files.
        if not self.path.exists():
            return 0, []
        try:
            if self.path.stat().st_size > 1_048_576:
                raise ConfigError("connector inventory exceeds size limit")
            data = json.loads(self.path.read_text())
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "generation", "configs", "revisions"}
                or data["version"] != 1
            ):
                raise ConfigError("unsupported connector inventory format")
            if type(data["generation"]) is not int or data["generation"] < 0:
                raise ConfigError("invalid connector inventory generation")
            if not isinstance(data["configs"], list) or not isinstance(data["revisions"], dict):
                raise ConfigError("invalid connector inventory")
            revisions = data["revisions"]
            if any(
                not isinstance(k, str) or type(v) is not int or v < 0 for k, v in revisions.items()
            ):
                raise ConfigError("invalid connector revisions")
            configs = [ConnectorConfig(**item).validate() for item in data["configs"]]
            if any(revisions.get(c.id) != c.revision for c in configs):
                raise ConfigError("connector revision mismatch")
            if len({c.id for c in configs}) != len(configs):
                raise ConfigError("duplicate connector identifiers")
            return data["generation"], configs
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ConfigError("cannot read connector inventory") from exc

    @staticmethod
    def validate_input(fields: dict[str, Any]) -> ConnectorConfig:
        if not isinstance(fields, dict):
            raise ConfigError("config must be an object")
        if fields.get("owner") or "revision" in fields:
            raise ConfigError("owner and revision are managed by the installation")
        if set(fields) - set(ConnectorConfig.__dataclass_fields__):
            raise ConfigError("unsupported connector fields")
        try:
            return ConnectorConfig(**fields).validate()
        except TypeError:
            raise ConfigError("invalid connector configuration") from None

    def configure(
        self, fields: dict[str, Any], *, expected_revision: int | None = None
    ) -> ConnectorConfig:
        config = self.validate_input(fields)
        with self._locked():
            generation, configs = self.load()
            if expected_revision is not None and not any(
                c.id == config.id and c.revision == expected_revision for c in configs
            ):
                raise ConnectorRevisionChanged("connector revision changed")
            revisions = self._revisions(configs)
            config.revision = revisions.get(config.id, 0) + 1
            revisions[config.id] = config.revision
            configs = [c for c in configs if c.id != config.id] + [config]
            self._write(generation + 1, configs, revisions)
        return config

    def remove(self, connector_id: str, *, expected_revision: int | None = None) -> bool:
        with self._locked():
            generation, configs = self.load()
            if expected_revision is not None and not any(
                c.id == connector_id and c.revision == expected_revision for c in configs
            ):
                raise ConnectorRevisionChanged("connector revision changed")
            if not any(c.id == connector_id for c in configs):
                return False
            revisions = self._revisions(configs)
            self._write(generation + 1, [c for c in configs if c.id != connector_id], revisions)
            return True

    def _revisions(self, configs):
        if self.path.exists():
            revisions = json.loads(self.path.read_text())["revisions"]
            if any(
                not isinstance(k, str) or type(v) is not int or v < 0 for k, v in revisions.items()
            ):
                raise ConfigError("invalid connector revisions")
            return revisions
        return {c.id: c.revision for c in configs}

    def _write(self, generation, configs, revisions):
        payload = json.dumps(
            {
                "version": 1,
                "generation": generation,
                "configs": [asdict(c) for c in sorted(configs, key=lambda c: c.id)],
                "revisions": revisions,
            },
            indent=2,
        )
        if len(payload.encode()) > 1_048_576:
            raise ConfigError("connector inventory exceeds size limit")
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".mcp-")
        try:
            with os.fdopen(fd, "w") as output:
                if not _WINDOWS:
                    os.fchmod(output.fileno(), 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            # Windows does not permit a directory handle through os.open.
            # File contents were fsynced before the atomic replacement.
            if not _WINDOWS:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
