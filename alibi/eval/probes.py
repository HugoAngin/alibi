"""Security probe harness over ``agents/execution/out/probes.json``.

    uv run --frozen python -m alibi.eval.probes --list
    uv run --frozen python -m alibi.eval.probes --run --target http://127.0.0.1:8000 \\
        --attempts alibi.app.probe_attempts --results data/probes.jsonl

Every probe in the catalogue is registered here and marked ``not_run``. The integration
session writes one attempt function per probe and nothing else:

    from alibi.eval.probes import Target, probe

    @probe("SP-01")
    def dispatch_without_approval(target: Target) -> bool:
        ...try the action against the running app at target.base_url...
        return True        # True: the backend blocked it. False: it went through.

An attempt may return a ``ProbeResult`` instead to add detail and the audit lines it saw. An
exception becomes ``error``. ``Harness.summary`` passes the outcomes to
``agents.execution.api.summarise``: a refusal probe passes when blocked, a control probe
passes when not blocked, and probes that were not run or errored count as attempted and
failed, so nothing drops out of the denominator. ``probes_blocked`` / ``probes_attempted``
in the results format are the refusal probes; ``controls_allowed`` / ``controls_attempted``
are the controls.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from agents.execution.api import load_probes, summarise

OUTCOMES = ("blocked", "not_blocked", "not_run", "error")
TO_SUMMARISE = {"blocked": "refused", "not_blocked": "accepted"}


@dataclass
class Target:
    """Where the attempts point: the running app and the credentials the harness may use."""

    base_url: str | None = None
    credentials: dict[str, Any] = field(default_factory=dict)
    client: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeResult:
    probe_id: str
    outcome: str
    detail: str = ""
    audit: list[str] = field(default_factory=list)
    kind: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"{self.probe_id}: outcome must be one of {OUTCOMES}, got {self.outcome!r}")

    @property
    def passed(self) -> bool | None:
        """True or False once run; None when not run or errored."""
        if self.outcome not in TO_SUMMARISE:
            return None
        return self.outcome == ("blocked" if self.kind == "refusal" else "not_blocked")

    def to_json(self) -> dict:
        return {"probe_id": self.probe_id, "kind": self.kind, "outcome": self.outcome,
                "passed": self.passed, "detail": self.detail, "audit": list(self.audit)}


Attempt = Callable[[Target], "bool | ProbeResult"]


class Harness:
    """Registry of attempt functions keyed by probe ID, and the runner over them."""

    def __init__(self, target: Target | None = None) -> None:
        self.target = target or Target()
        self.probes: dict[str, dict] = {p["id"]: p for p in load_probes()}
        self.attempts: dict[str, Attempt | None] = {pid: None for pid in self.probes}

    def register(self, probe_id: str) -> Callable[[Attempt], Attempt]:
        if probe_id not in self.probes:
            raise KeyError(f"unknown probe {probe_id!r}; see agents/execution/out/PROBES.md")

        def wrap(fn: Attempt) -> Attempt:
            if self.attempts[probe_id] is not None:
                raise ValueError(f"{probe_id} already has an attempt registered")
            self.attempts[probe_id] = fn
            return fn

        return wrap

    def registered(self) -> list[str]:
        return [pid for pid, fn in self.attempts.items() if fn is not None]

    def run_one(self, probe_id: str) -> ProbeResult:
        probe = self.probes[probe_id]
        attempt = self.attempts.get(probe_id)
        if attempt is None:
            return ProbeResult(probe_id, "not_run", "no attempt registered", kind=probe["kind"])
        try:
            got = attempt(self.target)
        except Exception as exc:  # noqa: BLE001 - any failure of the attempt is a recorded error
            return ProbeResult(probe_id, "error", f"{type(exc).__name__}: {exc}",
                               [traceback.format_exc(limit=3).strip()], kind=probe["kind"])
        if isinstance(got, ProbeResult):
            got.probe_id = probe_id
            got.kind = probe["kind"]
            return got
        if isinstance(got, bool):
            return ProbeResult(probe_id, "blocked" if got else "not_blocked", kind=probe["kind"])
        return ProbeResult(probe_id, "error", f"attempt returned {got!r}, expected bool or ProbeResult",
                           kind=probe["kind"])

    def run(self, ids: Iterable[str] | None = None) -> list[ProbeResult]:
        chosen = list(ids) if ids is not None else list(self.probes)
        unknown = [pid for pid in chosen if pid not in self.probes]
        if unknown:
            raise KeyError(f"unknown probes {unknown}")
        return [self.run_one(pid) for pid in chosen]

    @staticmethod
    def outcomes(results: Iterable[ProbeResult]) -> dict[str, str]:
        """What ``agents.execution.api.summarise`` takes; not_run and error are left out so
        they count as attempted and failed."""
        return {r.probe_id: TO_SUMMARISE[r.outcome] for r in results if r.outcome in TO_SUMMARISE}

    def summary(self, results: list[ProbeResult]) -> dict[str, Any]:
        out = summarise(self.outcomes(results))
        out["not_run"] = sorted(r.probe_id for r in results if r.outcome == "not_run")
        out["errors"] = sorted(r.probe_id for r in results if r.outcome == "error")
        out["core_failed"] = sorted(pid for pid in out["failed"] if self.probes[pid]["core"])
        return out

    def table(self, results: list[ProbeResult]) -> str:
        lines = ["| probe | kind | core | outcome | passed | detail |",
                 "| --- | --- | --- | --- | --- | --- |"]
        for r in results:
            probe = self.probes[r.probe_id]
            passed = "" if r.passed is None else ("yes" if r.passed else "no")
            lines.append(f"| {r.probe_id} | {probe['kind']} | {'yes' if probe['core'] else ''} | "
                         f"{r.outcome} | {passed} | {r.detail.replace('|', '/')[:80]} |")
        return "\n".join(lines)

    @staticmethod
    def write_results(results: Iterable[ProbeResult], path: Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for r in results:
                handle.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")
                count += 1
        return count


HARNESS = Harness()


def probe(probe_id: str) -> Callable[[Attempt], Attempt]:
    """Register an attempt on the default harness: ``@probe("SP-01")``."""
    return HARNESS.register(probe_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list every probe and its status")
    parser.add_argument("--run", action="store_true", help="run the registered attempts")
    parser.add_argument("--ids", nargs="*", help="only these probe IDs")
    parser.add_argument("--target", help="base URL of the running app")
    parser.add_argument("--attempts", nargs="*", default=[],
                        help="modules that register attempts with @probe(...)")
    parser.add_argument("--results", type=Path, help="write one JSON line per probe here")
    args = parser.parse_args(argv)
    if args.target:
        HARNESS.target.base_url = args.target
    for name in args.attempts:
        importlib.import_module(name)
    ids = args.ids if args.ids else list(HARNESS.probes)
    if args.run:
        results = HARNESS.run(ids)
    else:  # list only: never touch the target
        results = [ProbeResult(pid, "not_run",
                               "attempt registered" if HARNESS.attempts.get(pid) else "no attempt registered",
                               kind=HARNESS.probes[pid]["kind"]) for pid in ids]
    print(HARNESS.table(results))
    summary = HARNESS.summary(results)
    print(f"registered attempts: {len(HARNESS.registered())}/{len(HARNESS.probes)}")
    print(f"probes_blocked {summary['probes_blocked']} / probes_attempted {summary['probes_attempted']}; "
          f"controls_allowed {summary['controls_allowed']} / controls_attempted {summary['controls_attempted']}; "
          f"not run {len(summary['not_run'])}; errors {len(summary['errors'])}")
    if args.results:
        HARNESS.write_results(results, args.results)
        print(f"wrote {args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
