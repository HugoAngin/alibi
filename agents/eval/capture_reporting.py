"""Summaries of retained worker measurements and draft/final claim differences.

This module reads capture artifacts only when called; it never launches workers,
loads cases/gold, or alters scorer headline semantics.
"""

import json
import math
from pathlib import Path

from alibi.eval import scorer


METRICS = ("model_calls", "tokens", "cost_usd_estimate")
UNCERTAINTY = ("rows_with_potential_unmeasured_attempts", "rows_missing_tokens",
               "rows_with_inconsistent_tokens", "unavailable_rows", "unknown_pricing_models")


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _usage(output: Path, system: scorer.System, cases: list[int]) -> dict:
    partial = dict.fromkeys(METRICS, 0)
    known = dict.fromkeys(METRICS, 0)
    result = dict(expected_workers=len(cases), complete_workers=0, unknown_workers=0,
                  absent_logs=0, empty_logs=0, absent_or_invalid_usage=0,
                  absent_or_invalid_metadata=0, failed_workers=0, scope_mismatches=0)
    for case in cases:
        directory = output / "capture-workers" / f"{system.name}-{case}"
        usage, meta = _read(directory / "usage.json"), _read(directory / "metadata.json")
        log = directory / "llm_log.jsonl"
        try:
            size = log.stat().st_size
        except OSError:
            size = None
        result["absent_logs"] += size is None
        result["empty_logs"] += size == 0
        result["absent_or_invalid_usage"] += usage is None
        result["absent_or_invalid_metadata"] += meta is None
        totals = (usage or {}).get("totals", {})
        if not isinstance(totals, dict):
            totals = {}
        scope_mismatch = bool(meta is not None and (
            meta.get("system") != system.name or meta.get("case_number") != case))
        if usage is not None:
            unscoped = usage.get("unscoped", {})
            scope_mismatch |= isinstance(unscoped, dict) and bool(unscoped.get("log_rows"))
            by_case = usage.get("by_case", [])
            scope_mismatch |= not isinstance(by_case, list) or any(
                not isinstance(group, dict) or group.get("case_number") != case for group in by_case)
        result["scope_mismatches"] += scope_mismatch
        failed = bool((meta or {}).get("failures")) or any(
            row.get("failure") for (number, _), row in system.predictions.items() if number == case)
        result["failed_workers"] += failed
        complete = bool(usage is not None and meta is not None and not failed
                        and not scope_mismatch
                        and size is not None and not usage.get("usage_error")
                        and all(_number(totals.get(key)) for key in METRICS)
                        and not any(totals.get(key) for key in UNCERTAINTY))
        # Only a successful deterministic worker may have a complete empty log.
        if size == 0:
            complete = complete and system.name == "rules" and all(totals.get(k) == 0 for k in METRICS)
        retained_partial = (usage or {}).get("partial_totals", {})
        if not isinstance(retained_partial, dict):
            retained_partial = {}
        for key in METRICS:
            value = totals.get(key)
            if not _number(value):
                value = retained_partial.get(key)
            if key == "cost_usd_estimate" and not _number(value):
                value = retained_partial.get("priced_rows_cost_usd_estimate",
                                             totals.get("priced_rows_cost_usd_estimate"))
            if _number(value) and not scope_mismatch:
                partial[key] += value
                known[key] += 1
        result["complete_workers"] += complete
        result["unknown_workers"] += not complete
    result["complete"] = bool(cases) and result["unknown_workers"] == 0
    result["totals"] = partial.copy() if result["complete"] else dict.fromkeys(METRICS)
    result["partial_known_totals"] = {key: value if known[key] else None for key, value in partial.items()}
    result["workers_with_known_values"] = known
    return result


def _comparable(value, field):
    if field == "evidence_refs" and isinstance(value, list):
        return sorted({json.dumps(item, sort_keys=True) for item in value})
    return value


def _divergence(system: scorer.System) -> dict:
    details = []
    unknown = len(system.invalid)
    for (case, request), row in sorted(system.predictions.items()):
        final = row.get("package_claims")
        if not isinstance(final, list) or any(not isinstance(c, dict) or not isinstance(c.get("question_id"), str)
                                              for c in final):
            unknown += 1
            details.append(dict(case_number=case, request_id=request, differing_fields=[],
                                unknown_fields=["package_claims"], reason="unknown"))
            continue
        top = scorer.claims_by_question(row)
        packaged = {claim["question_id"]: claim for claim in final}
        differences, unknown_fields, reasons = set(), set(), set()
        if len(packaged) != len(final):
            unknown_fields.add("duplicate_question_id")
        for question in sorted(top.keys() | packaged.keys()):
            before, after = top.get(question), packaged.get(question)
            if before is None or after is None:
                differences.add("claim_presence")
                reasons.add("unknown")
                continue
            changed_fields = set()
            for field in ("status", "facts", "evidence_refs"):
                if field not in before or field not in after:
                    unknown_fields.add(field)
                elif _comparable(before[field], field) != _comparable(after[field], field):
                    changed_fields.add(field)
            differences.update(changed_fields)
            if changed_fields:
                change = after.get("changed")
                policy = isinstance(change, str) and any(
                    marker in change.lower() for marker in ("policy", "withheld", "withholds", "holder's request"))
                reasons.add("policy_filtering" if policy else "unknown")
        unknown += bool(unknown_fields)
        details.append(dict(case_number=case, request_id=request, differing_fields=sorted(differences),
                            unknown_fields=sorted(unknown_fields),
                            reason="policy_filtering" if reasons == {"policy_filtering"} else
                                   "unknown" if reasons or unknown_fields else "no_observed_difference"))
    differing = [row for row in details if row["differing_fields"]]
    return dict(rfcs=len(system.predictions) + len(system.invalid),
                differing_rfcs=len(differing), unknown_comparison_rfcs=unknown,
                policy_filtering_rfcs=sum(row["reason"] == "policy_filtering" for row in differing),
                unknown_reason_rfcs=sum(row["reason"] == "unknown" for row in differing),
                details=details)


def summarize_capture(output: Path, systems: list[scorer.System]) -> dict:
    """Aggregate capture-workers usage and compare available final claim fields.

    Missing final facts are unobservable, not proof of equality or deletion.
    Usage totals are complete only when every expected worker is complete;
    partial_known_totals may omit unlogged/in-flight calls and unpriced usage.
    Expected cases come from the union of supplied valid/invalid prediction keys.
    """
    cases = sorted({case for system in systems for case, _ in
                    (system.predictions.keys() | system.invalid.keys())})
    return {"usage_by_system": {system.name: _usage(Path(output), system, cases) for system in systems},
            "claim_divergence_by_system": {system.name: _divergence(system) for system in systems},
            "scope": "Top-level claims describe investigation/draft evidence; package_claims fields are compared only where retained. This does not establish post-policy recipient correctness."}
