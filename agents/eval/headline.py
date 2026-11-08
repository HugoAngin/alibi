"""Render deck statistics from the saved Alibi scorer output."""

import argparse
import json
from math import comb, expm1, log
from pathlib import Path

from agents.execution.api import load_probes
from alibi.eval import common, scorer


CONFIDENCE = 0.95
SYSTEM_NAMES = {"agents": "Full Alibi", "one_pass": "One-pass Gemini", "rules": "Rules"}


def exact_sign_test(wins: int, losses: int) -> float | None:
    """Two-sided exact binomial test after dropping ties."""
    count = wins + losses
    if count == 0:
        return None
    tail = sum(comb(count, k) for k in range(min(wins, losses) + 1))
    return min(1.0, (2 * tail) / (2 ** count))


def _failure_row(failures: int, observed: int, eligible: int) -> dict:
    # For zero events, the one-sided exact binomial bound solves (1 - p)^n = alpha.
    upper = -expm1(log(1 - CONFIDENCE) / observed) if observed and failures == 0 else None
    return {"failures": failures, "observed": observed, "eligible": eligible, "upper_95": upper}


def _fake_clearances(score: scorer.SystemScore | None) -> dict:
    questions = [question for request in score.requests for question in request.questions
                 if question.expected_status == "contradicted"] if score else []
    observed = [question for question in questions if question.predicted_status is not None]
    return _failure_row(sum(q.predicted_status == "supported" for q in observed), len(observed),
                        len(questions))


def _forbidden_disclosures(score: scorer.SystemScore) -> dict:
    delivered = [request for request in score.requests if request.disclosure is not None and isinstance(
        score.system.predictions[(request.case_number, request.request_id)].get("package"), dict)]
    return _failure_row(sum(request.disclosure.forbidden_items > 0 for request in delivered),
                        len(delivered), len(score.requests))


def _probe_failures(outcomes: dict[str, str], kind: str, expected: str) -> dict:
    ids = [row["id"] for row in load_probes() if row["kind"] == kind]
    observed = [outcomes[probe_id] for probe_id in ids if probe_id in outcomes]
    return _failure_row(sum(outcome != expected for outcome in observed), len(observed), len(ids))


def _paired(primary: scorer.SystemScore, baseline: scorer.SystemScore) -> dict:
    other = {case.case_number: case for case in baseline.cases}
    differences = dict.fromkeys(primary.by_family, 0)
    for case in primary.cases:
        counterpart = other[case.case_number]
        if counterpart.family != case.family:
            raise ValueError("paired systems must score the same case families")
        differences[case.family] += int(case.safe_complete) - int(counterpart.safe_complete)
    wins = sum(delta > 0 for delta in differences.values())
    losses = sum(delta < 0 for delta in differences.values())
    return {**scorer.paired(primary, baseline), "family_wins": wins, "family_losses": losses,
            "family_ties": len(differences) - wins - losses,
            "family_sign_test_p": exact_sign_test(wins, losses)}


def summarize(scores: list[scorer.SystemScore], adversarial: list[scorer.SystemScore],
              independent: dict, outcomes: dict[str, str], mode: str) -> dict:
    """Export aggregate-only headline inputs from the official scorer objects."""
    primary = next(score for score in scores if score.system.name == "agents")
    attacks = {score.system.name: score for score in adversarial}
    systems = {}
    for score in scores:
        name = score.system.name
        metrics = score.metrics
        cases = metrics["cases"]
        attack = attacks.get(name)
        systems[name] = {
            "safe_complete": metrics["safe_complete"], "cases": cases, "families": metrics["families"],
            "independent_safe_complete": independent[name]["metrics"]["safe_complete"],
            "family_bootstrap_95": [metrics["safe_complete_ci_low"], metrics["safe_complete_ci_high"]]
            if "safe_complete_ci_low" in metrics else None,
            "model_calls_per_case": metrics["model_calls"] / cases if metrics["model_calls"] is not None else None,
            "cost_usd_estimate_per_case": metrics["cost_usd_estimate"] / cases
            if metrics["cost_usd_estimate"] is not None else None,
            "adversarial_false_support": _fake_clearances(attack),
            "forbidden_disclosure": _forbidden_disclosures(score),
            "adversarial_forbidden_disclosure": _forbidden_disclosures(attack) if attack else None,
            "official_forbidden_items": metrics["forbidden_items"],
        }
    hidden_reference = {}
    for score in scores:
        group = score.by_family.get("hidden_reference")
        if group:
            hidden_reference[score.system.name] = {"safe_complete": sum(group), "cases": len(group)}
    return {
        "mode": mode, "systems": systems,
        "paired": {score.system.name: _paired(primary, score) for score in scores if score is not primary},
        "probes": {"attacks_accepted": _probe_failures(outcomes, "refusal", "refused"),
                   "controls_refused": _probe_failures(outcomes, "control", "accepted")},
        "hidden_reference": hidden_reference,
        "hidden_reference_mode": mode if hidden_reference else None,
    }


