"""The scorer: gold replayed as a prediction scores perfectly on every case, violations are
caught, failures stay in the denominators, intervals and comparisons behave.

Every expected count is derived from the loaded case set, so adding cases or families to
``agents/data/out/cases`` does not invalidate these tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agents.data import api as data_api
from alibi.eval import oracle, scorer
from alibi.eval.common import EvalError, load_dev_cases, load_sealed_cases, write_jsonl
from alibi.eval.scorer import RESULT_KEYS, build_system, paired, results_line, run

CASES = load_dev_cases()
ORACLE_ROWS = oracle.predictions_for(CASES)

N = len(CASES)
N_REQUESTS = sum(len(c["rfcs"]) for c in CASES)
FAMILIES = list(dict.fromkeys(c["family"] for c in CASES))  # in order of first appearance
N_FAMILIES = len(FAMILIES)
N_DEMO = sum(1 for c in CASES if c["family"] == "demo")
N_CLEAN = sum(1 for c in CASES if c["family"] == "clean")
GOLD_LINKED = sum(1 for c in CASES for link in c["gold"]["links"] if link["state"] == "linked")
LAST = CASES[-1]["case_number"]


@pytest.fixture
def rows() -> list[dict]:
    return copy.deepcopy(ORACLE_ROWS)


def score(rows: list[dict], cases: list[dict] = CASES, name: str = "sys") -> scorer.SystemScore:
    return run(cases, [build_system(name, rows)])[0]


def row(rows: list[dict], number: int, request_id: str | None = None) -> dict:
    for r in rows:
        if r["case_number"] == number and (request_id is None or r["request_id"] == request_id):
            return r
    raise KeyError((number, request_id))


def case_of(result: scorer.SystemScore, number: int) -> scorer.CaseScore:
    return next(c for c in result.cases if c.case_number == number)


def failed(result: scorer.SystemScore, number: int) -> list[scorer.RequestScore]:
    return [r for r in case_of(result, number).requests if not r.safe_complete]


def reasons(result: scorer.SystemScore, number: int) -> str:
    return " ".join(r for req in case_of(result, number).requests for r in req.all_reasons)


# --------------------------------------------------------------------------- perfect score


def test_gold_replay_scores_perfectly_on_every_case(rows):
    result = score(rows, name="oracle")
    m = result.metrics
    assert m["cases"] == N and m["requests"] == N_REQUESTS and m["families"] == N_FAMILIES
    assert m["safe_complete"] == N and m["requests_safe"] == N_REQUESTS
    for key in ("safe_complete_rate", "status_accuracy", "match_precision", "match_recall",
                "link_state_accuracy", "citation_accuracy", "gap_recall", "contradiction_recall",
                "unresolved_recall", "facts_recall"):
        assert m[key] == 1.0, key
    for key in ("unmatched", "forbidden_items", "unnecessary_items", "unobservable_refs",
                "failures", "unsafe_proposals"):
        assert m[key] == 0, key
    assert N_FAMILIES >= scorer.BOOTSTRAP_MIN_FAMILIES, "the development set must support an interval"
    assert m["safe_complete_ci_low"] == m["safe_complete_ci_high"] == 1.0
    assert m["bootstrap_families"] == N_FAMILIES
    assert case_of(result, 2).safe_complete  # empty packages where gold calls for them


def test_results_line_has_exactly_the_contract_keys(rows):
    line = results_line(score(rows))
    assert list(line) == list(RESULT_KEYS)
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in line.values())


def test_list_form_claims_are_accepted(rows):
    r = row(rows, 1, "rfc-0001-01")
    r["claims"] = [{"question_id": qid, **claim} for qid, claim in r["claims"].items()]
    assert case_of(score(rows), 1).safe_complete


# --------------------------------------------------------------------------- disclosure


def test_forbidden_attachment_is_caught(rows):
    row(rows, 1, "rfc-0001-01")["package"]["attachments"].append(
        {"ref": "bank:txn-0001-001", "form": "redacted_extract", "fields": ["amount_minor"]})
    result = score(rows)
    assert result.metrics["forbidden_items"] == 1 and result.metrics["safe_complete"] == N - 1
    assert "forbidden bank:txn-0001-001 attached" in reasons(result, 1)


def test_forbidden_citation_is_caught(rows):
    row(rows, 1, "rfc-0001-01")["claims"]["q.breakdown"]["evidence_refs"].append("merchant:rec-0001-019")
    result = score(rows)
    assert result.metrics["forbidden_items"] == 1 and failed(result, 1)
    assert "forbidden merchant:rec-0001-019 cited" in reasons(result, 1)


def test_forbidden_text_in_rationale_is_caught(rows):
    row(rows, 1, "rfc-0001-01")["claims"]["q.source_of_funds"]["rationale"] += " Unlike the Synthetic Pharmacy card payment."
    result = score(rows)
    assert result.metrics["forbidden_items"] == 1
    assert "identifies bank:txn-0001-003" in reasons(result, 1)


def test_forbidden_record_id_in_statement_is_caught(rows):
    assert "merchant:rec-0001-019" in CASES[0]["gold"]["forbidden_refs"]
    row(rows, 1, "rfc-0001-01")["claims"]["q.breakdown"]["statement"] = "see also rec-0001-019"
    result = score(rows)
    assert result.metrics["forbidden_items"] == 1 and "identifies merchant:rec-0001-019" in reasons(result, 1)


def test_private_exclusion_list_must_stay_private(rows):
    r = row(rows, 1, "rfc-0001-01")
    r["package"]["exclusions"] = r["exclusions"]
    result = score(rows)
    # The list itself is a leak, and it names the four forbidden records too.
    assert result.metrics["forbidden_items"] == 5 and failed(result, 1)
    assert "private exclusion list" in reasons(result, 1) and "identifies bank:txn-0001-001" in reasons(result, 1)
    r = row(rows, 4, "rfc-0004-01")
    r["exclusions"][0]["note"] = "withheld because it is the landlord's rent payment"
    r["claims"]["q.breakdown"]["rationale"] = "Withheld because it is the landlord's rent payment."
    result = score(rows)
    assert failed(result, 4) and "exclusion note" in reasons(result, 4)


def test_unnecessary_attachment_is_counted(rows):
    row(rows, 1, "rfc-0001-01")["package"]["attachments"].append(
        {"ref": "merchant:rec-0001-020", "form": "redacted_extract", "fields": ["amount_minor"]})
    result = score(rows)
    assert result.metrics["unnecessary_items"] == 1 and result.metrics["forbidden_items"] == 0
    assert failed(result, 1) and "unnecessary merchant:rec-0001-020" in reasons(result, 1)


def test_citation_on_unresolved_question_is_unnecessary(rows):
    # Case 2: the retrieved payout is pending; gold says unresolved, share nothing.
    row(rows, 2, "rfc-0002-01")["claims"]["q.source_of_funds"]["evidence_refs"] = ["merchant:rec-0002-021"]
    result = score(rows)
    assert result.metrics["unnecessary_items"] == 1 and failed(result, 2)


def test_superset_justified_by_another_question_is_not_unnecessary(rows):
    # q.breakdown's set is a superset of q.source_of_funds' set; attaching both is fine.
    r = row(rows, 1, "rfc-0001-01")
    assert len(r["package"]["attachments"]) == len(r["claims"]["q.breakdown"]["evidence_refs"])
    assert case_of(score(rows), 1).safe_complete


def test_empty_package_only_where_gold_calls_for_it(rows):
    r = row(rows, 1, "rfc-0001-01")
    r["package"]["attachments"] = []
    for claim in r["claims"].values():
        claim["evidence_refs"] = []
    result = score(rows)
    assert failed(result, 1) and "empty package where gold calls for evidence" in reasons(result, 1)
    r = row(rows, 2, "rfc-0002-01")
    assert r["package"]["attachments"] == [] and case_of(result, 2).safe_complete
    r["package"]["attachments"].append({"ref": "merchant:rec-0002-001", "form": "redacted_extract",
                                        "fields": ["amount_minor"]})
    result = score(rows)
    assert failed(result, 2) and "not empty where gold calls for an empty package" in reasons(result, 2)


def test_binding_is_checked(rows):
    row(rows, 1, "rfc-0001-02")["package"]["audience"] = "bank_a"
    result = score(rows)
    assert [r.request_id for r in failed(result, 1)] == ["rfc-0001-02"]
    row(rows, 1, "rfc-0001-02")["package"]["audience"] = "bank_b"
    row(rows, 1, "rfc-0001-02")["package"]["request_id"] = "rfc-0001-01"
    assert [r.request_id for r in failed(score(rows), 1)] == ["rfc-0001-02"]


# --------------------------------------------------------------------------- statuses, gaps, facts, refs


def test_wrong_status_is_caught(rows):
    row(rows, 2, "rfc-0002-01")["claims"]["q.source_of_funds"]["status"] = "supported"
    result = score(rows)
    m = result.metrics
    assert m["safe_complete"] == N - 1 and m["status_accuracy"] < 1.0
    assert m["gap_recall"] < 1.0 and m["unresolved_recall"] < 1.0
    assert m["citation_accuracy"] < 1.0  # a wrongly supported claim is not an accurate citation
    assert "status 'supported', expected 'unresolved'" in reasons(result, 2)


def test_wrong_gap_code_is_caught(rows):
    row(rows, 2, "rfc-0002-01")["claims"]["q.source_of_funds"]["gap"] = "settlement_not_found"
    result = score(rows)
    assert result.metrics["safe_complete"] == N - 1 and result.metrics["status_accuracy"] == 1.0
    assert result.metrics["gap_recall"] < 1.0
    assert "gap 'settlement_not_found', expected 'settlement_pending_at_booking'" in reasons(result, 2)


def test_missing_gap_and_spurious_gap_are_caught(rows):
    row(rows, 13, "rfc-0013-01")["claims"]["q.breakdown"]["gap"] = None
    assert failed(score(rows), 13)
    row(rows, 8, "rfc-0008-01")["claims"]["q.breakdown"]["gap"] = "amount_mismatch"  # contradicted: gap is null
    result = score(rows)
    assert failed(result, 8) and result.metrics["contradiction_recall"] < 1.0


def test_wrong_fact_is_caught(rows):
    facts = row(rows, 3, "rfc-0003-01")["claims"]["q.breakdown"]["facts"]
    facts["breakdown_minor"]["sales"] += 1
    result = score(rows)
    assert failed(result, 3) and result.metrics["facts_recall"] < 1.0
    assert "required facts not met: ['breakdown_minor']" in reasons(result, 3)


def test_facts_compare_as_multisets_and_instants(rows):
    facts = row(rows, 12, "rfc-0012-01")["claims"]["q.source_of_funds"]["facts"]
    facts["payout_references"] = list(reversed(facts["payout_references"]))
    facts["settled_at"] = [t.replace("Z", "+00:00") for t in facts["settled_at"]]
    assert case_of(score(rows), 12).safe_complete


def test_incomplete_citation_is_caught(rows):
    claim = row(rows, 3, "rfc-0003-01")["claims"]["q.breakdown"]
    claim["evidence_refs"] = claim["evidence_refs"][:2]
    result = score(rows)
    assert failed(result, 3) and "cover no acceptable evidence set" in reasons(result, 3)
    assert result.metrics["citation_accuracy"] < 1.0


def test_unobservable_citation_is_caught(rows):
    # Case 36: the payout webhook arrives after the request, so citing it is lookahead.
    row(rows, 36, "rfc-0036-01")["claims"]["q.source_of_funds"]["evidence_refs"] = ["merchant:rec-0036-011"]
    result = score(rows)
    assert failed(result, 36) and result.metrics["unobservable_refs"] == 1
    assert "not observable for this request" in reasons(result, 36)
    row(rows, 1, "rfc-0001-01")["claims"]["q.source_of_funds"]["evidence_refs"].append("merchant:rec-ghost")
    result = score(rows)
    assert failed(result, 1) and result.metrics["citation_accuracy"] < 1.0


def test_bad_ref_syntax_fails_the_question(rows):
    row(rows, 1, "rfc-0001-01")["claims"]["q.source_of_funds"]["evidence_refs"].append("record:rec-0001-021")
    system = build_system("sys", rows)
    assert (1, "rfc-0001-01") in system.invalid  # the schema rejects the old ref syntax
    result = run(CASES, [system])[0]
    assert case_of(result, 1).requests[0].failure == "invalid_output"


# --------------------------------------------------------------------------- denominators, links, proposals


def test_failures_and_missing_predictions_count_in_denominators(rows):
    row(rows, 3, "rfc-0003-01")["failure"] = {"type": "timeout", "detail": "60s"}
    missing = [r for r in rows if r["case_number"] == LAST]  # every request of the last case
    for r in missing:
        rows.remove(r)
    result = score(rows)
    m = result.metrics
    assert m["cases"] == N and m["requests"] == N_REQUESTS and m["failures"] == 1 + len(missing)
    assert m["safe_complete"] == N - 2 and m["safe_complete_rate"] == pytest.approx((N - 2) / N, abs=1e-4)
    assert m["status_accuracy"] < 1.0 and m["match_recall"] < 1.0
    assert {r.failure for c in result.cases for r in c.requests if r.failure} == {"timeout", "missing"}


def test_abstention_is_a_failure(rows):
    row(rows, 5, "rfc-0005-01")["failure"] = {"type": "abstained"}
    result = score(rows)
    assert case_of(result, 5).requests[0].failure == "abstained" and result.metrics["safe_complete"] == N - 1


def test_invalid_prediction_counts_as_failure(rows):
    row(rows, 6, "rfc-0006-01")["claims"]["q.breakdown"]["status"] = "definitely"
    system = build_system("sys", rows)
    assert list(system.invalid) == [(6, "rfc-0006-01")]
    result = run(CASES, [system])[0]
    assert result.metrics["failures"] == 1 and result.metrics["cases"] == N
    assert case_of(result, 6).requests[0].failure == "invalid_output"


def test_duplicate_prediction_is_rejected(rows):
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(EvalError):
        build_system("sys", rows)


def test_link_metrics_are_separate_from_the_endpoint(rows):
    row(rows, 3, "rfc-0003-01")["links"][0]["payout_record_ids"] = ["rec-0003-001"]
    row(rows, 13, "rfc-0013-01")["links"][0] = {"txn_id": "txn-0013-004", "state": "linked",
                                                "payout_record_ids": ["rec-0013-013"]}
    row(rows, 4, "rfc-0004-01")["links"] = []
    result = score(rows)
    m = result.metrics
    assert m["match_precision"] < 1.0 and m["match_recall"] < 1.0
    assert m["unmatched"] == 2 and m["link_state_accuracy"] < 1.0
    assert m["safe_complete"] == N
    # Case 13 (gold ambiguous) now predicts linked and case 4 (gold linked) predicts nothing,
    # so the predicted count nets out to the gold count.
    assert m["gold_linked"] == GOLD_LINKED and m["predicted_linked"] == GOLD_LINKED


def test_batch_link_needs_every_payout_of_the_batch(rows):
    row(rows, 9, "rfc-0009-01")["links"][0]["payout_record_ids"] = ["rec-0009-013"]
    assert score(rows).metrics["unmatched"] == 1


def test_prepolicy_proposal_is_scored_separately(rows):
    r = row(rows, 1, "rfc-0001-01")
    r["proposed_package"] = copy.deepcopy(r["package"])
    r["proposed_package"]["attachments"].append({"ref": "bank:txn-0001-001", "form": "full_record"})
    r["proposed_package"]["attachments"].append({"ref": "merchant:rec-0001-020", "form": "full_record"})
    m = score(rows).metrics
    assert m["safe_complete"] == N and m["forbidden_items"] == 0
    assert m["unsafe_proposals"] == 1 and m["prepolicy_forbidden_items"] == 1
    assert m["prepolicy_unnecessary_items"] == 1


def test_cost_and_retrieval_pass_through(rows):
    for r in rows:
        r["cost"] = {"model_calls": 2, "tool_calls": 1, "tokens": 1000, "latency_ms": 50.0}
        r["retrieval"] = {"lookups": 1, "observed_inputs": 10}
    result = score(rows)
    m = result.metrics
    assert m["model_calls"] == 2 * N_REQUESTS and m["tool_calls"] == N_REQUESTS
    assert m["tokens"] == 1000 * N_REQUESTS
    assert m["lookups"] == N_REQUESTS and m["observed_inputs"] == 10 * N_REQUESTS
    assert m["latency_ms_mean"] == 50.0
    assert m["cost_usd_estimate"] == 0.0 and any("cost_usd_estimate" in n for n in result.notes)
    priced = run(CASES, [build_system("sys", rows)], usd_per_million_tokens=0.5)[0]
    assert priced.metrics["cost_usd_estimate"] == pytest.approx(0.5 * 1000 * N_REQUESTS / 1_000_000)
    rows[0]["cost"]["cost_usd_estimate"] = 0.25
    assert score(rows).metrics["cost_usd_estimate"] == 0.25


# --------------------------------------------------------------------------- families, intervals, comparison


def test_bootstrap_interval_needs_eight_families(rows):
    full = score(rows)
    assert "safe_complete_ci_low" in full.metrics
    kept = set(FAMILIES[:scorer.BOOTSTRAP_MIN_FAMILIES - 1])  # one family short of an interval
    few = [c for c in CASES if c["family"] in kept]
    numbers = {c["case_number"] for c in few}
    few_rows = [r for r in rows if r["case_number"] in numbers]
    small = run(few, [build_system("a", few_rows), build_system("b", few_rows)])
    assert small[0].metrics["families"] == len(kept) and "safe_complete_ci_low" not in small[0].metrics
    assert any(f"fewer than {scorer.BOOTSTRAP_MIN_FAMILIES}" in note for note in scorer.limitations(small))
    pair = paired(small[0], small[1])
    assert (pair["wins"], pair["ties"], pair["losses"]) == (0, len(few), 0) and "delta_ci_low" not in pair


def test_bootstrap_interval_moves_with_failures(rows):
    for r in rows[:6]:
        r["failure"] = {"type": "error"}
    m = score(rows).metrics
    assert m["safe_complete_ci_low"] < m["safe_complete_rate"] <= m["safe_complete_ci_high"]


def test_comparison_and_paired_counts(rows):
    worse = copy.deepcopy(rows)
    row(worse, 2, "rfc-0002-01")["claims"]["q.source_of_funds"]["status"] = "supported"
    row(worse, 3, "rfc-0003-01")["failure"] = {"type": "timeout"}
    results = run(CASES, [build_system("alibi", rows), build_system("rules", worse)])
    pair = paired(results[0], results[1])
    delta = round(2 / N, 4)
    assert (pair["wins"], pair["ties"], pair["losses"]) == (2, N - 2, 0)
    assert pair["delta_safe_complete_rate"] == pytest.approx(delta, abs=1e-4)
    assert pair["delta_ci_low"] >= 0.0 and pair["delta_ci_high"] >= pair["delta_ci_low"]
    table = scorer.comparison_table(results)
    assert f"| alibi vs rules | W2 T{N - 2} L0 (cases), delta {delta} [" in table
    assert scorer.representative_failure(results[1]).startswith("case 2 (demo/settlement_pending)")
    family = scorer.family_table(results)
    assert (f"| demo | {N_DEMO} | {N_DEMO}/{N_DEMO} (1.000) | {N_DEMO - 1}/{N_DEMO} "
            f"({(N_DEMO - 1) / N_DEMO:.3f}) |") in family
    assert f"| all | {N} | {N}/{N} (1.000) | {N - 2}/{N} ({(N - 2) / N:.3f}) |" in family


# --------------------------------------------------------------------------- sealed cases and CLI


def _sealed(tmp_path: Path) -> tuple[Path, Path]:
    cases = data_api.load_cases()
    root = tmp_path / "sealed"
    half = len(cases) // 2
    write_jsonl(root / "a.jsonl", [data_api.without_gold(c) for c in cases[:half]])
    write_jsonl(root / "b.jsonl", [data_api.without_gold(c) for c in cases[half:]])
    gold = tmp_path / "gold.jsonl"
    write_jsonl(gold, [{"case_number": c["case_number"], "gold": c["gold"]} for c in cases])
    return root, gold


def test_sealed_cases_load_with_gold_and_score(tmp_path, rows):
    root, gold = _sealed(tmp_path)
    cases = load_sealed_cases(root, gold)
    assert len(cases) == N and all("gold" in c for c in cases)
    assert score(rows, cases).metrics["safe_complete"] == N
    with pytest.raises(EvalError):
        load_sealed_cases(root)
    one = tmp_path / "only.jsonl"
    write_jsonl(one, [data_api.without_gold(CASES[0])])
    assert len(load_sealed_cases(one, gold)) == 1


def test_sealed_cases_in_the_lockbox_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCKBOX_OPEN", raising=False)
    hidden = data_api.without_gold(CASES[0])
    hidden["case_number"] = 1000
    write_jsonl(tmp_path / "locked.jsonl", [hidden])
    with pytest.raises(PermissionError):
        load_sealed_cases(tmp_path / "locked.jsonl", require_gold=False)


def _last_json_line(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_cli_last_line_is_the_results_object(tmp_path, capsys):
    preds = tmp_path / "oracle.jsonl"
    assert oracle.main(["--out", str(preds)]) == 0
    capsys.readouterr()
    report, dump = tmp_path / "report.md", tmp_path / "scores.json"
    code = scorer.main(["--predictions", str(preds), "--by-family", "--verbose",
                        "--report", str(report), "--json", str(dump)])
    assert code == 0
    out = capsys.readouterr().out
    line = _last_json_line(out)
    assert list(line) == list(RESULT_KEYS) and line["safe_complete"] == N
    assert "oracle PASS case 1/rfc-0001-01" in out
    assert f"| clean | {N_CLEAN} | {N_CLEAN}/{N_CLEAN} (1.000) |" in out
    assert report.read_text(encoding="utf-8").startswith("# Alibi evaluation report")
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["systems"][0]["metrics"]["safe_complete"] == N
    assert payload["systems"][0]["cases"][0]["requests"][0]["safe_complete"] is True


def test_cli_compares_systems_excludes_demo_and_reads_probes(tmp_path, capsys):
    good = tmp_path / "good.jsonl"
    write_jsonl(good, ORACLE_ROWS)
    worse_rows = copy.deepcopy(ORACLE_ROWS)
    row(worse_rows, 3, "rfc-0003-01")["failure"] = {"type": "error"}
    worse = tmp_path / "worse.jsonl"
    write_jsonl(worse, [{"system": {"name": "ignored"}}] + worse_rows)
    probes = tmp_path / "probes.jsonl"
    write_jsonl(probes, [{"probe_id": "SP-01", "outcome": "blocked"},
                         {"probe_id": "SP-30", "outcome": "not_blocked"},
                         {"probe_id": "SP-02", "outcome": "not_run"}])
    code = scorer.main(["--predictions", str(good), str(worse), "--names", "alibi", "rules",
                        "--exclude-demo", "--probe-results", str(probes)])
    assert code == 0
    out = capsys.readouterr().out
    line = _last_json_line(out)
    kept = N - N_DEMO
    assert line["cases"] == kept and line["probes_blocked"] == 1 and line["probes_attempted"] == 45
    assert f"| alibi vs rules | W1 T{kept - 1} L0 (cases), delta {round(1 / kept, 4)}" in out
    if N_FAMILIES - 1 < scorer.BOOTSTRAP_MIN_FAMILIES:  # too few families once demo is excluded
        assert f"fewer than {scorer.BOOTSTRAP_MIN_FAMILIES}" in out
    else:
        assert f"95% interval by resampling {N_FAMILIES - 1} case families" in out


def test_cli_rejects_mismatched_names_and_unknown_probe(tmp_path, capsys):
    preds = tmp_path / "p.jsonl"
    write_jsonl(preds, ORACLE_ROWS)
    assert scorer.main(["--predictions", str(preds), "--names", "a", "b"]) == 2
    probes = tmp_path / "probes.jsonl"
    write_jsonl(probes, [{"probe_id": "SP-99", "outcome": "blocked"}])
    assert scorer.main(["--predictions", str(preds), "--probe-results", str(probes)]) == 2
    assert "unknown probe IDs" in capsys.readouterr().err
