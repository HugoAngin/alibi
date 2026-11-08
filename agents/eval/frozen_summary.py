"""Export allowlisted aggregates from a retained capture without reading cases or gold."""

import argparse
import json
import math
from pathlib import Path
import re


SYSTEMS = ("agents", "one_pass", "rules")
MODES = ("frozen", "public_live", "public_rehearsal")
METRICS = set("""cases requests families safe_complete safe_complete_rate requests_safe questions
status_accuracy gold_linked predicted_linked match_precision match_recall unmatched
link_state_accuracy supported_claims citation_accuracy gold_non_supported gap_recall
contradiction_recall unresolved_recall facts_checked facts_recall forbidden_items unnecessary_items
unobservable_refs failures unsafe_proposals lookups observed_inputs model_calls tool_calls tokens
latency_ms_mean cost_usd_estimate probes_blocked probes_attempted prepolicy_forbidden_items
prepolicy_unnecessary_items safe_complete_ci_low safe_complete_ci_high requests_observed
correct_questions supported_claims_observed expected_gaps controls_attempted controls_allowed
catalogue_probes_observed predicted_links expected_links""".split())
USAGE = set("""expected_workers complete_workers unknown_workers absent_logs empty_logs
absent_or_invalid_usage absent_or_invalid_metadata failed_workers scope_mismatches complete""".split())
DIVERGENCE = {"rfcs", "differing_rfcs", "unknown_comparison_rfcs", "policy_filtering_rfcs", "unknown_reason_rfcs"}
SCHEDULING = set("""total attempted completed errors not_started initial_workers final_workers
rate_limit_reduced deadline_reached aborted""".split())
HEADLINE = {"safe_complete", "cases", "families", "independent_safe_complete", "model_calls_per_case",
            "cost_usd_estimate_per_case", "official_forbidden_items"}
PAIRED = {"wins", "ties", "losses", "delta_safe_complete_rate", "family_wins", "family_losses",
          "family_ties", "family_sign_test_p", "delta_ci_low", "delta_ci_high"}
RISK = {"failures", "observed", "eligible", "upper_95"}
COUNTERS = {"model_calls", "tokens", "cost_usd_estimate"}
CAVEATS = [
    "Headline correctness concerns investigation/draft evidence from top-level claims, not final recipient correctness after policy filtering.",
    "Package claim divergence is reported as aggregate counts; absent final claim fields remain unknown.",
    "Systems have unequal retrieval access: one-pass has no follow-up retrieval. Comparisons are not an isolated test of orchestration.",
    "The prepared synthetic held-out pack received rules calibration during authoring; it was not untouched by every evaluated system.",
    "Second-scorer metrics share retained inputs and do not establish independent corroboration.",
    "Complete usage estimates and partial known usage are separate; unknown or unlogged work is not zero cost.",
]


def _dict(value):
    return value if isinstance(value, dict) else {}


def _scalar(value):
    return value is None or type(value) is bool or (type(value) in (int, float) and math.isfinite(value))


def _select(value, keys):
    source = _dict(value)
    return {key: source[key] for key in sorted(keys) if key in source and _scalar(source[key])}


def _systems(value, keys, nested=False):
    source = _dict(value)
    return {name: _select(_dict(source[name]).get("metrics") if nested else source[name], keys)
            for name in SYSTEMS if name in source}


def _digest(value, lengths=(64,)):
    return value if isinstance(value, str) and len(value) in lengths and re.fullmatch(r"[0-9a-f]+", value) else None


def _headline(value):
    source = _dict(value)
    result = {"systems": _systems(source.get("systems"), HEADLINE),
              "paired": _systems(source.get("paired"), PAIRED),
              "probes": {key: _select(_dict(source.get("probes")).get(key), RISK)
                         for key in ("attacks_accepted", "controls_refused")},
              "hidden_reference": _systems(source.get("hidden_reference"), {"cases", "safe_complete"})}
    for name, row in result["systems"].items():
        original = _dict(_dict(source.get("systems")).get(name))
        interval = original.get("family_bootstrap_95")
        row["family_bootstrap_95"] = interval if isinstance(interval, list) and len(interval) == 2 and all(
            type(n) in (int, float) and math.isfinite(n) for n in interval) else None
        for key in ("adversarial_false_support", "forbidden_disclosure", "adversarial_forbidden_disclosure"):
            row[key] = _select(original[key], RISK) if isinstance(original.get(key), dict) else None
    return result