def _number(value: float | None, digits: int = 4) -> str:
    return "unavailable" if value is None else f"{value:.{digits}g}"


def _interval(interval: list[float] | None) -> str:
    return "unavailable" if interval is None else f"{interval[0]:.1%} to {interval[1]:.1%}"


def _risk_cells(label: str, row: dict) -> str:
    upper = f"{row['upper_95']:.1%}" if row["upper_95"] is not None else "—"
    return f"| {label} | {row['failures']} / {row['observed']} | {row['observed']} / {row['eligible']} | {upper} |"


def render(result: dict) -> str:
    mode = {"frozen": "Frozen comparison", "public_live": "Public live check",
            "public_rehearsal": "Public rehearsal with stubbed model and drafts only"}
    lines = ["# Alibi headline results", "", mode.get(result["mode"], result["mode"]), "",
             "| System | Safe complete | Independent safe complete | Families | 95% family bootstrap | Model calls per case | Estimated USD per case | Forbidden items |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, row in result["systems"].items():
        lines.append(f"| {SYSTEM_NAMES[name]} | {row['safe_complete']} / {row['cases']} | "
                     f"{row['independent_safe_complete']} / {row['cases']} | {row['families']} | "
                     f"{_interval(row['family_bootstrap_95'])} | {_number(row['model_calls_per_case'])} | "
                     f"{_number(row['cost_usd_estimate_per_case'])} | {row['official_forbidden_items']} |")
    lines += ["", "| Full Alibi against | Case wins / ties / losses | Family wins / ties / losses | Exact family sign test p | Paired 95% rate difference |",
              "| --- | --- | --- | --- | --- |"]
    for name, row in result["paired"].items():
        interval = [row["delta_ci_low"], row["delta_ci_high"]] if "delta_ci_low" in row else None
        lines.append(f"| {SYSTEM_NAMES[name]} | {row['wins']} / {row['ties']} / {row['losses']} | "
                     f"{row['family_wins']} / {row['family_ties']} / {row['family_losses']} | "
                     f"{_number(row['family_sign_test_p'])} | {_interval(interval)} |")
    lines += ["", "The sign test uses each family's net win or loss and drops tied families.", "",
              "| Failure check | Failures / observed | Observed / eligible | One-sided 95% upper bound at zero failures |",
              "| --- | --- | --- | --- |"]
    for name, row in result["systems"].items():
        lines.append(_risk_cells(f"{SYSTEM_NAMES[name]} · false support for an adversarial contradicted claim",
                                 row["adversarial_false_support"]))
        lines.append(_risk_cells(f"{SYSTEM_NAMES[name]} · delivered package with forbidden disclosure",
                                 row["forbidden_disclosure"]))
        if row["adversarial_forbidden_disclosure"] is not None:
            lines.append(_risk_cells(f"{SYSTEM_NAMES[name]} · adversarial package with forbidden disclosure",
                                     row["adversarial_forbidden_disclosure"]))
    lines += [_risk_cells("Attack probes accepted", result["probes"]["attacks_accepted"]),
              _risk_cells("Allowed control probes refused", result["probes"]["controls_refused"]),
              "", "Zero-failure bounds use the exact binomial formula on observed trials. "
              "They assume independent trials and do not cover unobserved trials or measure a production rate.",
              "", "| Hidden-reference family | Safe complete |", "| --- | --- |"]
    if result["hidden_reference"]:
        source = mode.get(result["hidden_reference_mode"], result["hidden_reference_mode"])
        lines += [f"| {SYSTEM_NAMES[name]} ({source}) | {row['safe_complete']} / {row['cases']} |"
                  for name, row in result["hidden_reference"].items()]
    else:
        lines.append("| No hidden-reference results in this capture | — |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scores", type=Path, help="scores.json written by agents.eval.run_sealed")
    parser.add_argument("--public-scores", type=Path, help="public scores.json for a separate hidden-reference row")
    parser.add_argument("--out", type=Path, help="default is HEADLINE.md next to scores.json")
    args = parser.parse_args(argv)
    report = common.load_json(args.scores)
    if "headline" not in report:
        parser.error("this score file predates headline evidence; replay the scorer to regenerate it")
    result = report["headline"]
    if args.public_scores and not result["hidden_reference"]:
        public = common.load_json(args.public_scores).get("headline")
        if not public or public["mode"] not in ("public_rehearsal", "public_live"):
            parser.error("--public-scores requires public headline evidence")
        result = {**result, "hidden_reference": public["hidden_reference"],
                  "hidden_reference_mode": public["hidden_reference_mode"]}
    (args.out or args.scores.with_name("HEADLINE.md")).write_text(render(result), encoding="utf-8")
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
