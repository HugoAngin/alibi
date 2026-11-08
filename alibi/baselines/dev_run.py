"""Live dev run of the one-pass baseline on the Maya fixtures, for the S3 report.

    uv run --frozen python -m alibi.baselines.dev_run [--cap 4] [--variants contradiction,supported]
                                                      [--out alibi/REPORTS/traces/one_pass.md]

Needs GEMINI_API_KEY (exit 2 without it; nothing is called). One model call per RFC, so two
variants cost four calls. Each run is recorded to ``alibi/fixtures/replays/maya_onepass_<variant>.json``
and ``ALIBI_REPLAY=1`` serves the recording instead (zero calls, labelled in the trace). Stores
go under ``data/alibi/dev_traces/onepass_<variant>.db``. The last stdout line is one JSON object
with the SPEC comparison counts per variant.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "alibi" / "REPORTS" / "traces" / "one_pass.md"
RFCS = ("rfc_a_001", "rfc_b_001")


def _fresh(path: Path) -> Path:
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_variant(variant: str, budget: Any, keys: Any, work: Path) -> dict[str, Any]:
    from alibi.agents.replay import make_client, replay_label
    from alibi.api import load_fixture, open_store
    from alibi.baselines.one_pass import run_one_pass
    from alibi.llm.client import BudgetExceeded

    client = make_client("maya", f"onepass_{variant}", live=_live())
    out: dict[str, Any] = {"variant": variant, "client": replay_label(client), "rfcs": {}}
    with open_store(_fresh(work / f"onepass_{variant}.db")) as store:
        fx = load_fixture(store, variant, keystore=keys)
        for request_id in RFCS:
            if budget.remaining <= 0:
                out["rfcs"][request_id] = {"skipped": "budget spent"}
                continue
            try:
                budget.spend()
            except BudgetExceeded:
                out["rfcs"][request_id] = {"skipped": "budget spent"}
                continue
            run = run_one_pass(store, request_id, client=client, settlement_lookup=fx["settlement_lookup"],
                               txn_refresh=fx["txn_refresh"], registry=keys.registry(), now=fx["now"])
            notes = [e for e in run.result.trace if e.actor == "one_pass" and e.step == "note"
                     and "proposed_statuses" in e.result]
            proposed = notes[-1].result.get("proposed_statuses", {}) if notes else {}
            out["rfcs"][request_id] = {
                "statuses": {c.question_id: c.status.value for c in run.result.claims},
                "proposed_statuses": proposed,
                "gaps": {c.question_id: (c.gap or "")[:160] for c in run.result.claims},
                "lookups": run.lookups, "observed_inputs": run.observed_inputs,
                "unsafe_proposals": run.unsafe_proposals, "model_calls": run.model_calls,
                "tokens": run.tokens, "run_id": run.result.run_id,
                "trace": [f"{e.seq:3d} | {e.actor} | {e.step} | {e.tool or '-'} | {e.outcome} | "
                          f"{e.reason or '-'} | {', '.join(r.key() for r in e.refs) or '-'}"
                          for e in run.result.trace],
            }
    return out


def _live() -> Any:
    from alibi.llm.client import GeminiClient
    return GeminiClient(use_cache=False)


def render(results: list[dict[str, Any]], cap: int, used: int) -> str:
    lines = ["# One-pass baseline dev run", "",
             f"Fixed retrieval: look up any payout reference named in a balance_release message. "
             f"Gemini calls used: {used} of {cap}.", ""]
    for r in results:
        lines += [f"## {r['variant']} ({r['client']})", ""]
        for request_id, row in r["rfcs"].items():
            if "skipped" in row:
                lines += [f"- {request_id}: skipped ({row['skipped']})"]
                continue
            lines += [f"### {request_id} ({row['run_id']})", "",
                      f"- final statuses: {json.dumps(row['statuses'], sort_keys=True)}",
                      f"- model proposed: {json.dumps(row['proposed_statuses'], sort_keys=True)}",
                      f"- gaps: {json.dumps(row['gaps'], sort_keys=True)}",
                      f"- lookups={row['lookups']} observed_inputs={row['observed_inputs']} "
                      f"unsafe_proposals={row['unsafe_proposals']} model_calls={row['model_calls']} "
                      f"tokens={row['tokens']}", "", "```", *row["trace"], "```", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cap", type=int, default=4)
    parser.add_argument("--variants", default="contradiction,supported")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    from alibi.llm.client import CallBudget, MissingApiKey, load_api_key

    try:
        load_api_key()
    except MissingApiKey:
        print("dev_run: GEMINI_API_KEY is not set; nothing was called", file=sys.stderr)
        return 2
    from alibi.api import default_keystore

    work = REPO / "data" / "alibi" / "dev_traces"
    keys = default_keystore(work / "keys")
    budget = CallBudget(args.cap, name="one_pass_dev")
    results = [run_variant(v.strip(), budget, keys, work) for v in args.variants.split(",") if v.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(results, args.cap, budget.used), encoding="utf-8")
    summary = {r["variant"]: {rid: {k: row.get(k) for k in ("statuses", "lookups", "observed_inputs",
                                                               "unsafe_proposals", "model_calls", "tokens")}
                              for rid, row in r["rfcs"].items()} for r in results}
    print(json.dumps({"variants": summary, "calls_used": budget.used, "cap": args.cap}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
