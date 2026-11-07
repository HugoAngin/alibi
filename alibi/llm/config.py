"""Model names, thinking levels, call cap and on-disk locations for the Gemini client.

Both model ids were confirmed available on the team key on 2026-11-07 (see
alibi/REPORTS/S3a.md). Change them here only.
"""

from __future__ import annotations

import os
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
    return data_dir() / "llm_log.jsonl"


def call_cap() -> int:
    raw = os.environ.get(CAP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_CAP
    return int(raw)


def thinking_level(model: str) -> str:
    return THINKING_LEVELS.get(model, DEFAULT_THINKING)
