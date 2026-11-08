"""Gemini client with structured output, a hard call cap, a disk cache, bounded retries and a log.

Usage:

    from alibi.llm import GeminiClient, CallBudget
    from alibi.llm.config import FLASH
    client = GeminiClient()
    result = client.generate(FLASH, system_prompt, user_text, schema, CallBudget(4))
    result.data  # dict that conforms to `schema`

Every transport attempt, retries included, spends one unit of the process-wide cap
(ALIBI_LLM_CAP, default 120) and one unit of the optional per-task CallBudget, so a cap of
one means exactly one attempt. Cache hits spend nothing, and a cached answer is checked
against the active schema again before it is returned. The key is read from
GEMINI_API_KEY, falling back to the repo-root .env file. Nothing here ever logs the key,
the system prompt, the user text or the model's answer: the log carries hashes and token
counts only.

Log rows
--------
Each `generate` appends one JSON object per line to `client.log_path` (default
`data/alibi/llm_log.jsonl`; a scored run uses `config.fresh_log_path(system, run_id)` or
`GeminiClient.for_run(...)` so each system and run has its own file). Fields:

    time           ISO-8601 UTC, seconds
    model          model id as passed to generate
    prompt_sha256  SHA-256 over model, system prompt, user text and canonical schema
    usage          {prompt_tokens, output_tokens, thinking_tokens, total_tokens} from the
                   API response (all zero when the response carried none)
    cached         true only for outcome cache_hit
    outcome        ok | cache_hit | budget_exceeded | unavailable | schema_invalid
    attempts       transport attempts this row covers (1 for cache_hit and budget_exceeded)
    case_number    int or null: the evaluation case the call served
    system         string or null: rules | gemini_onepass | alibi, the scored system
    run_id         string or null: the evaluation run

The last three come from the client's scope, set at construction or per case with
`client.scoped(case_number=..., system=..., run_id=...)`. Rows with a null case number are
reported as unscoped by `agents.eval.llm_cost`; never zero.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from alibi.llm import config
from alibi.llm.schema import validate


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base class for every typed failure raised by this module."""


class BudgetExceeded(LLMError):
    """The process-wide cap or the per-task CallBudget has no calls left."""


class Unavailable(LLMError):
    """Gemini kept answering 429/503 after the allowed retries, the budget allowed no further
    retry, or the transport failed."""


class SchemaValidationError(LLMError):
    """The model's answer was not valid JSON or did not conform to the requested schema.

    `usage` is what the billed call consumed, so callers can count the tokens of an answer
    they could not use (all zero when the response carried no usage metadata).
    """

    def __init__(self, problems: list[str], usage: Optional["Usage"] = None):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.usage = usage if usage is not None else Usage()


class MissingApiKey(LLMError):
    """GEMINI_API_KEY is neither in the environment nor in the repo-root .env."""


def clip_trace(data: Any, json_schema: dict) -> Any:
    """Cut each top-level ``trace`` step to the schema's own ``maxLength`` before validation.

    Output shaping only: the trace is free-text narration of what was checked (only the
    Challenger has one), and no status, reference or amount is read from it. A step the model
    wrote past the limit is cut at the limit instead of failing the whole response. The limit
    comes from the schema and is never raised; nothing outside ``trace`` is touched.
    """
    if not isinstance(data, dict) or not isinstance(data.get("trace"), list):
        return data
    spec = (json_schema.get("properties") or {}).get("trace") if isinstance(json_schema, dict) else None
    items = spec.get("items") if isinstance(spec, dict) else None
    limit = items.get("maxLength") if isinstance(items, dict) else None
    if not isinstance(limit, int) or limit < 1:
        return data
    data["trace"] = [step[:limit] if isinstance(step, str) and len(step) > limit else step
                     for step in data["trace"]]
    return data


# --------------------------------------------------------------------------- budgets


class CallBudget:
    """A counter of transport attempts. Thread-safe. `spend()` raises BudgetExceeded at zero."""

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

    @classmethod
    def from_dict(cls, raw: Any) -> "Usage":
        """Rebuild from a cache entry, ignoring unknown or malformed fields."""
        if not isinstance(raw, dict):
            return cls()
        values = {}
        for f in fields(cls):
            v = raw.get(f.name, 0)
            values[f.name] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
        return cls(**values)


@dataclass
class Result:
    data: Any
    model: str
    prompt_sha256: str
    cached: bool
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1


