"""The adapter: team cases through the simulated services into predictions.

No network and no Gemini: the model modes run with ``alibi.llm.fake.FakeClient``. Every store the
adapter seeds lives under the test's ``tmp_path`` (``root=``), never under ``data/alibi``.
"""

from __future__ import annotations

import copy
import json

import pytest

from agents.data import api as data_api
from alibi import api
from alibi.adapter import MODES, STRATEGIES, main, normalise_mode, run_case, run_cases, system_label
from alibi.eval import jsonschema_lite
from alibi.eval.common import GAP_CODES, load_schema
from alibi.llm.client import Unavailable
from alibi.llm.fake import FakeClient

SCHEMA = load_schema("prediction")
RFC_A, RFC_B = "rfc-0001-01", "rfc-0001-02"
Q_SOURCE, Q_BREAKDOWN, Q_PERIOD = "q.source_of_funds", "q.breakdown", "q.sales_refunds_in_period"
PRIVATE_CASE_1 = {"bank:txn-0001-001", "bank:txn-0001-002", "bank:txn-0001-003", "merchant:rec-0001-019"}


def by_request(predictions):
    return {p["request_id"]: p for p in predictions}


def disclosed(prediction) -> set[str]:
    refs = {a["ref"] for a in prediction["package"]["attachments"]}
    for claim in prediction["claims"].values():
        refs.update(claim["evidence_refs"])
    return refs


def assert_valid(predictions):
    assert predictions, "a case with RFCs yields predictions"
    for prediction in predictions:
        assert jsonschema_lite.errors(SCHEMA, prediction) == []


# --------------------------------------------------------------------------- rules, case 1


@pytest.fixture(scope="module")
def case1_rules(tmp_path_factory):
    return by_request(run_case(1, "rules", root=tmp_path_factory.mktemp("adapter-rules-1")))


def test_case_1_rules_answers_bank_a_with_the_retrieved_settlement(case1_rules):
    assert_valid(list(case1_rules.values()))
    a = case1_rules[RFC_A]
    assert a["failure"] is None and a["system"] == "rules"
    source = a["claims"][Q_SOURCE]
    assert source["status"] == "supported" and source["gap"] is None
    assert source["facts"]["payout_references"] == ["PO-0001-01"]
    assert {"bank:txn-0001-004", "merchant:rec-0001-021"} <= set(source["evidence_refs"])
    breakdown = a["claims"][Q_BREAKDOWN]
    assert breakdown["status"] == "supported"
    assert breakdown["facts"]["breakdown_minor"] == {"sales": 240000, "refunds": 18000,
                                                     "fees": 6000, "net": 216000}
    # The settlement record is lookup-only in case 1: it came through the merchant connector.
    assert a["retrieval"]["lookups"] >= 1
    assert a["retrieval"]["strategy"] == STRATEGIES["rules"]
    assert a["cost"]["model_calls"] == 0 and a["cost"]["tool_calls"] >= 1
    [link] = a["links"]
    assert link["txn_id"] == "txn-0001-004" and link["state"] == "linked"
    assert link["payout_record_ids"] == ["rec-0001-021"]


def test_case_1_rules_answers_bank_b_with_the_period_totals(case1_rules):
    b = case1_rules[RFC_B]
    assert b["failure"] is None
    period = b["claims"][Q_PERIOD]
    assert period["status"] == "supported"
    assert period["facts"] == {"sales_minor": 240000, "sale_count": 8,
                               "refunds_minor": 18000, "refund_count": 2}
    assert len(period["evidence_refs"]) == 10
    assert b["package"]["request_id"] == RFC_B and b["package"]["audience"] == "bank_b"
    assert all(a["ref"].startswith("merchant:") for a in b["package"]["attachments"])


def test_case_1_private_items_are_excluded_from_both_packages(case1_rules):
    for prediction in case1_rules.values():
        assert not (disclosed(prediction) & PRIVATE_CASE_1)
        text = str(prediction["package"])
        for ident in ("txn-0001-001", "txn-0001-002", "txn-0001-003", "rec-0001-019"):
            assert ident not in text
        excluded = {e["ref"] for e in prediction["exclusions"]}
        assert PRIVATE_CASE_1 <= excluded
        assert all(e["reason"] for e in prediction["exclusions"])
        assert "exclusions" not in prediction["package"]