def build_summary(scores: dict, run: dict, scheduling: dict | None = None) -> dict:
    """Never copy arbitrary keys, text values, lists, details, IDs or source paths."""
    scores, run = _dict(scores), _dict(run)
    signature = _dict(run.get("input"))
    retained = _dict(scores.get("retained_capture"))
    scheduling = _dict(scheduling if scheduling is not None else retained.get("scheduling"))
    usage = _systems(retained.get("usage_by_system"), USAGE)
    for name, row in usage.items():
        original = _dict(_dict(retained.get("usage_by_system")).get(name))
        for key in ("totals", "partial_known_totals", "workers_with_known_values"):
            row[key] = _select(original.get(key), COUNTERS)
    if "original_run_sha256" in signature and not all(
            isinstance(scheduling.get(key), dict) and scheduling[key].get("aggregate")
            for key in ("original", "resume")):
        raise ValueError("resume export requires original and continuation scheduling")
    config = _select(signature, {"timeout", "workers", "dry", "inherited_llm_cap"})
    config["limits"] = _select(signature.get("limits"), set(SYSTEMS))
    result = {
        "format": "alibi.aggregate-summary.v1",
        "mode": scores.get("mode") if scores.get("mode") in MODES else None,
        "candidate_sha": _digest(signature.get("candidate", signature.get("commit", run.get("candidate"))), (40,)),
        "evaluator_sha": _digest(signature.get("evaluator_commit"), (40,)),
        "signature": {"config": config, "digests": {key: _digest(signature.get(key))
                      for key in ("manifest", "inputs", "model_config")}},
        "official": _systems(scores.get("official"), METRICS),
        "independent": _systems(scores.get("independent"), METRICS, nested=True),
        "adversarial": _systems(scores.get("adversarial"), METRICS),
        "independent_adversarial": _systems(scores.get("independent_adversarial"), METRICS, nested=True),
        "headline": _headline(scores.get("headline")),
        "probe_coverage": _select(scores.get("probe_coverage"), {"catalogue", "observed", "not_run", "error"}),
        "retained_usage": usage,
        "claim_divergence": _systems(retained.get("claim_divergence_by_system"), DIVERGENCE),
        "capture": {"aggregate": _select(scheduling.get("aggregate"), SCHEDULING),
                    "systems": _systems(scheduling.get("systems"), {"attempted", "completed", "failed", "not_started"})},
        "interpretation_and_caveats": CAVEATS.copy(),
    }
    if "original" in scheduling and "resume" in scheduling:
        result["interrupted_capture"] = {
            "original_run_sha256": _digest(signature.get("original_run_sha256")),
            "permission_proof_sha256": _digest(signature.get("permission_proof_sha256")),
            "original": {"aggregate": _select(_dict(scheduling["original"]).get("aggregate"), SCHEDULING),
                         "systems": _systems(_dict(scheduling["original"]).get("systems"),
                                             {"attempted", "completed", "failed", "not_started"})},
            "continuation": {"aggregate": _select(_dict(scheduling["resume"]).get("aggregate"), SCHEDULING),
                             "systems": _systems(_dict(scheduling["resume"]).get("systems"),
                                                 {"attempted", "completed", "failed", "not_started"})},
        }
        result["interpretation_and_caveats"].append(
            "Capture was interrupted after operator suspension exposed a worker cleanup race. "
            "All 67 original returned groups were preserved, including unexplained child exits. "
            "Only 62 unstarted groups and three publicly reproduced scheduler failures were eligible "
            "for continuation under the original lock and candidate. Original and continuation "
            "attempt counts are separate; combined attempted counts count unique groups. "
            "Scheduler completed counts returned groups, including candidate failures; per-system "
            "completed counts groups without a worker failure. Original pause-affected timing is "
            "qualified; unknown first-attempt usage is not zero."
        )
    return result


def _load(path: Path):
    return json.loads(path.read_text())


def _numeric(value, prefix=""):
    result = {}
    for key, item in value.items():
        name = f"{prefix}_{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_numeric(item, name))
        elif type(item) in (int, float):
            result[name] = item
        elif type(item) is bool:
            result[name] = int(item)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True, help="external retained capture directory")
    parser.add_argument("--out", type=Path, help="new aggregate JSON artifact; omitted prints only")
    args = parser.parse_args(argv)
    capture = args.capture.resolve()
    if capture.is_relative_to(Path(__file__).resolve().parents[2]):
        parser.error("capture must be external to the source checkout")
    protected_root = capture.parent if (capture / "resume-lock.json").is_file() else capture
    if args.out and args.out.resolve().is_relative_to(protected_root):
        parser.error("output must be outside the retained capture")
    schedule = capture / "scheduling.json"
    summary = build_summary(_load(capture / "scores.json"), _load(capture / "run.json"),
                            _load(schedule) if schedule.is_file() else None)
    encoded = json.dumps(summary, sort_keys=True, indent=2, allow_nan=False)
    if args.out:
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    print(encoded)
    print(json.dumps(_numeric(summary), sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
