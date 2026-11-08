"""Public input contract check: native import semantics, typed safe codes, inputs untouched."""

import copy
import json

import pytest

import agents.data.api as data_api
from agents.data.api import load_case, without_gold
from agents.eval import input_contract
from agents.eval.input_contract import main, validate_case_contract

SENTINEL = "ZQXSENTINEL7731"


@pytest.fixture
def case39():
    return without_gold(load_case(39))


def with_imports(case, *records):
    at = case["imports"][0]["received_at"]
    return {**copy.deepcopy(case), "imports": [{"received_at": at, "record": r} for r in records]}


def test_public_case39_import_is_valid_and_tagged_self_supplied(case39):
    report = validate_case_contract(case39)
    assert report["valid"] and report["issues"] == [] and report["imports"] == 1
    [row] = report["accepted"]
    assert row["shape"] == "case_shaped" and row["trust"] == "self_supplied"


def test_unsupported_kind_with_amount_minor_is_a_case_kind_refusal(case39):
    record = {**case39["imports"][0]["record"], "kind": "invoice"}
    [issue] = validate_case_contract(with_imports(case39, record))["issues"]
    assert issue["stage"] == "native_import" and issue["shape"] == "case_shaped"
    assert issue["code"] == "case_kind_unsupported" and issue["fields"] == ["kind"]


def test_unsupported_kind_without_amount_minor_fails_core_schema(case39):
    record = {k: v for k, v in case39["imports"][0]["record"].items() if k != "amount_minor"}
    record["kind"] = "invoice"
    [issue] = validate_case_contract(with_imports(case39, record))["issues"]
    assert issue["stage"] == "native_import" and issue["shape"] == "core_shaped"
    assert issue["code"] == "core_schema_invalid"
    assert "kind" in issue["fields"] and "kind:invalid_value" in issue["problems"]
    assert "<extra field>:not_permitted" in issue["problems"]


def test_valid_core_shaped_import_keeps_core_defaults(case39):
    receipt = {"kind": "receipt", "amount_pence": 4599, "currency": "GBP",
               "occurred_at": "2026-07-10T13:20:00Z", "text": "desk lamp"}
    report = validate_case_contract(with_imports(case39, receipt))
    assert report["valid"]
    [row] = report["accepted"]
    assert row["shape"] == "core_shaped" and row["trust"] == "self_supplied"
    assert row["defaulted"] == ["subject_ref", "issuer_id", "revision", "record_id", "event_id"]


def test_wrapped_core_event_reports_defaults_of_the_inner_event(case39):
    event = {"kind": "receipt", "amount_pence": 4599, "currency": "GBP",
             "occurred_at": "2026-07-10T13:20:00Z", "record_id": "rec-w1", "event_id": "evt-w1",
             "issuer_id": "shop-w1", "revision": 2, "subject_ref": case39["subject_ref"]}
    partial = {k: v for k, v in event.items() if k not in ("issuer_id", "revision")}
    wrapped = [{"event": event, "signature": {"kid": SENTINEL}}, {"event": {**partial, "record_id": "rec-w2"}}]
    report = validate_case_contract(with_imports(case39, *wrapped))
    assert report["valid"] and SENTINEL not in json.dumps(report)
    full, part = report["accepted"]
    assert full["shape"] == part["shape"] == "core_shaped" and full["trust"] == "self_supplied"
    assert full["defaulted"] == [] and part["defaulted"] == ["issuer_id", "revision"]
    # The native door kept the inner event's own fields, so nothing was defaulted.
    from cryptography.fernet import Fernet
    from alibi.connectors.case_source import parse_time
    from alibi.connectors.self_supplied import import_record
    from alibi.core.store import Store
    with Store(":memory:", master_key=Fernet.generate_key()) as store:
        stored = import_record(store, copy.deepcopy(wrapped[0]), subject_ref=case39["subject_ref"],
                               now=parse_time(case39["imports"][0]["received_at"])).record
    assert (stored.issuer_id, stored.revision, stored.subject_ref) == ("shop-w1", 2, case39["subject_ref"])


def test_trust_claim_is_not_honoured_but_record_is_accepted(case39):
    record = {**case39["imports"][0]["record"], "claimed_trust": "issuer_verified"}
    report = validate_case_contract(with_imports(case39, record))
    assert report["valid"] and report["accepted"][0]["trust"] == "self_supplied"


def test_foreign_subject_is_refused(case39):
    record = {**case39["imports"][0]["record"], "subject_ref": "maya-001"}
    [issue] = validate_case_contract(with_imports(case39, record))["issues"]
    assert issue["code"] == "foreign_subject" and issue["fields"] == ["subject_ref"]


def test_bad_entry_time_is_typed_before_native_import(case39):
    case = with_imports(case39, case39["imports"][0]["record"])
    case["imports"][0]["received_at"] = "yesterday"
    [issue] = validate_case_contract(case)["issues"]
    assert issue["stage"] == "entry" and issue["code"] == "received_at_invalid"


def test_raw_inputs_are_unchanged(case39):
    record = {**case39["imports"][0]["record"], "kind": "invoice", "signature": "x"}
    case = with_imports(case39, case39["imports"][0]["record"], record,
                        {"kind": "receipt", "amount_pence": 1, "currency": "GBP",
                         "occurred_at": "2026-07-10T13:20:00Z"})
    before = copy.deepcopy(case)
    validate_case_contract(case)
    assert case == before


def sentinel_case(case39):
    base = case39["imports"][0]["record"]
    return with_imports(
        case39,
        {**base, "kind": SENTINEL, "event_id": f"evt-{SENTINEL}"},
        {**{k: v for k, v in base.items() if k != "amount_minor"}, "kind": SENTINEL,
         f"x_{SENTINEL}": SENTINEL, "text": SENTINEL},
        {**base, "subject_ref": f"seller-{SENTINEL}", "item": SENTINEL},
        {**base, "amount_minor": SENTINEL},
    )


def test_sentinel_values_and_keys_never_reach_the_report(case39):
    report = validate_case_contract(sentinel_case(case39))
    assert len(report["issues"]) == 4 and not report["valid"]
    assert SENTINEL not in json.dumps(report)


def test_cli_prints_codes_without_sentinel_and_fails(case39, monkeypatch, capsys):
    monkeypatch.setattr(data_api, "load_case", lambda n: sentinel_case(case39))
    assert main(["--case", "39"]) == 1
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err
    summary = json.loads(out.out.strip().splitlines()[-1])
    assert summary["issue_codes"] and summary["failing_cases"][0]["case_number"] == 39


def test_cli_rejects_non_public_case_numbers():
    with pytest.raises(SystemExit):
        main(["--case", "81"])
    with pytest.raises(ValueError):
        input_contract.validate_public_cases([81])


def test_default_cli_checks_public_cases_with_candidate_sha(capsys):
    code = main([])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["cases_checked"] + len(summary["cases_not_visible"]) == 80
    sha = summary["candidate"]["git_sha"]
    assert sha is None or len(sha) == 40
    assert code == (0 if summary["valid"] else 1)
    print("PUBLIC_SUMMARY", json.dumps({k: summary[k] for k in (
        "cases_checked", "cases_not_visible", "imports_checked", "imports_accepted",
        "issue_codes", "valid", "candidate")}))
