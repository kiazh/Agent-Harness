"""Skill system — registry, SKILL.md parser, trigger matching, telemetry, curator, hub.

Injection scanning is BEST-EFFORT. :meth:`SkillParser._scan_injection`
normalizes common evasion encodings before matching the known injection
patterns: NFKC folding (fullwidth/compatibility forms), invisible characters
(zero-width joiners/spaces, soft hyphen, word joiner, BOM), homoglyph
lookalikes (Cyrillic/Greek), letter-spacing collapse, leetspeak digit/symbol
substitution, whitespace and split-word evasion, and unlabeled base64 blobs.
Novel encodings, paraphrase, and semantically equivalent attacks are not
guaranteed to be caught — this filter is one layer of defense, never a
security boundary. Skill installs still require human review elsewhere.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from machina.observability.diagnostics import record_failure
from machina.skills.ports import SkillSource

try:  # POSIX advisory locks for cross-process telemetry merges.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Defaults are anchored to the project root, not the current directory, so the
# same skills are found no matter where `machina` is launched. Override with env vars.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_INSTALL_BUILD_RE = re.compile(r"[a-z0-9_-]+\.install-[0-9]+-[0-9a-f]{8}")

# ─── best-effort injection-scan normalization ───────────────────────────────
# The scanner below is deliberately normalization-based, not semantic. These
# helpers collapse the encodings that defeat a raw regex: invisible separators,
# compatibility/fullwidth forms, homoglyphs, letter spacing, leetspeak, split
# words, and base64-wrapped payloads. None of it makes the scan complete.

_INVISIBLE_TRANSLATION = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u00ad\u2060\ufeff"), None
)

# Common Cyrillic/Greek lookalikes that NFKC leaves untouched. Applied after
# casefold(), so only the lowercase forms are needed.
_HOMOGLYPH_FOLD = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "в": "b",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "һ": "h",
        "ԛ": "q",
        "ԝ": "w",
        # Greek
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "σ": "s",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "μ": "m",
        "ω": "w",
    }
)

_LEET_FOLD = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)

# Runs of single alphanumerics separated by whitespace/punctuation, e.g.
# "i g n o r e" or "i-g-n-o-r-e". The joined text is scan-only.
_SPACED_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9][\s._\-]+){2,}[A-Za-z0-9](?![A-Za-z0-9])"
)
_SPACED_SEPARATOR_RE = re.compile(r"[\s._\-]+")
_WHITESPACE_RE = re.compile(r"\s+")
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _fold_for_scan(text: str) -> str:
    """Normalize compatibility forms, invisibles, homoglyphs, and leetspeak."""
    folded = unicodedata.normalize("NFKC", text)
    folded = folded.translate(_INVISIBLE_TRANSLATION)
    folded = folded.translate(_HOMOGLYPH_FOLD)
    folded = folded.translate(_LEET_FOLD)
    return folded.casefold()


def _collapse_spaced_letters(text: str) -> str:
    """Join runs of letter-spaced characters ("i g n o r e" -> "ignore")."""
    return _SPACED_RUN_RE.sub(lambda match: _SPACED_SEPARATOR_RE.sub("", match.group(0)), text)


def _decoded_base64_payloads(text: str) -> list[str]:
    """Decode unlabeled base64 blobs that yield printable ASCII text.

    Only blobs longer than 40 characters that are valid base64 and decode to
    printable ASCII are returned; everything else (keys, hashes, binaries) is
    ignored.
    """
    payloads: list[str] = []
    for match in _BASE64_BLOB_RE.finditer(text):
        blob = match.group(0)
        if len(blob) <= 40:
            continue
        candidate = blob + "=" * (-len(blob) % 4)
        try:
            data = base64.b64decode(candidate, validate=True)
        except ValueError:
            continue
        if len(data) < 8:
            continue
        try:
            payload = data.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not all(char.isprintable() or char in "\r\n\t" for char in payload):
            continue
        payloads.append(payload)
    return payloads


def default_skills_dir() -> Path:
    # Explicit override wins.
    override = os.environ.get("MACHINA_SKILLS_DIR")
    if override:
        return Path(override)
    # Editable checkout: sibling skills/ next to the repo root.
    candidate = _PROJECT_ROOT / "skills"
    try:
        if candidate.is_dir():
            return candidate
    except OSError:
        pass
    # AH-030: non-editable wheel / runtime image — skills are shipped as
    # package data (force-include). Resolve via importlib.resources when the
    # checkout directory is absent.
    try:
        from importlib.resources import files as _files

        pkg = _files("skills")
        # files() may return a Traversable; convert file-system packages.
        try:
            p = Path(str(pkg))
            if p.is_dir():
                return p
        except OSError:
            pass
    except Exception as _boundary_error:
        # Optional fallback preserves the primary outcome; report no payload.
        record_failure("registry.default_skills_dir", _boundary_error)
    # Docker runtime image copies ./skills next to /app.
    for fallback in (Path("/app/skills"), Path.cwd() / "skills"):
        try:
            if fallback.is_dir():
                return fallback
        except OSError:
            continue
    return candidate


def default_hub_dir() -> Path:
    return Path(os.environ.get("MACHINA_SKILL_HUB_DIR") or _PROJECT_ROOT / ".skill-hub")


@dataclass
class Skill:
    name: str
    description: str
    triggers: list[str]
    content: str
    file_path: str
    version: str = "1.0.0"
    enabled: bool = True
    usage_count: int = 0
    view_count: int = 0
    last_activity_at: datetime | None = None
    # Provenance fields
    source: str = ""  # Where the skill came from (file path, URL, "builtin", etc.)
    source_type: str = "local"  # "local", "url", "builtin", "hub", "learned"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


# Prompt-injection pattern sources. The compact variants replace `\s+` with
# `\s*` and are matched against whitespace-stripped text so split words
# ("ig nore", "ignoreprevious") are still caught.
_INJECTION_PATTERN_SOURCES = (
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?prior\s+instructions",
    r"forget\s+(all\s+)?previous\s+instructions",
    r"system\s*:\s*(you\s+are|ignore|disregard|forget|override)",
    r"<\s*system\s*>",
    r"reveal\s+(your\s+|the\s+)?(system|developer|hidden|secret)\s*(prompt|message|instruction)",
    r"bypass\s+(safety|safe|guardrail|filter|restriction)",
    r"jailbreak",
    r"exfiltrat\w*",
    r"system\s+prompt",
    r"developer\s+message",
    r"decode\s+base64|base64\s+decode|from\s*base64",
    r"override\s+(safety|guardrail|instruction|system)",
)
_INJECTION_PATTERNS = tuple(
    re.compile(source, re.IGNORECASE) for source in _INJECTION_PATTERN_SOURCES
)
_INJECTION_COMPACT_PATTERNS = tuple(
    re.compile(source.replace(r"\s+", r"\s*"), re.IGNORECASE)
    for source in _INJECTION_PATTERN_SOURCES
)


class SkillParser:
    """Parse SKILL.md files with YAML frontmatter."""

    # Maximum allowed content size (1 MB) to prevent memory exhaustion
    MAX_CONTENT_SIZE = 1_000_000

    # Patterns that may indicate prompt injection attempts. These are targeted
    # to reduce false positives on legitimate content, and the scan below is
    # documented as BEST-EFFORT: normalization catches common evasions, but it
    # cannot prove content is safe.
    _PROMPT_INJECTION_PATTERNS = _INJECTION_PATTERNS

    @staticmethod
    def _scan_injection(text: str) -> None:
        """Best-effort scan for known injection patterns in common encodings.

        The raw text and normalization variants (NFKC, invisibles stripped,
        homoglyph/leet folded, letter-spacing collapsed, whitespace removed,
        and decoded base64 payloads) are matched against the known patterns.
        Evasions outside this set, and paraphrase, are not guaranteed to be
        caught; callers must not treat a pass as proof of safety.
        """
        original = text or ""
        variants = [original]
        folded = _fold_for_scan(original)
        if folded != original:
            variants.append(folded)
        collapsed = _collapse_spaced_letters(folded)
        if collapsed != folded:
            variants.append(collapsed)
        variants.extend(
            compact
            for candidate in list(variants)
            if (compact := _WHITESPACE_RE.sub("", candidate)) and compact != candidate
        )
        for decoded in _decoded_base64_payloads(original):
            variants.append(decoded)
            variants.append(_fold_for_scan(decoded))
        for candidate in variants:
            compact = _WHITESPACE_RE.sub("", candidate)
            for pattern in _INJECTION_PATTERNS:
                if pattern.search(candidate):
                    raise ValueError(
                        f"Skill content contains potential prompt injection pattern "
                        f"(matched: {pattern.pattern[:30]}...)"
                    )
            for pattern in _INJECTION_COMPACT_PATTERNS:
                if pattern.search(compact):
                    raise ValueError(
                        f"Skill content contains potential prompt injection pattern "
                        f"(matched: {pattern.pattern[:30]}...)"
                    )

    @staticmethod
    def _validate_metadata(name: Any, description: Any, triggers: Any) -> None:
        """Reject malformed metadata before search or prompt catalog use."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}", name):
            raise ValueError(
                "skill name must be a short alphanumeric slug of at least 3 characters"
            )
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError("skill description must be a string of at most 1000 characters")
        if (
            not isinstance(triggers, list)
            or len(triggers) > 32
            or any(not isinstance(item, str) or not 0 < len(item) <= 100 for item in triggers)
        ):
            raise ValueError("skill triggers must be a list of short strings")

    @staticmethod
    def _validate_content(content: str) -> str:
        """Validate skill content for prompt injection attempts.

        Returns the content if safe, raises ValueError if suspicious patterns found.
        Also scans the text regardless of caller; description/triggers are
        scanned separately via _scan_injection by callers.
        """
        if len(content) > SkillParser.MAX_CONTENT_SIZE:
            raise ValueError(
                f"Skill content exceeds maximum size of {SkillParser.MAX_CONTENT_SIZE} characters"
            )

        SkillParser._scan_injection(content)

        return content

    @staticmethod
    def parse(file_path: str | Path) -> Skill:
        """Parse a SKILL.md file."""
        path = Path(file_path)
        text = path.read_text(encoding="utf-8")

        # Extract YAML frontmatter
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not frontmatter_match:
            content = SkillParser._validate_content(text)
            SkillParser._validate_metadata(path.parent.name, "", [])
            return Skill(
                name=path.parent.name,
                description="",
                triggers=[],
                content=content,
                file_path=str(path),
                created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            )

        yaml_text = frontmatter_match.group(1)
        content = SkillParser._validate_content(frontmatter_match.group(2).strip())

        # Parse YAML frontmatter with PyYAML
        try:
            metadata: dict[str, Any] = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            # Graceful fallback: log and use defaults
            logger.warning("Failed to parse YAML frontmatter in %s: %s", path, exc)
            metadata = {}

        if not isinstance(metadata, dict):
            raise ValueError("skill frontmatter must be a mapping")
        name = metadata.get("name", path.parent.name)
        description = metadata.get("description", "")
        triggers = metadata.get("triggers", [])
        SkillParser._validate_metadata(name, description, triggers)
        # Scan description + triggers as well as content for injection.
        if isinstance(description, str):
            SkillParser._scan_injection(description)
        if isinstance(triggers, list):
            for _t in triggers:
                if isinstance(_t, str):
                    SkillParser._scan_injection(_t)
        created_at = metadata.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                raise ValueError("skill created_at must be an ISO timestamp") from None
        if created_at is None:
            created_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if not isinstance(created_at, datetime):
            raise ValueError("skill created_at must be an ISO timestamp")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return Skill(
            name=name,
            description=description,
            triggers=triggers,
            content=content,
            file_path=str(path),
            version=metadata.get("version", "1.0.0"),
            enabled=metadata.get("enabled", True) is not False,
            source=metadata.get("source", ""),
            source_type=metadata.get("source_type", "local"),
            created_at=created_at,
        )


