"""Gemini client with structured output, a hard call cap, a disk cache, bounded retries and a log.

Usage:

    from alibi.llm import GeminiClient, CallBudget
    from alibi.llm.config import FLASH
    client = GeminiClient()
    result = client.generate(FLASH, system_prompt, user_text, schema, CallBudget(4))
    result.data  # dict that conforms to `schema`

Every real call spends one unit of the process-wide cap (ALIBI_LLM_CAP, default 120)
and one unit of the optional per-task CallBudget. Cache hits spend nothing. The key is
read from GEMINI_API_KEY, falling back to the repo-root .env file. Nothing here ever
logs the key, the system prompt, the user text or the model's answer: the log carries
hashes and token counts only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from alibi.llm import config
from alibi.llm.schema import validate


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base class for every typed failure raised by this module."""


class BudgetExceeded(LLMError):
    """The process-wide cap or the per-task CallBudget has no calls left."""


class Unavailable(LLMError):
    """Gemini kept answering 429/503 after the allowed retries, or the transport failed."""


class SchemaValidationError(LLMError):
    """The model's answer was not valid JSON or did not conform to the requested schema."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class MissingApiKey(LLMError):
    """GEMINI_API_KEY is neither in the environment nor in the repo-root .env."""


# --------------------------------------------------------------------------- budgets


class CallBudget:
    """A counter of real calls. Thread-safe. `spend()` raises BudgetExceeded at zero."""

    def __init__(self, cap: int, name: str = "task"):
        self.cap = int(cap)
        self.name = name
        self.used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    def spend(self) -> None:
        with self._lock:
            if self.used >= self.cap:
                raise BudgetExceeded(f"{self.name} budget of {self.cap} Gemini calls is spent")
            self.used += 1

    def refund(self) -> None:
        with self._lock:
            self.used = max(self.used - 1, 0)

    def reset(self, cap: Optional[int] = None) -> None:
        with self._lock:
            if cap is not None:
                self.cap = int(cap)
            self.used = 0


#: The one hard cap shared by every GeminiClient in this process.
process_budget = CallBudget(config.call_cap(), name="process")


# --------------------------------------------------------------------------- results


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_metadata(cls, meta: Any) -> "Usage":
        if meta is None:
            return cls()

        def get(name: str) -> int:
            return int(getattr(meta, name, None) or 0)

        return cls(
            prompt_tokens=get("prompt_token_count"),
            output_tokens=get("candidates_token_count"),
            thinking_tokens=get("thoughts_token_count"),
            total_tokens=get("total_token_count"),
        )


@dataclass
class Result:
    data: Any
    model: str
    prompt_sha256: str
    cached: bool
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1


# --------------------------------------------------------------------------- transport


@dataclass
class RawResponse:
    """What a transport returns: the answer text plus whatever usage metadata it has."""

    text: Optional[str]
    usage_metadata: Any = None


Transport = Callable[[str, str, str, dict, str], RawResponse]
"""transport(model, system, user, json_schema, thinking_level) -> RawResponse.

It raises google.genai.errors.APIError (or any exception with an int `.code`) on HTTP
failures; the client retries codes in config.RETRYABLE_STATUS.
"""


def load_api_key(env_file: Optional[Path] = None) -> str:
    """Return the key from the environment, or from the repo-root .env, without printing it."""
    key = os.environ.get(config.API_KEY_ENV)
    if key:
        return key
    env_file = env_file or (config.REPO_ROOT / ".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == config.API_KEY_ENV:
                value = value.strip().strip("'\"")
                if value:
                    os.environ[config.API_KEY_ENV] = value
                    return value
    raise MissingApiKey(f"set {config.API_KEY_ENV} in the environment or in {env_file}")


def gemini_transport(api_key: Optional[str] = None) -> Transport:
    """The real transport over google-genai 3.0. Built lazily so tests never import it."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key or load_api_key())
    levels = {
        "minimal": types.ThinkingLevel.MINIMAL,
        "low": types.ThinkingLevel.LOW,
        "medium": types.ThinkingLevel.MEDIUM,
        "high": types.ThinkingLevel.HIGH,
    }

    def call(model: str, system: str, user: str, json_schema: dict, thinking: str) -> RawResponse:
        # Temperature is deliberately never set: the frontier model must run at its
        # default, and we keep Flash on the same footing so runs are comparable.
        cfg = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=json_schema,
            thinking_config=types.ThinkingConfig(thinking_level=levels[thinking]),
        )
        resp = client.models.generate_content(model=model, contents=user, config=cfg)
        return RawResponse(text=resp.text, usage_metadata=resp.usage_metadata)

    return call


# --------------------------------------------------------------------------- client