def test_case_1_package_attachments_carry_form_fields_and_trust(case1_rules):
    a = case1_rules[RFC_A]
    assert a["package"]["empty"] is False
    forms = {att["form"] for att in a["package"]["attachments"]}
    assert forms <= {"full_record", "redacted_extract"}
    for att in a["package"]["attachments"]:
        assert att["source_trust"] in ("issuer_verified", "bank_observed", "self_supplied")
        if att["form"] == "redacted_extract":
            assert att["fields"] == sorted(att["content"])
    # The pre-policy proposal (the unfiltered cited set) has the same shape.
    assert a["proposed_package"]["request_id"] == RFC_A
    assert {"bank:txn-0001-004", "merchant:rec-0001-021"} <= {
        att["ref"] for att in a["proposed_package"]["attachments"]}


def test_each_rfc_is_answered_at_its_own_issue_time(case1_rules):
    case = data_api.load_case(1)
    issued = {r["request_id"]: r["issued_at"] for r in case["rfcs"]}
    assert case1_rules[RFC_A]["issued_at"] == issued[RFC_A]
    assert case1_rules[RFC_B]["issued_at"] == issued[RFC_B]
    assert issued[RFC_A] < issued[RFC_B]


# --------------------------------------------------------------------------- rules, other cases


def test_case_2_rules_does_not_support_the_source_of_funds(tmp_path):
    predictions = run_case(2, "rules", root=tmp_path)
    assert_valid(predictions)
    a = by_request(predictions)["rfc-0002-01"]
    source = a["claims"][Q_SOURCE]
    assert source["status"] in ("unresolved", "contradicted")
    if source["status"] == "unresolved":
        assert source["gap"] in GAP_CODES
    else:
        assert source["gap"] is None
    assert source["facts"] == {}


def test_second_platform_case_runs_and_validates(tmp_path):
    predictions = run_case(71, "rules", root=tmp_path)
    assert_valid(predictions)
    case = data_api.load_case(71)
    assert {p["request_id"] for p in predictions} == {r["request_id"] for r in case["rfcs"]}
    for prediction in predictions:
        assert prediction["case_number"] == 71
        assert set(prediction["claims"]) == set(
            next(r for r in case["rfcs"] if r["request_id"] == prediction["request_id"])["question_ids"])


def test_run_case_accepts_a_case_dict_without_gold(tmp_path):
    case = data_api.without_gold(data_api.load_case(1))
    predictions = run_case(case, "rules", root=tmp_path, as_of="2026-11-04T14:00:00Z")
    assert_valid(predictions)
    # With a fixed clock every visible RFC is answered at that moment.
    assert {p["issued_at"] for p in predictions} == {"2026-11-04T14:00:00Z"}
    assert by_request(predictions)[RFC_A]["claims"][Q_SOURCE]["status"] == "supported"


# --------------------------------------------------------------------------- model modes


def test_agents_mode_with_an_unavailable_model_yields_a_typed_failure(tmp_path):
    client = FakeClient({"*": Unavailable("simulated outage")})
    predictions = run_case(1, "agents", root=tmp_path, client=client)
    assert_valid(predictions)
    for prediction in predictions:
        assert prediction["failure"]["type"] == "unavailable"
        assert prediction["system"] == "agents"
        assert prediction["cost"]["model_calls"] == 0
        assert prediction["retrieval"]["strategy"] == STRATEGIES["agents"]
    assert client.call_count >= 1


def test_one_pass_mode_runs_with_a_scripted_proposal(tmp_path):
    client = FakeClient({"*": {"claims": [], "disclosures": [], "rationale": "No proposal."}})
    predictions = run_case(1, "one_pass", root=tmp_path, client=client)
    assert_valid(predictions)
    a = by_request(predictions)[RFC_A]
    assert a["failure"] is None and a["system"] == "one_pass"
    assert a["cost"]["model_calls"] == 1 and a["cost"]["tokens"] == 20
    assert a["retrieval"]["strategy"] == STRATEGIES["one_pass"]
    assert a["proposed_package"]["attachments"] == [] and a["proposed_package"]["claims"] == []
    assert set(a["claims"]) == {Q_SOURCE, Q_BREAKDOWN}
    assert client.call_count == 2    # one call per request


def test_one_pass_budget_refusal_is_a_typed_failure(tmp_path):
    from alibi.llm.client import BudgetExceeded
    client = FakeClient({"*": BudgetExceeded("process cap")})
    predictions = run_case(1, "one_pass", root=tmp_path, client=client)
    assert_valid(predictions)
    assert {p["failure"]["type"] for p in predictions} == {"budget_exceeded"}


# --------------------------------------------------------------------------- api dispatch and labels


def test_modes_and_labels():
    assert MODES == ("rules", "one_pass", "agents")
    assert normalise_mode("single_call") == "one_pass" and normalise_mode("alibi") == "agents"
    with pytest.raises(ValueError):
        normalise_mode("guess")
    label = system_label("one_pass")
    assert set(label) == {"name", "model", "commit"} and label["name"] == "one_pass"
    assert system_label("rules")["model"] == "none"