@dataclass(frozen=True)
class Scope:
    """What a log row is attributed to: the evaluation case, the scored system and the run."""

    case_number: Optional[int] = None
    system: Optional[str] = None
    run_id: Optional[str] = None

    def __post_init__(self) -> None:
        cn = self.case_number
        if cn is not None and (not isinstance(cn, int) or isinstance(cn, bool)):
            raise TypeError(f"case_number must be an int or None, got {type(cn).__name__}")
        for name in ("system", "run_id"):
            v = getattr(self, name)
            if v is not None and not isinstance(v, str):
                raise TypeError(f"{name} must be a str or None, got {type(v).__name__}")


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


# --------------------------------------------------------------------------- parsing


def _reject_constant(token: str) -> Any:
    # json.loads only calls parse_constant for NaN, Infinity and -Infinity, none of which
    # is JSON. Refusing here keeps them out of Result.data and out of the cache.
    raise ValueError(f"non-finite number {token} is not JSON")


def parse_json(text: str) -> Any:
    """`json.loads` that refuses NaN and Infinity instead of producing Python floats for them."""
    return json.loads(text, parse_constant=_reject_constant)


def _has_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_nonfinite(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_nonfinite(v) for v in value)
    return False


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
        scope: Optional[Scope] = None,
    ):
        self._api_key = api_key
        self._transport = transport
        self.cache_dir = Path(cache_dir) if cache_dir else config.cache_dir()
        self.log_path = Path(log_path) if log_path else config.log_path()
        self.process_budget = budget or process_budget
        self._sleep = sleep
        self.use_cache = use_cache
        self._scope = scope if scope is not None else Scope()
        self._lock = threading.Lock()

    @classmethod
    def for_run(cls, system: str, run_id: str, *, case_number: Optional[int] = None,
                overwrite_log: bool = False, **kwargs: Any) -> "GeminiClient":
        """A client for one scored run: a fresh log at `config.fresh_log_path(system, run_id)`
        and every row scoped to `system` and `run_id` (and `case_number`, if given now)."""
        if "log_path" in kwargs:
            raise TypeError("for_run chooses the log path; pass system and run_id only")
        kwargs["log_path"] = config.fresh_log_path(system, run_id, overwrite=overwrite_log)
        kwargs["scope"] = Scope(case_number=case_number, system=system, run_id=run_id)
        return cls(**kwargs)

    # ---- scope: what log rows are attributed to
    @property
    def scope(self) -> Scope:
        return self._scope

    def set_scope(self, *, case_number: Any = ..., system: Any = ..., run_id: Any = ...) -> Scope:
        """Replace the given scope fields (keeping the others) for every row logged from now on.
        Pass None to clear a field. Returns the previous scope."""
        previous = self._scope
        with self._lock:
            self._scope = Scope(
                case_number=previous.case_number if case_number is ... else case_number,
                system=previous.system if system is ... else system,
                run_id=previous.run_id if run_id is ... else run_id,
            )
        return previous

    @contextmanager
    def scoped(self, *, case_number: Any = ..., system: Any = ..., run_id: Any = ...) -> Iterator["GeminiClient"]:
        """`with client.scoped(case_number=7, system="alibi", run_id=rid): ...` tags every row
        logged inside the block; the previous scope is restored on exit. Not for use from
        several threads at once, since the scope is one field on the client."""
        previous = self.set_scope(case_number=case_number, system=system, run_id=run_id)
        try:
            yield self
        finally:
            with self._lock:
                self._scope = previous

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
        is cached on disk and returned from the cache on the next identical call, after
        being checked against `json_schema` again; an entry that no longer conforms is
        ignored and the model is asked afresh.
        """
        key = prompt_sha256(model, system, user, json_schema)
        thinking = config.thinking_level(model)

        cached = self._read_cache(key, json_schema) if self.use_cache else None
        if cached is not None:
            usage = Usage.from_dict(cached.get("usage"))
            self._log(model, key, usage, cached=True, outcome="cache_hit")
            return Result(data=cached["data"], model=model, prompt_sha256=key, cached=True, usage=usage, attempts=0)

        # Spend both caps before touching the network; every retry spends them again.
        self._spend(budget, model, key, log_refusal=True)

        raw, attempts = self._with_retries(model, system, user, json_schema, thinking, key, budget)
        usage = Usage.from_metadata(raw.usage_metadata)

        data = self._parse_and_validate(raw.text, json_schema, model, key, usage, attempts)
        if self.use_cache:
            self._write_cache(key, model, data, usage)
        self._log(model, key, usage, cached=False, outcome="ok", attempts=attempts)
        return Result(data=data, model=model, prompt_sha256=key, cached=False, usage=usage, attempts=attempts)

    # ---- pieces
    def _spend(self, budget: Optional[CallBudget], model: str, key: str, *, log_refusal: bool) -> None:
        """Take one attempt from the process cap and the task budget, or neither."""
        try:
            self.process_budget.spend()
        except BudgetExceeded:
            if log_refusal:
                self._log(model, key, Usage(), cached=False, outcome="budget_exceeded")
            raise
        if budget is not None:
            try:
                budget.spend()
            except BudgetExceeded:
                self.process_budget.refund()
                if log_refusal:
                    self._log(model, key, Usage(), cached=False, outcome="budget_exceeded")
                raise

    def _with_retries(self, model, system, user, json_schema, thinking, key, budget) -> tuple[RawResponse, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._call()(model, system, user, json_schema, thinking), attempts
            except Exception as exc:  # noqa: BLE001 - classified by status code below
                code = getattr(exc, "code", None)
                if isinstance(code, str) and code.isdigit():
                    code = int(code)
                reason = f"status {code}" if code is not None else type(exc).__name__
                retry_index = attempts - 1
                if code not in config.RETRYABLE_STATUS or retry_index >= len(config.RETRY_BACKOFF_SECONDS):
                    self._log(model, key, Usage(), cached=False, outcome="unavailable", attempts=attempts)
                    raise Unavailable(f"{model} unavailable after {attempts} attempt(s): {reason}") from exc
                # A retry is another billed attempt: it needs its own unit of both budgets.
                try:
                    self._spend(budget, model, key, log_refusal=False)
                except BudgetExceeded as spent:
                    self._log(model, key, Usage(), cached=False, outcome="unavailable", attempts=attempts)
                    raise Unavailable(
                        f"{model} unavailable after {attempts} attempt(s): {reason}; "
                        f"no budget left to retry ({spent})"
                    ) from exc
                self._sleep(config.RETRY_BACKOFF_SECONDS[retry_index])

    def _parse_and_validate(self, text: Optional[str], json_schema: dict, model: str, key: str,
                            usage: Usage, attempts: int = 1) -> Any:
        if not text or not text.strip():
            self._log(model, key, usage, cached=False, outcome="schema_invalid", attempts=attempts)
            raise SchemaValidationError(["$: empty response"], usage)
        try:
            data = parse_json(text)
        except json.JSONDecodeError as exc:
            self._log(model, key, usage, cached=False, outcome="schema_invalid", attempts=attempts)
            raise SchemaValidationError([f"$: response is not JSON ({exc.msg})"], usage) from None
        except ValueError as exc:  # parse_constant refused NaN or Infinity
            self._log(model, key, usage, cached=False, outcome="schema_invalid", attempts=attempts)
            raise SchemaValidationError([f"$: response is not JSON ({exc})"], usage) from None
        data = clip_trace(data, json_schema)
        problems = validate(data, json_schema)
        if problems:
            self._log(model, key, usage, cached=False, outcome="schema_invalid", attempts=attempts)
            raise SchemaValidationError(problems, usage)
        return data

    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str, json_schema: dict) -> Optional[dict]:
        """The cached entry for `key`, or None when there is none or it fails the active schema."""
        path = self._cache_file(key)
        if not path.is_file():
            return None
        try:
            entry = parse_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # JSONDecodeError is a ValueError
            return None
        if not isinstance(entry, dict) or "data" not in entry:
            return None
        data = entry["data"]
        if _has_nonfinite(data) or validate(data, json_schema):
            return None  # a stale or edited entry is a miss, never an answer
        return entry

    def _write_cache(self, key: str, model: str, data: Any, usage: Usage) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"model": model, "data": data, "usage": asdict(usage), "written_at": _now()}
        tmp = self._cache_file(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
        os.replace(tmp, self._cache_file(key))

    def _log(self, model: str, key: str, usage: Usage, *, cached: bool, outcome: str, attempts: int = 1) -> None:
        with self._lock:
            scope = self._scope
            entry = {
                "time": _now(),
                "model": model,
                "prompt_sha256": key,
                "usage": asdict(usage),
                "cached": cached,
                "outcome": outcome,
                "attempts": attempts,
                "case_number": scope.case_number,
                "system": scope.system,
                "run_id": scope.run_id,
            }
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
