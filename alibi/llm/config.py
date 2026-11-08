"""Model names, thinking levels, call cap and on-disk locations for the Gemini client.

Both model ids were confirmed available on the team key on 2026-11-07 (see
alibi/REPORTS/S3a.md). Change them here only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

FLASH = "gemini-3.8-flash"
FRONTIER = "gemini-4-argon"

#: Default thinking level per model. Any other model id falls back to DEFAULT_THINKING.
THINKING_LEVELS: dict[str, str] = {FLASH: "low", FRONTIER: "medium"}
DEFAULT_THINKING = "low"

#: Process-wide hard cap on real Gemini calls. Cached answers do not count.
CAP_ENV = "ALIBI_LLM_CAP"
DEFAULT_CAP = 120

#: Retry policy for 429 and 503: at most two retries, with these waits in seconds.
RETRY_BACKOFF_SECONDS = (2.0, 6.0)
RETRYABLE_STATUS = (429, 503)

API_KEY_ENV = "GEMINI_API_KEY"

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Where generated, git-ignored files go. Override with ALIBI_DATA_DIR."""
    return Path(os.environ.get("ALIBI_DATA_DIR") or REPO_ROOT / "data" / "alibi")


def cache_dir() -> Path:
    return data_dir() / "llm_cache"


def log_path() -> Path:
    """The default, shared log. Scored runs use `fresh_log_path` instead."""
    return data_dir() / "llm_log.jsonl"


def logs_dir() -> Path:
    """Where per-system, per-run logs go (see `fresh_log_path`)."""
    return data_dir() / "llm_logs"


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _file_token(label: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    token = _UNSAFE.sub("-", value.strip()).strip("-.")
    if not token:
        raise ValueError(f"{label} {value!r} has no filename-safe characters")
    return token


def fresh_log_path(system: str, run_id: str, *, overwrite: bool = False) -> Path:
    """One empty log file for one scored system and run: `llm_logs/<system>_<run_id>.jsonl`.

    `system` is the scored system (`rules`, `gemini_onepass`, `alibi`) and `run_id` the
    evaluation run. Characters outside `[A-Za-z0-9_.-]` become `-` in the file name. The
    parent directories are created and the file is created empty. An existing non-empty
    file is a previous run's evidence, so it raises FileExistsError unless `overwrite=True`.
    Pass the result as `GeminiClient(log_path=...)` (or use `GeminiClient.for_run`).
    """
    path = logs_dir() / f"{_file_token('system', system)}_{_file_token('run_id', run_id)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0 and not overwrite:
        raise FileExistsError(f"{path} already holds a run's log; pass overwrite=True to replace it")
    path.write_text("", encoding="utf-8")
    return path


def call_cap() -> int:
    raw = os.environ.get(CAP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_CAP
    return int(raw)


def thinking_level(model: str) -> str:
    return THINKING_LEVELS.get(model, DEFAULT_THINKING)