def prompt_sha256(model: str, system: str, user: str, json_schema: dict) -> str:
    """Cache and log key. The schema is serialised canonically so key order does not matter."""
    schema_text = json.dumps(json_schema, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    for part in (model, system, user, schema_text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class GeminiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        transport: Optional[Transport] = None,
        cache_dir: Optional[Path] = None,
        log_path: Optional[Path] = None,
        budget: Optional[CallBudget] = None,
        sleep: Callable[[float], None] = time.sleep,
        use_cache: bool = True,
    ):
        self._api_key = api_key
        self._transport = transport
        self.cache_dir = Path(cache_dir) if cache_dir else config.cache_dir()
        self.log_path = Path(log_path) if log_path else config.log_path()
        self.process_budget = budget or process_budget
        self._sleep = sleep
        self.use_cache = use_cache
        self._lock = threading.Lock()

    # ---- transport is created on first real call, so tests and cache-only runs need no key
    def _call(self) -> Transport:
        if self._transport is None:
            self._transport = gemini_transport(self._api_key)
        return self._transport

    # ---- public API
    def generate(
        self,
        model: str,
        system: str,
        user: str,
        json_schema: dict,
        budget: Optional[CallBudget] = None,
    ) -> Result:
        """Ask `model` for JSON conforming to `json_schema`.

        Raises BudgetExceeded, Unavailable or SchemaValidationError. A successful answer
        is cached on disk and returned from the cache on the next identical call.
        """
        key = prompt_sha256(model, system, user, json_schema)
        thinking = config.thinking_level(model)

        cached = self._read_cache(key) if self.use_cache else None
        if cached is not None:
            usage = Usage(**cached.get("usage", {}))
            self._log(model, key, usage, cached=True, outcome="cache_hit")
            return Result(data=cached["data"], model=model, prompt_sha256=key, cached=True, usage=usage, attempts=0)

        # Spend both caps before touching the network. Refund the first if the second is empty.
        try:
            self.process_budget.spend()
        except BudgetExceeded:
            self._log(model, key, Usage(), cached=False, outcome="budget_exceeded")
            raise
        if budget is not None:
            try:
                budget.spend()
            except BudgetExceeded:
                self.process_budget.refund()
                self._log(model, key, Usage(), cached=False, outcome="budget_exceeded")
                raise

        raw, attempts = self._with_retries(model, system, user, json_schema, thinking, key)
        usage = Usage.from_metadata(raw.usage_metadata)

        data = self._parse_and_validate(raw.text, json_schema, model, key, usage)
        if self.use_cache:
            self._write_cache(key, model, data, usage)
        self._log(model, key, usage, cached=False, outcome="ok", attempts=attempts)
        return Result(data=data, model=model, prompt_sha256=key, cached=False, usage=usage, attempts=attempts)

    # ---- pieces
    def _with_retries(self, model, system, user, json_schema, thinking, key) -> tuple[RawResponse, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._call()(model, system, user, json_schema, thinking), attempts
            except Exception as exc:  # noqa: BLE001 - classified by status code below
                code = getattr(exc, "code", None)
                if isinstance(code, str) and code.isdigit():
                    code = int(code)
                retry_index = attempts - 1
                if code in config.RETRYABLE_STATUS and retry_index < len(config.RETRY_BACKOFF_SECONDS):
                    self._sleep(config.RETRY_BACKOFF_SECONDS[retry_index])
                    continue
                self._log(model, key, Usage(), cached=False, outcome="unavailable", attempts=attempts)
                reason = f"status {code}" if code is not None else type(exc).__name__
                raise Unavailable(f"{model} unavailable after {attempts} attempt(s): {reason}") from exc

    def _parse_and_validate(self, text: Optional[str], json_schema: dict, model: str, key: str, usage: Usage) -> Any:
        if not text or not text.strip():
            self._log(model, key, usage, cached=False, outcome="schema_invalid")
            raise SchemaValidationError(["$: empty response"])
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._log(model, key, usage, cached=False, outcome="schema_invalid")
            raise SchemaValidationError([f"$: response is not JSON ({exc.msg})"]) from None
        problems = validate(data, json_schema)
        if problems:
            self._log(model, key, usage, cached=False, outcome="schema_invalid")
            raise SchemaValidationError(problems)
        return data

    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Optional[dict]:
        path = self._cache_file(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, model: str, data: Any, usage: Usage) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"model": model, "data": data, "usage": asdict(usage), "written_at": _now()}
        tmp = self._cache_file(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._cache_file(key))

    def _log(self, model: str, key: str, usage: Usage, *, cached: bool, outcome: str, attempts: int = 1) -> None:
        entry = {
            "time": _now(),
            "model": model,
            "prompt_sha256": key,
            "usage": asdict(usage),
            "cached": cached,
            "outcome": outcome,
            "attempts": attempts,
        }
        with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
