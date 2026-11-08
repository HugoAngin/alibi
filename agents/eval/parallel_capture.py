"""Bounded evaluation scheduling; never retries a job or drops its outcome.

``run_job(job)`` must enforce its own subprocess timeout, capped to the remaining
deadline. The scheduler stops launching work at the deadline and drains calls
already running: Python threads cannot safely kill an executing subprocess.
``progress(outcome, metadata)`` runs on the coordinator thread for every outcome,
including jobs not started before the deadline. It should persist that outcome.
"""

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limited(result: Any) -> bool:
    """Accept a metadata dict or the worker's ``(rows, metadata)`` return value."""
    if isinstance(result, Mapping):
        return bool(result.get("rate_limited", False))
    if isinstance(result, (list, tuple)) and result and isinstance(result[-1], Mapping):
        return bool(result[-1].get("rate_limited", False))
    return False


def schedule(jobs: Iterable[Any], run_job: Callable[[Any], Any], *, workers: int = 4,
             deadline: datetime | None = None, progress: Callable | None = None,
             stop_on_error: bool = False) -> dict:
    """Return input-ordered outcomes with attempted/completed/deadline counts.

    Ordinary worker exceptions become error outcomes with only their class name,
    avoiding accidental disclosure of case text or credentials in exception text.
    BaseException and progress-callback failures propagate, so interrupted capture
    is not misrepresented as a complete run. The caller must preserve that run.
    With stop_on_error, worker exceptions stop new submissions and queued jobs
    become unavailable; already running jobs drain without cancellation or retry.
    """
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be an integer from 1 to 4")
    if deadline is not None and (not isinstance(deadline, datetime) or
                                 deadline.tzinfo is None or deadline.utcoffset() is None):
        raise ValueError("deadline must be a timezone-aware datetime")
    items = list(jobs)
    outcomes: list[dict | None] = [None] * len(items)
    counts = dict(total=len(items), attempted=0, completed=0, errors=0,
                  not_started=0, initial_workers=workers, final_workers=workers,
                  rate_limit_reduced=False, deadline_reached=False,
                  aborted=False, abort_reason=None,
                  deadline=deadline.isoformat() if deadline else None)
    target = workers
    next_index = 0

    def finish(index: int, status: str, *, result=None, error=None, reason=None):
        if deadline is not None and _now() >= deadline:
            counts["deadline_reached"] = True
        outcome = dict(index=index, job=items[index], status=status,
                       result=result, error=error, reason=reason)
        outcomes[index] = outcome
        counts[{"completed": "completed", "error": "errors", "not_started": "not_started"}[status]] += 1
        if progress is not None:
            progress(outcome, dict(counts))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="alibi-eval") as pool:
        active = {}
        while next_index < len(items) or active:
            expired = deadline is not None and _now() >= deadline
            if expired:
                counts["deadline_reached"] = True
            if (expired or counts["aborted"]) and next_index < len(items):
                for index in range(next_index, len(items)):
                    finish(index, "not_started", reason="capture_integrity" if counts["aborted"] else "deadline")
                next_index = len(items)
            while not expired and not counts["aborted"] and next_index < len(items) and len(active) < target:
                # Recheck before every submission, including expensive job iterators/callbacks.
                if deadline is not None and _now() >= deadline:
                    break
                active[pool.submit(run_job, items[next_index])] = next_index
                counts["attempted"] += 1
                next_index += 1
            if not active:
                continue
            remaining = (deadline - _now()).total_seconds() if deadline else None
            # After expiry, drain bounded workers without spinning on a zero timeout.
            wait_seconds = max(0.001, remaining) if remaining is not None and remaining > 0 else None
            done, _ = wait(active, timeout=wait_seconds, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=active.__getitem__):
                index = active.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    setup_expired = isinstance(exc, TimeoutError) and deadline is not None and _now() >= deadline
                    if stop_on_error and not counts["aborted"] and not setup_expired:
                        counts["aborted"] = True
                        counts["abort_reason"] = type(exc).__name__
                    finish(index, "error", error=type(exc).__name__)
                else:
                    if _rate_limited(result) and target > 2:
                        target = 2
                        counts["final_workers"] = target
                        counts["rate_limit_reduced"] = True
                    finish(index, "completed", result=result)
    assert all(outcome is not None for outcome in outcomes)
    assert counts["attempted"] == counts["completed"] + counts["errors"]
    return {"outcomes": outcomes, "metadata": counts}
