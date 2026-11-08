"""A small JSON document store for app state the core passport does not model.

It holds the bank side (received packages, verification results, dispositions, follow-ups),
held privacy rules awaiting clarification, and dispatch results. Writes are atomic (write to a
temporary file, then replace). The file lives under ``data/alibi/`` and is git-ignored.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any


class Sidecar:
    def __init__(self, path: Path | str | None):
        self.path = None if path is None else Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        if self.path is not None and self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = copy.deepcopy(value)
            self._flush()

    def update(self, key: str, fn) -> Any:
        """Apply ``fn(current_value_or_None) -> new_value`` under the lock and save."""
        with self._lock:
            new = fn(copy.deepcopy(self._data.get(key)))
            self._data[key] = new
            self._flush()
            return copy.deepcopy(new)

    def append(self, key: str, item: Any) -> None:
        self.update(key, lambda cur: [*(cur or []), copy.deepcopy(item)])

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)
