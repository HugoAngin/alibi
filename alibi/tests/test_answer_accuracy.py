"""Answer accuracy: a holder's stated breakdown is an untrusted claim checked against issuer
records; a reference-named payout of a different amount leaves the source unresolved; a period
total spanning several issuers states each issuer's share. Built on mutated copies of the public
control case, so no expectation depends on a case number's gold."""
from __future__ import annotations

from types import SimpleNamespace

from agents.data import api as data_api
from alibi.adapter import run_case
from alibi.core import facts as core_facts
from alibi.core import investigate_rules as rules
from alibi.core.schemas import RecordKind

RFC_A, RFC_B = "rfc-0001-01", "rfc-0001-02"
TXN = "txn-0001-004"


def _control() -> dict:
    return data_api.without_gold(data_api.load_case(1))


def _claims(predictions, request_id):
    return next(p for p in predictions if p["request_id"] == request_id)["claims"]


def _true_breakdown(tmp_path) -> dict:
    case = _control()
    case["holder_claim"] = None
    claims = _claims(run_case(case, "rules", root=tmp_path / "truth"), RFC_A)
    assert claims["q.breakdown"]["status"] == "supported"
    return claims["q.breakdown"]["facts"]["breakdown_minor"]


def test_stated_breakdown_matching_the_records_stays_supported(tmp_path):
    truth = _true_breakdown(tmp_path)
    case = _control()
    case["holder_claim"] = {"txn_id": TXN, "explanation": "marketplace_payout",
                            "payout_reference": None, "stated_minor": dict(truth)}
    claims = _claims(run_case(case, "rules", root=tmp_path / "run"), RFC_A)
    assert claims["q.breakdown"]["status"] == "supported"
    assert claims["q.source_of_funds"]["status"] == "supported"


def test_stated_breakdown_the_records_contradict_is_contradicted_with_evidence(tmp_path):
    truth = _true_breakdown(tmp_path)
    supported = _claims(run_case(_control() | {"holder_claim": None}, "rules",
                                 root=tmp_path / "base"), RFC_A)["q.breakdown"]
    # Gross sales stated as the whole net amount, with refunds and fees left out.
    stated = {"sales": truth["net"], "refunds": 0, "fees": 0, "net": truth["net"]}
    assert stated != truth
    case = _control()
    case["holder_claim"] = {"txn_id": TXN, "explanation": "marketplace_payout",
                            "payout_reference": None, "stated_minor": stated}
    claims = _claims(run_case(case, "rules", root=tmp_path / "run"), RFC_A)
    breakdown = claims["q.breakdown"]
    assert breakdown["status"] == "contradicted"
    assert breakdown["gap"] is None
    # The issuer records that contradict the claim travel with it, and nothing new is cited:
    # the holder's figures are never evidence.
    assert sorted(breakdown["evidence_refs"]) == sorted(supported["evidence_refs"])
    assert all(ref.startswith(("bank:", "merchant:", "import:")) for ref in breakdown["evidence_refs"])
    # The source of funds does not depend on the holder's arithmetic.
    assert claims["q.source_of_funds"]["status"] == "supported"


def test_stated_breakdown_for_another_credit_or_malformed_changes_nothing(tmp_path):
    truth = _true_breakdown(tmp_path)
    wrong = {"sales": truth["net"], "refunds": 0, "fees": 0, "net": truth["net"]}
    for label, claim in [("other", {"txn_id": "txn-other", "stated_minor": wrong}),
                         ("floats", {"txn_id": TXN, "stated_minor": {k: float(v) for k, v in
                                                                     wrong.items()}}),
                         ("partial", {"txn_id": TXN, "stated_minor": {"sales": 1}}),
                         ("absent", {"txn_id": TXN})]:
        case = _control()
        case["holder_claim"] = claim
        claims = _claims(run_case(case, "rules", root=tmp_path / label), RFC_A)
        assert claims["q.breakdown"]["status"] == "supported", label


def test_stated_breakdowns_parser_is_conservative():
    assert rules._stated_breakdowns(None) == {}
    assert rules._stated_breakdowns("the money is from my shop") == {}
    assert rules._stated_breakdowns({"txn_id": "t", "stated_minor": {"sales": True, "refunds": 0,
                                                                     "fees": 0, "net": 1}}) == {}
    assert rules._stated_breakdowns({"txn_id": "t", "stated_minor": {
        "sales": 3, "refunds": 1, "fees": 1, "net": 1, "extra": 9}}) == {
        "t": {"sales": 3, "refunds": 1, "fees": 1, "net": 1}}


