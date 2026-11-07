"""Keep the lockbox, the final slice of time, hidden until eval scores the chosen strategy.

`lead` sets where the lockbox starts in shared/splits.json:

    {"lockbox_from": "2024-07-01T00:00:00Z"}

A number also works, for tasks split by step or episode. Data's loaders pass every frame
they return through visible():

    from tools.lockbox import visible
    return visible(frame, "timestamp")     # or visible(frame) when time is the index

Rows at or after lockbox_from come back only when eval runs
`tools/results.py add --split lockbox`, which records the look. Everyone else, including
eval at any other time, gets the rows before it.
"""
from __future__ import annotations

import json
import numbers
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import team as roster
except ImportError:
    from tools import team as roster

SPLITS = roster.ROOT / "shared" / "splits.json"
OPEN_FLAG = "LOCKBOX_OPEN"
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(f"lockbox: {message}", file=sys.stderr)


def lockbox_from(path: Path | None = None):
    """The first time in the lockbox, or None if lead has not set one."""
    try:
        value = json.loads((path or SPLITS).read_text()).get("lockbox_from")
    except FileNotFoundError:
        return None
    if isinstance(value, str):
        return to_utc(value)
    return value


def to_utc(value):
    """Turn a time from strings, dates, pandas or numpy into a UTC datetime. Numbers pass through."""
    if isinstance(value, bool):
        raise TypeError("a boolean is not a time")
    if isinstance(value, numbers.Real):
        return value
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    elif type(value).__name__ == "datetime64":
        import numpy as np
        value = np.datetime64(value, "us").item()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if not isinstance(value, datetime):
        raise TypeError(f"cannot read {value!r} as a time")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_role: list = []


def role() -> str | None:
    """This clone's role from team.json, looked up once."""
    if not _role:
        try:
            member = roster.member_for_login(roster.load(), roster.current_login())
        except (OSError, subprocess.CalledProcessError, ValueError):
            member = None
        _role.append(member["role"] if member else None)
    return _role[0]


def is_open() -> bool:
    """True only inside `results.py add --split lockbox` run by eval."""
    return os.environ.get(OPEN_FLAG) == "1" and role() == "eval"


def reaches(times, start) -> bool:
    """True if any of times is at or after start. Takes one time or many."""
    if isinstance(times, (str, numbers.Real, date)) or type(times).__name__ == "datetime64":
        values = [times]
    elif hasattr(times, "max"):
        values = [times.max()]
    else:
        values = list(times)
    return any(to_utc(v) >= start for v in values if v is not None and v == v)


def check(times) -> None:
    """Raise if times reach the lockbox and it is not open."""
    start = lockbox_from()
    if start is None:
        _warn_once("no lockbox set in shared/splits.json, so nothing is hidden yet.")
        return
    if not is_open() and reaches(times, start):
        raise PermissionError(f"these times reach the lockbox, which starts at {start}. "
                              f"Only eval opens it, through tools/results.py add --split lockbox.")


def visible(frame, column: str | None = None):
    """Return the rows of a pandas frame you may see: all of them while the lockbox is open."""
    start = lockbox_from()
    if start is None:
        _warn_once("no lockbox set in shared/splits.json, so nothing is hidden yet.")
        return frame
    if is_open():
        return frame
    import pandas as pd

    times = frame.index if column is None else frame[column]
    if isinstance(start, datetime):
        times = pd.to_datetime(times)
        tz = getattr(times, "tz", None) if column is None else times.dt.tz
        cutoff = pd.Timestamp(start)
        cutoff = cutoff.tz_convert(tz) if tz is not None else cutoff.tz_convert(None)
    else:
        cutoff = start
    keep = times < cutoff
    hidden = int(len(frame) - keep.sum())
    if hidden:
        _warn_once(f"hiding {hidden} rows from {start} on. Only eval sees the lockbox.")
    return frame[keep]
