"""Record every live Gemini call of a run and serve it back when ``ALIBI_REPLAY=1``.

    from alibi.agents.replay import make_client, replay_label
    client = make_client("case_1", "supported")        # RecordingClient (live) or ReplayClient
    result = client.generate(model, system, user, schema, budget)
    print(replay_label(client))                        # 'live' or 'REPLAY: served from recording ...'

Recordings live in ``alibi/fixtures/replays/<case>_<variant>.json``. An entry holds the model,
the prompt sha256, the role line (first line of the system prompt) and the model's answer. The
system prompt and the user text are never stored: they can carry passport data. Replay looks an
entry up by sha256 first; when the prompt drifted it falls back to the next unused entry of the
same role in recorded order, so a demo still replays after a small prompt edit.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from alibi.llm.client import (
    BudgetExceeded,
    CallBudget,
    LLMError,
    Result,
    SchemaValidationError,
    Unavailable,
    Usage,
    prompt_sha256,
)

REPLAY_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replays"
SCHEMA = "alibi.replay.v1"
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_REPLAY_ON = ("1", "true", "yes")
_REPLAYABLE_ERRORS = {
    "Unavailable": Unavailable,
    "SchemaValidationError": SchemaValidationError,
    "BudgetExceeded": BudgetExceeded,
}


class ReplayMiss(LLMError):
    """The recording has no entry for this call: neither by sha256 nor an unused one for the role."""


def replay_path(case: str, variant: str, directory: Optional[Path] = None) -> Path:
    """``<directory>/<case>_<variant>.json``. Both names must match ``[A-Za-z0-9_-]+``."""
    for label, value in (("case", case), ("variant", variant)):
        if not isinstance(value, str) or not _NAME.match(value):
            raise ValueError(f"{label} must match [A-Za-z0-9_-]+, got {value!r}")
    base = Path(directory) if directory is not None else REPLAY_DIR
    return base / f"{case}_{variant}.json"


def role_of(system: str) -> str:
    """The role line: the first non-empty line of the system prompt, stripped (e.g. '# Challenger')."""
    for line in (system or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- recording


class RecordingClient:
    """Wrap a live client; append one entry per ``generate`` call and save the file after each.

    Successful calls record ``{seq, model, prompt_sha256, role, data, usage, cached, recorded_at}``;
    failures record ``{seq, model, prompt_sha256, role, error, message, recorded_at}`` and re-raise.
    The system prompt and user text are never written, only their sha256.
    """

    replay = False
    label = "live"

    def __init__(self, live: Any, path: Path, *, case: str, variant: str):
        self.live = live
        self.path = Path(path)
        self.case = case
        self.variant = variant
        self.entries: list[dict] = []
        self.started_at = _now()

    def generate(
        self,
        model: str,
        system: str,
        user: str,
        json_schema: dict,
        budget: Optional[CallBudget] = None,
    ) -> Result:
        key = prompt_sha256(model, system, user, json_schema)
        base = {"seq": len(self.entries), "model": model, "prompt_sha256": key, "role": role_of(system)}
        try:
            result = self.live.generate(model, system, user, json_schema, budget)
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised unchanged
            entry = dict(base, error=type(exc).__name__, message=str(exc)[:300], recorded_at=_now())
            self.entries.append(entry)
            self.save()
            raise
        entry = dict(
            base,
            data=copy.deepcopy(result.data),
            usage=asdict(result.usage),
            cached=bool(result.cached),
            recorded_at=_now(),
        )
        self.entries.append(entry)
        self.save()
        return result

    def payload(self) -> dict:
        return {
            "schema": SCHEMA,
            "case": self.case,
            "variant": self.variant,
            "recorded_at": self.started_at,
            "models": sorted({e["model"] for e in self.entries}),
            "calls": self.entries,
        }

    def save(self) -> Path:
        """Write the recording atomically (tmp file + os.replace) and return its path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.payload(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path


# --------------------------------------------------------------------------- replay


class ReplayClient:
    """Serve recorded answers. Match by prompt sha256, else the next unused entry of the same role.

    A recorded failure replays as the same error class for Unavailable, SchemaValidationError and
    BudgetExceeded, and as Unavailable otherwise. No match raises ReplayMiss naming the role.
    """

    replay = True

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"no replay recording at {self.path}; run live once to record it")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema") != SCHEMA:
            raise ValueError(f"{self.path} has schema {payload.get('schema')!r}, expected {SCHEMA!r}")
        self.case = payload.get("case")
        self.variant = payload.get("variant")
        self.models: list[str] = list(payload.get("models", []))
        self.calls: list[dict] = list(payload.get("calls", []))
        self.label = f"replay: {self.path.name}"
        self.served = 0
        self.misses = 0
        self._used: set[int] = set()

    def generate(
        self,
        model: str,
        system: str,
        user: str,
        json_schema: dict,
        budget: Optional[CallBudget] = None,
    ) -> Result:
        key = prompt_sha256(model, system, user, json_schema)
        role = role_of(system)
        index = self._find(key, role)
        if index is None:
            self.misses += 1
            raise ReplayMiss(
                f"recording {self.path.name} has no unused entry for role {role!r} "
                f"(sha256 {key[:12]}..., {self.served} served so far)"
            )
        self._used.add(index)
        self.served += 1
        entry = self.calls[index]
        if "error" in entry:
            cls = _REPLAYABLE_ERRORS.get(entry["error"], Unavailable)
            message = f"replayed {entry['error']}: {entry.get('message', '')}"
            if cls is SchemaValidationError:
                raise cls([message])
            raise cls(message)
        return Result(
            data=copy.deepcopy(entry.get("data")),
            model=entry.get("model", model),
            prompt_sha256=key,
            cached=True,
            usage=Usage(**entry.get("usage", {})),
            attempts=0,
        )

    def _find(self, key: str, role: str) -> Optional[int]:
        for i, entry in enumerate(self.calls):
            if i not in self._used and entry.get("prompt_sha256") == key:
                return i
        for i, entry in enumerate(self.calls):
            if i not in self._used and entry.get("role") == role:
                return i
        return None

    @property
    def remaining(self) -> int:
        return len(self.calls) - len(self._used)


# --------------------------------------------------------------------------- factory


def make_client(
    case: str,
    variant: str,
    *,
    live: Any = None,
    env: Optional[Mapping[str, str]] = None,
    directory: Optional[Path] = None,
) -> RecordingClient | ReplayClient:
    """ReplayClient when ``ALIBI_REPLAY`` is 1/true/yes, else a RecordingClient around ``live``.

    ``live`` defaults to a lazily imported ``GeminiClient`` so importing this module needs no key.
    """
    env = os.environ if env is None else env
    path = replay_path(case, variant, directory)
    if str(env.get("ALIBI_REPLAY", "")).strip().lower() in _REPLAY_ON:
        return ReplayClient(path)
    if live is None:
        from alibi.llm.client import GeminiClient

        live = GeminiClient()
    return RecordingClient(live, path, case=case, variant=variant)


def replay_label(client: Any) -> str:
    """'REPLAY: served from recording <name>' for a ReplayClient, else 'live'."""
    if getattr(client, "replay", False):
        path = getattr(client, "path", None)
        return f"REPLAY: served from recording {Path(path).name if path else 'unknown'}"
    return "live"


def list_recordings(directory: Optional[Path] = None) -> list[Path]:
    """Sorted ``*.json`` recordings in the replay directory (empty when it does not exist)."""
    base = Path(directory) if directory is not None else REPLAY_DIR
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.json") if p.is_file())