def test_reference_named_payout_of_another_amount_is_unresolved_amount_mismatch(tmp_path):
    case = _control()
    changed = 0
    for delivery in case["bank_deliveries"]:
        if delivery["row"]["txn_id"] == TXN:
            delivery["row"]["amount_minor"] += 7500
            changed += 1
    assert changed
    claims = _claims(run_case(case, "rules", root=tmp_path / "run"), RFC_A)
    source = claims["q.source_of_funds"]
    assert source["status"] == "unresolved"
    assert source["gap"] == "amount_mismatch"
    assert claims["q.breakdown"]["status"] == "unresolved"


def test_single_issuer_period_total_has_no_issuer_split(tmp_path):
    claims = _claims(run_case(_control(), "rules", root=tmp_path / "run"), RFC_B)
    (answer,) = claims.values()
    assert answer["status"] == "supported"
    assert "by_issuer" not in answer["facts"]


def _term(ref_id, kind, amount):
    return SimpleNamespace(ref=SimpleNamespace(ref_id=ref_id, revision=1), kind=kind.value,
                           amount_pence=amount, sign=1)


class _Store:
    def __init__(self, issuers):
        self.issuers = issuers

    def get_source(self, record_id, revision=None):
        if record_id not in self.issuers:
            raise KeyError(record_id)
        return SimpleNamespace(issuer_id=self.issuers[record_id])


def test_issuer_shares_split_every_kind_including_empty_ones():
    found = SimpleNamespace(kinds=(RecordKind.ORDER, RecordKind.REFUND))
    calc = SimpleNamespace(terms=[_term("a", RecordKind.ORDER, 100), _term("b", RecordKind.REFUND, 5),
                                  _term("c", RecordKind.ORDER, 40), _term("d", RecordKind.ORDER, 2)])
    store = _Store({"a": "iss-1", "b": "iss-1", "c": "iss-2", "d": "iss-2"})
    assert core_facts._issuer_shares(found, calc, store) == {
        "iss-1": {"sales_minor": 100, "sale_count": 1, "refunds_minor": 5, "refund_count": 1},
        "iss-2": {"sales_minor": 42, "sale_count": 2, "refunds_minor": 0, "refund_count": 0}}


def test_issuer_shares_never_states_a_partial_split():
    found = SimpleNamespace(kinds=(RecordKind.ORDER,))
    calc = SimpleNamespace(terms=[_term("a", RecordKind.ORDER, 1), _term("b", RecordKind.ORDER, 2)])
    assert core_facts._issuer_shares(found, calc, _Store({"a": "x", "b": "x"})) is None
    assert core_facts._issuer_shares(found, calc, _Store({"a": "x"})) is None
    assert core_facts._issuer_shares(found, calc, None) is None


def _one_pass(case, root):
    from alibi.llm.fake import FakeClient
    client = FakeClient({"*": {"claims": [], "disclosures": [], "rationale": "No proposal."}})
    return run_case(case, "one_pass", root=root, client=client)


def test_one_pass_checks_the_same_untrusted_stated_breakdown(tmp_path):
    base = _claims(_one_pass(_control() | {"holder_claim": None}, tmp_path / "base"), RFC_A)
    if base["q.breakdown"]["status"] != "supported":
        # One-pass does not establish this credit offline; there is nothing to contradict.
        assert base["q.breakdown"]["status"] == "unresolved"
        return
    truth = base["q.breakdown"]["facts"]["breakdown_minor"]
    wrong = {"sales": truth["net"], "refunds": 0, "fees": 0, "net": truth["net"]}
    case = _control()
    case["holder_claim"] = {"txn_id": TXN, "stated_minor": wrong}
    claims = _claims(_one_pass(case, tmp_path / "run"), RFC_A)
    assert claims["q.breakdown"]["status"] == "contradicted"
    assert claims["q.breakdown"]["facts"] == {"breakdown_minor": truth}
    assert sorted(claims["q.breakdown"]["evidence_refs"]) == sorted(base["q.breakdown"]["evidence_refs"])
    case["holder_claim"] = {"txn_id": TXN, "stated_minor": dict(truth)}
    assert _claims(_one_pass(case, tmp_path / "same"), RFC_A)["q.breakdown"]["status"] == "supported"