def test_api_run_investigation_rejects_an_unknown_mode(tmp_path):
    store = api.open_store(tmp_path / "p.db")
    try:
        with pytest.raises(NotImplementedError):
            api.run_investigation(store, "rfc-none", mode="guess")
    finally:
        store.close()


def test_one_pass_runs_leave_the_same_audit_row_as_the_other_modes(tmp_path):
    """one_pass goes through run_one_pass (for the proposal), so the adapter writes the
    run_investigation audit row itself; the passport then records every mode's runs alike."""
    client = FakeClient({"*": {"claims": [], "disclosures": [], "rationale": "No proposal."}})
    run_case(1, "one_pass", root=tmp_path, client=client)
    store = api.open_store(tmp_path / "case_1" / "passport.db")
    try:
        for request_id in (RFC_A, RFC_B):
            rows = [e for e in store.list_audit(target_id=request_id)
                    if e.action == "run_investigation"]
            assert [e.actor for e in rows] == ["one_pass"], request_id
            assert rows[0].outcome == "ok" and rows[0].target_id == request_id
            assert "(single_call)" in rows[0].detail and "gateway calls" in rows[0].detail
    finally:
        store.close()


# --------------------------------------------------------------------------- the clock, no lookahead

LATE_RECORD = "rec-0001-099"
BETWEEN_LOOKUP, BETWEEN_PUSH = "2026-11-03T00:00:00Z", "2026-11-03T01:00:00Z"


def case_with_data_between_the_rfcs() -> dict:
    """Case 1 with its lookup-only settlement and one extra order arriving between rfc-0001-01
    (issued 2026-11-02T09:00Z) and rfc-0001-02 (2026-11-04T14:00Z). No dev case has data between
    its RFCs, so the clock-advance path is pinned with this synthetic case."""
    case = copy.deepcopy(data_api.without_gold(data_api.load_case(1)))
    [lookup] = case["merchant_lookup"]
    assert lookup["event"]["record_id"] == "rec-0001-021"
    lookup["available_from"] = BETWEEN_LOOKUP
    late = copy.deepcopy(case["merchant_deliveries"][0])
    assert late["event"]["kind"] == "sale"
    late["received_at"] = BETWEEN_PUSH
    late["event"].update(event_id="evt-0001-099", record_id=LATE_RECORD)
    case["merchant_deliveries"].append(late)
    issued = [r["issued_at"] for r in case["rfcs"]]
    assert issued[0] < BETWEEN_LOOKUP < BETWEEN_PUSH < issued[1]
    return case


def refs_known_at(case: dict, at: str) -> set[str]:
    """Every gold ref a system could have seen by ``at`` (lookup-only records included)."""
    view = data_api.known_at(case, at, include_lookup=True)
    known = {f"merchant:{d['event']['record_id']}" for d in view["merchant_deliveries"]}
    known |= {f"merchant:{d['event']['record_id']}" for d in view["merchant_lookup"]}
    known |= {f"import:{d['record']['record_id']}" for d in view["imports"]}
    known |= {f"bank:{d['row']['txn_id']}" for d in view["bank_deliveries"]}
    return known


def every_ref(prediction) -> set[str]:
    refs = disclosed(prediction)
    refs |= {e["ref"] for e in prediction["exclusions"]}
    refs |= {a["ref"] for a in (prediction["proposed_package"] or {}).get("attachments", [])}
    for link in prediction["links"]:
        refs |= {f"merchant:{r}" for r in link["payout_record_ids"] + link["candidate_record_ids"]}
    return refs


def test_an_rfc_is_answered_before_later_data_lands_and_the_next_one_sees_it(tmp_path):
    case = case_with_data_between_the_rfcs()
    predictions = run_case(case, "rules", root=tmp_path)
    assert_valid(predictions)
    a, b = by_request(predictions)[RFC_A], by_request(predictions)[RFC_B]
    assert a["issued_at"] == case["rfcs"][0]["issued_at"]
    assert b["issued_at"] == case["rfcs"][1]["issued_at"]

    # Bank A at 2026-11-02: the settlement is not yet available to the merchant lookup, so the
    # engine looks (lookups 1) and finds nothing; the answer is unresolved with the pinned gap.
    source = a["claims"][Q_SOURCE]
    assert source["status"] == "unresolved" and source["gap"] == "settlement_not_found"
    assert a["retrieval"]["lookups"] == 1
    assert a["links"] and a["links"][0]["state"] != "linked"
    text_a = json.dumps(a, sort_keys=True)
    assert "rec-0001-021" not in text_a and LATE_RECORD not in text_a
    assert a["retrieval"]["observed_inputs"] == 24

    # Bank B at 2026-11-04: the clock moved, the late order was pushed and is now in the passport.
    assert b["retrieval"]["observed_inputs"] == 25
    assert f"merchant:{LATE_RECORD}" in every_ref(b)
    assert f"merchant:{LATE_RECORD}" not in every_ref(a)
    # Neither prediction mentions anything the case had not delivered or made available by then.
    for prediction in (a, b):
        assert every_ref(prediction) <= refs_known_at(case, prediction["issued_at"]), prediction["request_id"]

    # The store itself only ever saw the late order through the clock advance, after Bank A.
    store = api.open_store(tmp_path / "case_1" / "passport.db")
    try:
        record = store.get_source(LATE_RECORD)
        assert record.record_id == LATE_RECORD
        runs = [e for e in store.list_audit(target_id=RFC_A) if e.action == "run_investigation"]
        assert len(runs) == 1
    finally:
        store.close()


