"""Static complexity scoring from non-model signals only.

Inputs: the raw query, optional repo stats (``{"files": int, "loc": int}``),
and an optional failing-test/error count. Output levels:

- ``direct`` — answer inline, no tool fan-out (greetings, plain questions).
- ``single`` — one bounded workstream (small debug, single-file implement).
- ``subagents`` — fan-out justified (multi-file review, large-repo search).

Thresholds are the named constants below; tune them in this one place.
"""

import re

from machina.core.intent import classify_task

# --- Tunable thresholds (documented defaults) -------------------------------
SCORE_DIRECT_MAX = 0  # total <= 0 -> direct: only zero-signal turns skip tools.
SCORE_SUBAGENTS_MIN = 5  # total >= 5 -> subagents: needs 2+ strong signals.

FILE_WEIGHT = 1  # per explicitly named/mentioned file; linear but capped.
FILE_CAP = 3  # cap: beyond 3 files the marginal planning cost flattens.
ERROR_WEIGHT = 1  # per failing test/error; each failure adds diagnosis work.
ERROR_CAP = 4  # cap: failure count saturates, multi-file signals take over.
REPO_FILES_MEDIUM = 100  # +1: navigation overhead starts (~a real project).
REPO_FILES_LARGE = 500  # +2: parallel exploration pays off.
REPO_LOC_MEDIUM = 10000  # +1: single-read understanding gets unlikely.
REPO_LOC_LARGE = 50000  # +2: working set exceeds one linear pass.
REDISCOVERY_BONUS = 2  # location/structure asks need search before acting.
MULTI_FILE_BONUS = 3  # explicit cross-file scope is the fan-out trigger.
HEAVY_REWRITE_BONUS = 1  # refactor/migrate verbs add blast-radius planning.

_FILE_PATH = re.compile(r"[\w\-./]+\.\w+")
_FILE_MENTION = re.compile(r"\bfiles?\b", re.I)
_MULTI_FILE = re.compile(
    r"(?:multiple\s+files?|all\s+files?|every\s+file|across\b.{0,30}\b"
    r"(?:files?|repo|codebase)|entire\s+(?:repo|codebase|project)|"
    r"whole\s+(?:project|codebase|repo)|repo[\s-]?wide)",
    re.I,
)
_REDISCOVERY = re.compile(
    r"(?:where\s+(?:is|are)\b|\bfind\b|\blocate\b|\bsearch\b.{0,20}"
    r"(?:repo|codebase|files?)|list\s+(?:all\s+)?\w*\s*files?|"
    r"\bexplore\b|project\s+structure|directory\s+(?:layout|structure|listing)|"
    r"layout\s+of\s+this\s+repo)",
    re.I,
)
_HEAVY_REWRITE = re.compile(r"\b(?:refactor|migrate|redesign|rearchitect)\b", re.I)

_CLASS_WEIGHTS = {
    "debug": 2,
    "implement": 2,
    "review-readonly": 2,
    "learn-directory": 1,
    "explain": 0,
    "conversational": 0,
    "ambiguous": 0,
}


def _repo_counts(repo_stats) -> tuple[int, int]:
    if not isinstance(repo_stats, dict):
        return 0, 0
    files = repo_stats.get("files", repo_stats.get("file_count", repo_stats.get("num_files", 0)))
    loc = repo_stats.get("loc", repo_stats.get("lines", repo_stats.get("total_loc", 0)))
    try:
        return max(0, int(files)), max(0, int(loc))
    except (TypeError, ValueError):
        return 0, 0


def score_complexity(query: str, repo_stats=None, error_count: int = 0) -> dict:
    """Score *query* to ``{"level": ..., "reasons": [...], "score": int}``."""
    score = 0
    reasons: list[str] = []

    task_class = classify_task(query)
    weight = _CLASS_WEIGHTS.get(task_class, 0)
    if weight:
        score += weight
        reasons.append(f"task-class={task_class} (+{weight})")

    paths = len(_FILE_PATH.findall(query))
    mentions = len(_FILE_MENTION.findall(query))
    files_hit = min(paths + mentions, FILE_CAP)
    # Path tokens already contain the word "file" rarely; avoid double count
    # when the only match is a bare "files" word — it still counts once.
    if files_hit:
        score += files_hit * FILE_WEIGHT
        reasons.append(f"files-mentioned={files_hit} (+{files_hit * FILE_WEIGHT})")

    try:
        errors = max(0, int(error_count))
    except (TypeError, ValueError):
        errors = 0
    errors_hit = min(errors, ERROR_CAP)
    if errors_hit:
        score += errors_hit * ERROR_WEIGHT
        reasons.append(f"errors={errors} (+{errors_hit * ERROR_WEIGHT})")

    n_files, loc = _repo_counts(repo_stats)
    if n_files >= REPO_FILES_LARGE:
        score += 2
        reasons.append(f"repo-files={n_files} (+2)")
    elif n_files >= REPO_FILES_MEDIUM:
        score += 1
        reasons.append(f"repo-files={n_files} (+1)")
    if loc >= REPO_LOC_LARGE:
        score += 2
        reasons.append(f"repo-loc={loc} (+2)")
    elif loc >= REPO_LOC_MEDIUM:
        score += 1
        reasons.append(f"repo-loc={loc} (+1)")

    if _REDISCOVERY.search(query):
        score += REDISCOVERY_BONUS
        reasons.append(f"rediscovery-markers (+{REDISCOVERY_BONUS})")
    if _MULTI_FILE.search(query):
        score += MULTI_FILE_BONUS
        reasons.append(f"multi-file-scope (+{MULTI_FILE_BONUS})")
    if _HEAVY_REWRITE.search(query):
        score += HEAVY_REWRITE_BONUS
        reasons.append(f"heavy-rewrite-verb (+{HEAVY_REWRITE_BONUS})")

    if not reasons:
        reasons.append("no-complexity-signals (+0)")
    if score <= SCORE_DIRECT_MAX:
        level = "direct"
    elif score >= SCORE_SUBAGENTS_MIN:
        level = "subagents"
    else:
        level = "single"
    return {"level": level, "reasons": reasons, "score": score}
