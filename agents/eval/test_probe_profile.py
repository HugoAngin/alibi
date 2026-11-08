from agents.eval.probe_profile import _rfc_intake_plan
from alibi.connectors.case_source import load_case_without_gold


def test_rfc_intake_profile_separates_subject_scope_and_issuer_attacks():
    case = load_case_without_gold(1)
    plan = _rfc_intake_plan(case)
    assert all(entry["routes"] == {"rfc_intake": "/rfc"} for entry in plan.values())

    foreign_subject = plan["SP-22"]["fixtures"]["rfc_foreign_subject"]["request"]
    assert foreign_subject["subject_ref"] != case["subject_ref"]
    assert "subject_mismatch" in plan["SP-22"]["fixtures"]["sp22_codes"]

    foreign_scope = plan["SP-23"]["fixtures"]["rfc_out_of_scope_txns"]["request"]
    own_txns = {delivery["row"].get("txn_id") for delivery in case["bank_deliveries"]}
    assert foreign_scope["subject_ref"] == case["subject_ref"]
    assert foreign_scope["txn_ids"] and own_txns.isdisjoint(foreign_scope["txn_ids"])

    issuer = plan["SP-24"]["fixtures"]
    attack = issuer["rfc_claims_bank_a"]["request"]
    control = issuer["rfc_claims_bank_a_control"]["request"]
    assert attack["issuer"] == "bank_a" and control["issuer"] == "bank_b"
    assert attack["audience"] == control["audience"] == "bank_b"
    assert {key for key in attack if attack[key] != control[key]} == {"issuer", "request_id"}
    assert _rfc_intake_plan(case)["SP-24"]["fixtures"]["rfc_claims_bank_a"] != issuer["rfc_claims_bank_a"]