class SkillRegistry(SkillSource):
    """Load, match, and manage skills.

    Stream B seam: conforms to :class:`machina.skills.ports.SkillSource` (the
    narrow consumer-facing interface). Internals below are untouched.
    """

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self._include_ecc = skills_dir is None
        self._ecc_bundle = None
        self.skills_dir = Path(skills_dir) if skills_dir is not None else default_skills_dir()
        self._skills: dict[str, Skill] = {}
        self._telemetry_file = self.skills_dir / "telemetry.json"
        # ``load_all()`` is called on every skill tool call and by every CLI /
        # gateway skill handler. Re-reading and re-parsing every SKILL.md plus
        # telemetry.json each time is pure waste, so the parsed result is
        # cached and validated against a cheap on-disk signature. That keeps
        # reload semantics (out-of-band edits are still picked up) without
        # paying for a full parse on every call.
        self._signature_cache: tuple | None = None
        # Merge-on-write bookkeeping: counters this instance has already
        # accounted for on disk. New increments are the delta over the
        # baseline, so a concurrent instance's writes are never overwritten.
        self._telemetry_baseline: dict[str, tuple[int, int]] = {}
        self._telemetry_lock = threading.Lock()

    def _invalidate(self) -> None:
        """Mark the parsed skill cache stale so the next load_all() re-reads."""
        self._signature_cache = None

    def _signature(self) -> tuple:
        """Cheap fingerprint of the on-disk skill set used to validate the cache.

        Stats only — no file contents are read. A changed mtime or size on any
        SKILL.md, or on telemetry.json, invalidates the cached parse.
        """
        entries: list[tuple[str, int, int]] = []
        try:
            children = sorted(self.skills_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or _LEGACY_INSTALL_BUILD_RE.fullmatch(child.name):
                continue
            try:
                stat = (child / "SKILL.md").stat()
            except OSError:
                continue
            entries.append((child.name, stat.st_mtime_ns, stat.st_size))
        try:
            telemetry_stat = self._telemetry_file.stat()
            telemetry = (telemetry_stat.st_mtime_ns, telemetry_stat.st_size)
        except OSError:
            telemetry = None
        return (tuple(entries), telemetry)

    def _load_telemetry(self) -> None:
        """Load telemetry data from JSON file into skill objects."""
        if not self._telemetry_file.exists():
            return
        try:
            data = json.loads(self._telemetry_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for name, telemetry in data.items():
                if name in self._skills and isinstance(telemetry, dict):
                    skill = self._skills[name]
                    skill.usage_count = telemetry.get("usage_count", 0)
                    skill.view_count = telemetry.get("view_count", 0)
                    last_activity = telemetry.get("last_activity_at")
                    if last_activity:
                        skill.last_activity_at = datetime.fromisoformat(last_activity)
                    self._telemetry_baseline[name] = (skill.usage_count, skill.view_count)
        except (json.JSONDecodeError, OSError, ValueError):
            logger.warning("Failed to load telemetry", exc_info=True)

    @contextmanager
    def _telemetry_file_lock(self):
        """Serialize telemetry writers in-process and across processes."""
        lock_file = self._telemetry_file.with_name(self._telemetry_file.name + ".lock")
        with self._telemetry_lock:
            try:
                descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                descriptor = None
            try:
                if descriptor is not None and fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                if descriptor is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    @staticmethod
    def _nonneg(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _latest_activity(existing: Any, current: datetime | None) -> str | None:
        previous: datetime | None = None
        if isinstance(existing, str):
            try:
                previous = datetime.fromisoformat(existing)
            except ValueError:
                previous = None
        if previous is not None and previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if current is None:
            current = previous
        elif previous is not None and previous > current:
            current = previous
        return current.isoformat() if isinstance(current, datetime) else None

    def _save_telemetry(self) -> None:
        """Persist telemetry with merge-on-write under an advisory file lock.

        Two registries sharing a skills directory each hold their own in-memory
        counters. Under ``fcntl.flock`` this writer re-reads the latest file,
        adds only the increments accumulated since this instance last observed
        the file (its baseline), and atomically replaces the file via a
        same-directory temp file plus ``os.replace``. Concurrent increments are
        merged instead of lost, and a crash mid-write cannot truncate the live
        telemetry.
        """
        try:
            self._telemetry_file.parent.mkdir(parents=True, exist_ok=True)
            with self._telemetry_file_lock():
                self._write_merged_telemetry()
        except OSError:
            logger.warning("Failed to save telemetry", exc_info=True)
        if self._signature_cache is not None:
            # Only acknowledge our telemetry write, not concurrent source edits.
            self._signature_cache = (self._signature_cache[0], self._signature()[1])

    def _write_merged_telemetry(self) -> None:
        try:
            existing = json.loads(self._telemetry_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            existing = {}
        merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        next_baseline: dict[str, tuple[int, int]] = {}
        for name, skill in self._skills.items():
            base_usage, base_view = self._telemetry_baseline.get(name, (0, 0))
            prior = merged.get(name)
            prior = prior if isinstance(prior, dict) else {}
            merged[name] = {
                "usage_count": max(self._nonneg(prior.get("usage_count")), base_usage)
                + max(0, skill.usage_count - base_usage),
                "view_count": max(self._nonneg(prior.get("view_count")), base_view)
                + max(0, skill.view_count - base_view),
                "last_activity_at": self._latest_activity(
                    prior.get("last_activity_at"), skill.last_activity_at
                ),
            }
            next_baseline[name] = (skill.usage_count, skill.view_count)
        temp_file = self._telemetry_file.with_name(f"{self._telemetry_file.name}.{os.getpid()}.tmp")
        try:
            temp_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            with open(temp_file, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_file, self._telemetry_file)
        except OSError:
            try:
                temp_file.unlink()
            except OSError:
                pass
            raise
        self._telemetry_baseline.update(next_baseline)

    def load_all(self, *, refresh: bool = False) -> None:
        """Load all skills from the skills directory (reload semantics, cached).

        A single malformed or rejected SKILL.md (e.g. it trips the
        prompt-injection filter) is skipped with a warning rather than
        aborting the whole load.

        Repeated calls with no on-disk change are cheap no-ops: the parsed
        result is cached and validated against a stat-only signature, so
        out-of-band edits are still picked up. ``refresh=True`` forces a
        re-parse even when the signature matches.
        """
        signature = self._signature()
        if self._signature_cache is not None and signature == self._signature_cache and not refresh:
            return
        self._skills.clear()
        if self._include_ecc:
            if self._ecc_bundle is None:
                from machina.skills.bundle import ECCBundle

                self._ecc_bundle = ECCBundle()
            self._skills.update((skill.name, skill) for skill in self._ecc_bundle.skills())
        if self.skills_dir.exists():
            for skill_dir in sorted(self.skills_dir.iterdir()):
                if skill_dir.is_dir() and not _LEGACY_INSTALL_BUILD_RE.fullmatch(skill_dir.name):
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        try:
                            skill = SkillParser.parse(skill_file)
                        except (ValueError, OSError, UnicodeDecodeError) as e:
                            logger.warning("Skipping skill %s: %s", skill_file, e)
                            continue
                        existing = self._skills.get(skill.name.casefold())
                        if not existing or existing.source_type != "builtin-ecc":
                            # Local provenance cannot claim the verified bundle's trust.
                            if skill.source_type == "builtin-ecc":
                                skill.source_type = "local"
                            self._skills[skill.name] = skill
            self._load_telemetry()
        self._signature_cache = signature

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def match_triggers(self, query: str) -> list[Skill]:
        """Match query against skill triggers using whole-token boundaries.

        A trigger matches only when it appears as a complete token in the
        query, preventing false positives like trigger "cat" matching
        "concatenate". The boundaries are explicit lookarounds rather than
        ``\\b`` so triggers that do not end in a word character ("c++") match
        symmetrically: "use c++ now" matches and "c++builder" does not.
        """
        matched = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for trigger in skill.triggers:
                # Whole-token regex: neither flank may be a word character.
                pattern = re.compile(
                    r"(?<![A-Za-z0-9_])" + re.escape(trigger.lower()) + r"(?![A-Za-z0-9_])"
                )
                if pattern.search(query.lower()):
                    matched.append(skill)
                    break
        return matched

    def get_skill_content(self, name: str) -> str | None:
        """Get the full content of a skill (for injection into prompt)."""
        skill = self._skills.get(name)
        if skill and skill.source_type == "builtin-ecc":
            return self._ecc_bundle.instructions(skill)
        return skill.content if skill and skill.enabled else None

    # ─── Telemetry methods ─────────────────────────────────────────────────

    def record_use(self, name: str) -> None:
        """Record that a skill was used (triggered and content injected)."""
        skill = self._skills.get(name)
        if skill:
            skill.usage_count += 1
            skill.last_activity_at = datetime.now(UTC)
            self._save_telemetry()

    def record_view(self, name: str) -> None:
        """Record that a skill was viewed (listed or inspected)."""
        skill = self._skills.get(name)
        if skill:
            skill.view_count += 1
            skill.last_activity_at = datetime.now(UTC)
            self._save_telemetry()

    def get_telemetry(self, name: str) -> dict[str, Any] | None:
        """Get telemetry data for a skill."""
        skill = self._skills.get(name)
        if not skill:
            return None
        return {
            "name": skill.name,
            "usage_count": skill.usage_count,
            "view_count": skill.view_count,
            "last_activity_at": skill.last_activity_at.isoformat()
            if skill.last_activity_at
            else None,
            "source": skill.source,
            "source_type": skill.source_type,
        }

    def get_all_telemetry(self) -> list[dict[str, Any]]:
        """Get telemetry data for all skills."""
        return [self.get_telemetry(s.name) for s in self._skills.values()]

    # ─── Provenance methods ────────────────────────────────────────────────

    def get_provenance(self, name: str) -> dict[str, Any] | None:
        """Get provenance data for a skill."""
        skill = self._skills.get(name)
        if not skill:
            return None
        return {
            "name": skill.name,
            "source": skill.source,
            "source_type": skill.source_type,
            "file_path": skill.file_path,
            "created_at": skill.created_at.isoformat(),
            "version": skill.version,
        }

    def list_by_source(self, source_type: str) -> list[Skill]:
        """List skills filtered by source type."""
        return [s for s in self._skills.values() if s.source_type == source_type]

    # ─── Skill creation (for /learn) ───────────────────────────────────────

    def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        triggers: list[str] | None = None,
        source: str = "",
        source_type: str = "learned",
        version: str = "1.0.0",
    ) -> Skill:
        """Create a new skill and persist it to the skills directory."""
        SkillParser._validate_metadata(name, description, triggers or [])
        if self._include_ecc:
            self.load_all()
            existing = self._skills.get(name.casefold())
            if existing and existing.source_type == "builtin-ecc":
                raise ValueError(f"skill '{name}' is a bundled default")
        if source_type == "builtin-ecc":
            raise ValueError("builtin-ecc provenance is reserved for the verified bundle")
        # Validate content for prompt injection (content + description/triggers).
        content = SkillParser._validate_content(content)
        triggers = triggers or []
        SkillParser._validate_metadata(name, description, triggers)
        SkillParser._scan_injection(description)
        for _t in triggers:
            SkillParser._scan_injection(_t)

        # Sanitize name for directory
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        skill_dir = self.skills_dir / safe_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Build SKILL.md content
        created_at = datetime.now(UTC)
        frontmatter = {
            "name": name,
            "description": description,
            "triggers": triggers,
            "version": version,
            "enabled": True,
            "created_at": created_at.isoformat(),
            "source": source,
            "source_type": source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{content}\n"

        skill_file = skill_dir / "SKILL.md"
        # Exclusive creation is the final read-before-write guard when two
        # reviews (or a manual learn) target the same skill concurrently.
        try:
            with skill_file.open("x", encoding="utf-8") as output:
                output.write(skill_content)
        except FileExistsError:
            raise ValueError(f"skill '{name}' already exists") from None

        skill = Skill(
            name=name,
            description=description,
            triggers=triggers,
            content=content,
            file_path=str(skill_file),
            version=version,
            source=source,
            source_type=source_type,
            created_at=created_at,
        )
        self._skills[name] = skill
        self._invalidate()
        return skill

    def update_skill(
        self,
        name: str,
        description: str | None = None,
        content: str | None = None,
        triggers: list[str] | None = None,
    ) -> Skill | None:
        """Update an existing skill."""
        skill = self._skills.get(name)
        if not skill:
            return None
        if skill.source_type == "builtin-ecc":
            return None

        if description is not None:
            if not isinstance(description, str) or len(description) > 1000:
                raise ValueError("skill description must be a string of at most 1000 characters")
            SkillParser._scan_injection(description)
            skill.description = description
        if content is not None:
            skill.content = SkillParser._validate_content(content)
        if triggers is not None:
            for _t in triggers:
                SkillParser._scan_injection(_t)
            SkillParser._validate_metadata(skill.name, skill.description, triggers)
            skill.triggers = triggers

        # Rebuild SKILL.md
        frontmatter = {
            "name": skill.name,
            "description": skill.description,
            "triggers": skill.triggers,
            "version": skill.version,
            "enabled": skill.enabled,
            "created_at": skill.created_at.isoformat(),
            "source": skill.source,
            "source_type": skill.source_type,
        }
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True)
        skill_content = f"---\n{yaml_text}---\n{skill.content}\n"

        Path(skill.file_path).write_text(skill_content, encoding="utf-8")
        self._invalidate()
        return skill

    def set_enabled(self, name: str, enabled: bool) -> Skill | None:
        """Persist whether a skill is available for agent discovery and reading."""
        skill = self._skills.get(name)
        if skill is None:
            return None
        if skill.source_type == "builtin-ecc":
            return None
        skill.enabled = enabled
        return self.update_skill(name)

    def delete_skill(self, name: str) -> bool:
        """Delete a skill by name."""
        if self._skills.get(name) and self._skills[name].source_type == "builtin-ecc":
            return False
        skill = self._skills.pop(name, None)
        if skill:
            self._invalidate()
            skill_path = Path(skill.file_path)
            if skill_path.exists():
                skill_path.unlink()
            # Remove parent directory if empty
            parent = skill_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            return True
        return False

    @classmethod
    def reset(cls) -> None:
        """Reset the global SkillRegistry singleton to a fresh instance."""
        global skill_registry
        skill_registry = cls()


class SkillCurator:
    """Background maintenance for skills — archive stale, clean up unused."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def archive_stale(self, days: int = 30) -> list[str]:
        """Archive skills that haven't been used in *days* days."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        archived = []
        for skill in self.registry.list_skills():
            if skill.source_type == "builtin-ecc":
                continue
            if skill.last_activity_at is None:
                # Never used — check created_at
                if skill.created_at < cutoff:
                    self.registry.set_enabled(skill.name, False)
                    archived.append(skill.name)
            elif skill.last_activity_at < cutoff:
                self.registry.set_enabled(skill.name, False)
                archived.append(skill.name)
        return archived

    def get_stale_skills(self, days: int = 30) -> list[Skill]:
        """Get list of stale skills without modifying them."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stale = []
        for skill in self.registry.list_skills():
            if skill.source_type == "builtin-ecc":
                continue
            if skill.last_activity_at is None:
                if skill.created_at < cutoff:
                    stale.append(skill)
            elif skill.last_activity_at < cutoff:
                stale.append(skill)
        return stale

    def get_unused_skills(self) -> list[Skill]:
        """Get skills that have never been used."""
        return [s for s in self.registry.list_skills() if s.usage_count == 0]

    def get_top_skills(self, limit: int = 10) -> list[Skill]:
        """Get most frequently used skills."""
        skills = self.registry.list_skills()
        return sorted(skills, key=lambda s: s.usage_count, reverse=True)[:limit]

    def cleanup_unused(self, dry_run: bool = True) -> list[str]:
        """Remove skills that have never been used. Returns list of removed skill names."""
        unused = self.get_unused_skills()
        removed = []
        for skill in unused:
            if skill.source_type == "builtin-ecc":
                continue
            if not dry_run:
                self.registry.delete_skill(skill.name)
            removed.append(skill.name)
        return removed

    def get_health_report(self) -> dict[str, Any]:
        """Get a health report for the skill system."""
        skills = self.registry.list_skills()
        total = len(skills)
        enabled = sum(1 for s in skills if s.enabled)
        disabled = total - enabled
        never_used = sum(1 for s in skills if s.usage_count == 0)
        total_uses = sum(s.usage_count for s in skills)
        total_views = sum(s.view_count for s in skills)
        stale = len(self.get_stale_skills())

        return {
            "total_skills": total,
            "enabled": enabled,
            "disabled": disabled,
            "never_used": never_used,
            "total_uses": total_uses,
            "total_views": total_views,
            "stale_skills": stale,
            "top_skills": [
                {"name": s.name, "usage_count": s.usage_count} for s in self.get_top_skills(5)
            ],
        }


class SkillHub:
    """Community-curated skill sharing — publish, discover, install skills."""

    def __init__(self, registry: SkillRegistry, hub_dir: str | Path | None = None) -> None:
        self.registry = registry
        self.hub_dir = Path(hub_dir) if hub_dir is not None else default_hub_dir()
        self.hub_dir.mkdir(parents=True, exist_ok=True)

    def _hub_file(self, name: str) -> Path:
        """Return the hub JSON path for *name*, rejecting path traversal."""
        if (
            not name
            or any(sep in name for sep in ("/", "\\"))
            or name in (".", "..")
            or ".." in name
        ):
            raise ValueError(f"Invalid skill name: {name!r}")
        path = (self.hub_dir / f"{name}.json").resolve()
        if not path.is_relative_to(self.hub_dir.resolve()):
            raise ValueError(f"Invalid skill name: {name!r}")
        return path

    def publish(self, name: str, author: str = "", tags: list[str] | None = None) -> dict[str, Any]:
        """Publish a skill to the hub.

        Disabled skills have no readable content; publishing one would write a
        ``content: null`` entry that can never be installed, so it is refused
        up front.
        """
        skill = self.registry.get(name)
        if not skill:
            raise ValueError(f"Skill '{name}' not found")
        content = self.registry.get_skill_content(skill.name)
        if not isinstance(content, str) or not content:
            raise ValueError(
                f"Skill '{name}' is disabled or has no readable content; enable it before publishing"
            )

        hub_entry = {
            "name": skill.name,
            "description": skill.description,
            "triggers": skill.triggers,
            "content": content,
            "version": skill.version,
            "author": author,
            "tags": tags or [],
            "published_at": datetime.now(UTC).isoformat(),
            "usage_count": skill.usage_count,
            "view_count": skill.view_count,
        }

        hub_file = self._hub_file(name)
        hub_file.write_text(json.dumps(hub_entry, indent=2), encoding="utf-8")
        return hub_entry

    def list_hub(self) -> list[dict[str, Any]]:
        """List all skills available in the hub."""
        skills = []
        for f in self.hub_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                skills.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return skills

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search hub skills by name, description, or tags."""
        query_lower = query.lower()
        results = []
        for entry in self.list_hub():
            if (
                query_lower in entry.get("name", "").lower()
                or query_lower in entry.get("description", "").lower()
                or any(query_lower in t.lower() for t in entry.get("tags", []))
            ):
                results.append(entry)
        return results

    def install(self, name: str) -> Skill:
        """Install a skill from the hub into the local registry."""
        hub_file = self._hub_file(name)
        if not hub_file.exists():
            raise ValueError(f"Skill '{name}' not found in hub")

        data = json.loads(hub_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Skill '{name}' hub entry is not a JSON object")
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError(
                f"Skill '{name}' hub entry has no publishable content; publish it again"
            )
        return self.registry.create_skill(
            name=data["name"],
            description=data.get("description", ""),
            content=content,
            triggers=data.get("triggers", []),
            source=f"hub:{name}",
            source_type="hub",
            version=data.get("version", "1.0.0"),
        )

    def get_hub_telemetry(self) -> list[dict[str, Any]]:
        """Get telemetry for all hub skills."""
        return self.list_hub()


# Global registry
skill_registry = SkillRegistry()