def test_the_dev_case_predictions_never_cite_what_was_not_yet_known(case1_rules):
    case = data_api.load_case(1)
    for prediction in case1_rules.values():
        assert every_ref(prediction) <= refs_known_at(case, prediction["issued_at"])


# --------------------------------------------------------------------------- run_cases and the CLI


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_run_cases_writes_the_system_line_then_one_prediction_per_request(tmp_path):
    lines = []
    cases = [data_api.load_case(1)]
    summary = run_cases(cases, "rules", out=tmp_path / "predictions", root=tmp_path / "stores",
                        log=lines.append)
    rows = read_jsonl(tmp_path / "predictions" / "rules.jsonl")
    assert rows[0] == {"system": system_label("rules")}
    assert set(rows[0]["system"]) == {"name", "model", "commit"}
    predictions = rows[1:]
    assert [p["request_id"] for p in predictions] == [RFC_A, RFC_B]
    assert_valid(predictions)
    assert summary["mode"] == "rules" and summary["cases"] == 1 and summary["requests"] == 2
    assert summary["failures"] == 0 and summary["lookups"] >= 1 and summary["model_calls"] == 0
    assert summary["observed_inputs"] == sum(p["retrieval"]["observed_inputs"] for p in predictions)
    assert summary["out"] == str(tmp_path / "predictions" / "rules.jsonl")
    assert len(lines) == 1 and lines[0].startswith("case 1 rules: 2 request(s), 0 failure(s)")
    # The stores went under root, not under data/alibi.
    assert (tmp_path / "stores" / "case_1" / "passport.db").exists()


def test_run_cases_turns_a_seeding_error_into_typed_failures(tmp_path):
    broken = data_api.without_gold(data_api.load_case(1))
    del broken["subject_ref"]                      # run_case refuses it before any seeding
    summary = run_cases([broken], "rules", out=tmp_path / "predictions", root=tmp_path / "stores",
                        log=lambda _text: None)
    rows = read_jsonl(tmp_path / "predictions" / "rules.jsonl")
    assert summary["requests"] == 2 and summary["failures"] == 2
    assert {p["failure"]["type"] for p in rows[1:]} == {"error"}
    assert_valid(rows[1:])


def test_main_refuses_an_out_folder_outside_the_data_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    code = main(["--mode", "rules", "--cases", "1", "--out", str(tmp_path / "elsewhere")])
    assert code == 2
    captured = capsys.readouterr()
    assert "--out must be under" in captured.err and captured.out == ""
    assert not (tmp_path / "elsewhere").exists()


def test_main_runs_the_selected_cases_and_prints_the_summary_last(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ALIBI_DATA_DIR", str(data_dir))
    out = data_dir / "predictions"
    code = main(["--mode", "rules", "--cases", "1", "--families", "demo", "--out", str(out)])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("case 1 rules: 2 request(s), 0 failure(s)")
    summary = json.loads(lines[-1])
    assert summary["cases"] == 1 and summary["requests"] == 2 and summary["failures"] == 0
    assert set(summary) == {"mode", "cases", "requests", "failures", "lookups", "observed_inputs",
                            "model_calls", "tokens", "out"}
    rows = read_jsonl(out / "rules.jsonl")
    assert rows[0]["system"]["name"] == "rules" and len(rows) == 3
    # Every file the CLI writes is under the data dir: the predictions and the per-case stores.
    assert (data_dir / "adapter" / "rules" / "case_1" / "passport.db").exists()


def test_main_with_no_matching_case_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "data"))
    code = main(["--cases", "1", "--families", "no-such-family",
                 "--out", str(tmp_path / "data" / "predictions")])
    assert code == 2 and "no visible cases match" in capsys.readouterr().err
