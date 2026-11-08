"""Maya fixtures: schema validity, determinism, canonical numbers, variants and signing."""

import copy
import json

import pytest

from alibi.core.canon import digest
from alibi.core.schemas import (
    BankTxn,
    EvidenceRecord,
    MerchantEvent,
    Period,
    RequestForContext,
    SigningDomain,
    SourceTrust,
    parse_utc,
)
from alibi.core.signing import SignatureError, verify
from alibi.fixtures import maya

ALL = maya.VARIANTS + (maya.RETAIL,)
FILE_NAMES = ["maya_supported.json", "maya_contradiction.json", "maya_pending_correction.json",
              "maya_ambiguous.json", "retail_purchase.json"]
EVENT_SECTIONS = ("merchant_events", "lookup_events", "corrections", "self_supplied")
SIGNED_SECTIONS = ("merchant_events", "lookup_events", "corrections")
TOP_KEYS = {"fixture", "variant", "schema", "subject_ref", "account_ref", "issuer_id",
            "merchant_kid", "merchant_events", "lookup_events", "corrections", "bank_txns",
            "self_supplied", "rfcs", "expected", "notes"}
OCTOBER = Period.model_validate({"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
SETTLED = {"settled", "paid", "completed", "succeeded"}
NOT_SUPPORTED = {"contradicted", "unresolved"}


@pytest.fixture(scope="module")
def committed():
    return {name: maya.load_variant(name) for name in ALL}


def events_of(fx, *sections, kind=None):
    return [e for section in (sections or EVENT_SECTIONS) for e in fx[section]
            if kind is None or e["kind"] == kind]


def one(items, **match):
    found = [item for item in items if all(item.get(k) == v for k, v in match.items())]
    assert len(found) == 1, (match, found)
    return found[0]


def all_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from all_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from all_keys(inner)


# --------------------------------------------------------------------------- files and schemas


def test_committed_files_exist_under_their_names():
    assert [maya.fixture_path(name).name for name in ALL] == FILE_NAMES
    for name in ALL:
        assert maya.fixture_path(name).is_file()
    with pytest.raises(ValueError):
        maya.load_variant("nope")
    with pytest.raises(ValueError):
        maya.build_fixture("nope")


@pytest.mark.parametrize("name", ALL)
def test_every_object_validates_against_its_schema(committed, name):
    fx = committed[name]
    assert set(fx) == TOP_KEYS
    assert fx["schema"] == "alibi.fixture.v1" and fx["variant"] == name
    assert fx["subject_ref"] == "subj_maya" and fx["account_ref"] == "acc_maya_bank_a"
    event_ids, revisions = set(), set()
    for raw in events_of(fx):
        event = MerchantEvent.model_validate(raw)
        assert event.to_json_dict() == raw  # committed bodies are already normalised
        assert raw["event_id"] == f"evt_{raw['record_id']}_r{raw['revision']}"
        assert event.subject_ref == "subj_maya"
        assert raw["event_id"] not in event_ids and (raw["record_id"], raw["revision"]) not in revisions
        event_ids.add(raw["event_id"])
        revisions.add((raw["record_id"], raw["revision"]))
        if event.kind == "order":
            assert event.items and all(i.description and i.category for i in event.items)
    for raw in events_of(fx, *SIGNED_SECTIONS):
        assert raw["issuer_id"] == fx["issuer_id"] == "threadline"
    txn_ids = set()
    for raw in fx["bank_txns"]:
        txn = BankTxn.model_validate(raw)
        assert txn.to_json_dict() == raw
        assert txn.subject_ref == "subj_maya" and txn.account_ref == "acc_maya_bank_a"
        assert txn.provenance.connector == "bank_feed"
        assert txn.provenance.connection_id == "conn_bank_a_maya"
        txn_ids.add(txn.txn_id)
    for raw in fx["rfcs"]:
        rfc = RequestForContext.model_validate(raw)
        assert rfc.to_json_dict() == raw
        assert rfc.audience == rfc.issuer and rfc.subject_ref == "subj_maya"
        assert set(rfc.txn_ids or []) <= txn_ids
    # Unsigned bodies only: no signature envelopes or key material are committed.
    keys = set(all_keys(fx))
    assert not keys & {"signature", "sig", "issuer_signature", "private_key"}
    assert "PRIVATE KEY" not in maya.fixture_path(name).read_text(encoding="utf-8")


def test_regenerating_reproduces_the_committed_files(tmp_path, committed):
    paths = maya.write_all(tmp_path)
    assert [p.name for p in paths] == FILE_NAMES
    for name in ALL:
        assert maya.load_variant(name, tmp_path) == committed[name]
        assert maya.build_fixture(name) == committed[name]


def test_generation_is_byte_deterministic(tmp_path):
    first, second = maya.write_all(tmp_path / "a"), maya.write_all(tmp_path / "b")
    for a, b in zip(first, second):
        data = a.read_bytes()
        assert data == b.read_bytes()
        assert data.endswith(b"}\n") and b"\r" not in data
        assert data.decode("utf-8") == maya.dumps(json.loads(data.decode("utf-8")))


# --------------------------------------------------------------------------- canonical ledger


@pytest.mark.parametrize("name", maya.VARIANTS)
def test_canonical_totals(committed, name):
    fx = committed[name]
    events = fx["merchant_events"]
    orders = [e for e in events if e["kind"] == "order"]
    assert [o["record_id"] for o in orders] == ["ord_1001", "ord_1002", "ord_1003", "ord_1004"]
    assert [o["amount_pence"] for o in orders] == [60000, 72000, 48000, 60000]
    for order in orders:
        assert order["status"] == "completed" and order["currency"] == "GBP"
        assert sum(i["quantity"] * i["unit_amount_pence"] for i in order["items"]) \
            == order["amount_pence"]
    refund, fee = one(events, record_id="rfd_2001"), one(events, record_id="fee_3001")
    credit = one(fx["bank_txns"], txn_id="txn_9001")
    sales = sum(o["amount_pence"] for o in orders)
    assert sales == 240000
    assert refund["kind"] == "refund" and refund["amount_pence"] == 18000
    assert refund["related_record_ids"] == ["ord_1003"] and refund["status"] == "completed"
    assert fee["kind"] == "fee" and fee["amount_pence"] == 6000 and fee["status"] == "completed"
    assert credit["amount_pence"] == 216000 and credit["currency"] == "GBP"
    assert credit["booked_at"] == "2026-11-03T10:00:00Z"
    assert 240000 - 18000 - 6000 == 216000
    assert sales - refund["amount_pence"] - fee["amount_pence"] == credit["amount_pence"]

    payout = one(events, record_id="po_5001")
    assert (payout["kind"], payout["payment_ref"], payout["status"], payout["amount_pence"]) \
        == ("payout", "PO-5001", "initiated", 216000)
    assert payout["related_record_ids"] == ["ord_1001", "ord_1002", "ord_1003", "ord_1004",
                                            "rfd_2001", "fee_3001"]
    message = one(events, record_id="msg_7001")
    assert message["kind"] == "message"
    assert (message["amount_pence"], message["currency"]) == (216000, "GBP")
    assert "released to your seller balance" in message["text"]
    assert message["status"] not in SETTLED  # a balance release is not a bank settlement

    rec = fx["expected"]["reconciliation"]
    assert (rec["sales_pence"], rec["refunds_pence"], rec["fees_pence"], rec["net_pence"],
            rec["payout_pence"], rec["credit_pence"]) == (240000, 18000, 6000, 216000, 216000,
                                                          216000)
    assert rec["difference_pence"] == 0 and rec["balanced"] is True
    assert rec["components"] == payout["related_record_ids"]


@pytest.mark.parametrize("name", maya.VARIANTS)
def test_timeline_and_requests(committed, name):
    fx = committed[name]
    events = fx["merchant_events"]
    for event in events:
        if event["kind"] in ("order", "refund", "fee"):
            assert OCTOBER.contains(parse_utc(event["occurred_at"]))
    assert one(events, record_id="msg_7001")["occurred_at"].startswith("2026-11-01")
    assert one(events, record_id="po_5001")["occurred_at"].startswith("2026-11-02")
    assert not events_of(fx, "merchant_events", kind="settlement")  # only reachable by lookup

    rfc_a, rfc_b = fx["rfcs"]
    assert (rfc_a["request_id"], rfc_a["issuer"], rfc_a["txn_ids"]) \
        == ("rfc_a_001", "bank_a", ["txn_9001"])
    assert (rfc_a["issued_at"], rfc_a["expires_at"]) \
        == ("2026-11-04T09:00:00Z", "2026-11-11T09:00:00Z")
    assert (rfc_b["request_id"], rfc_b["issuer"]) == ("rfc_b_001", "bank_b")
    assert rfc_b["period"] == {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"}
    assert (rfc_b["issued_at"], rfc_b["expires_at"]) \
        == ("2026-11-05T09:00:00Z", "2026-11-12T09:00:00Z")
    q1, q2 = rfc_b["questions"]
    assert (q1["question_id"], q2["question_id"]) == ("q1", "q2")
    assert "sales" in q1["text"] and "refunds" in q2["text"]
    # Webhook events and lookups exist before any request is issued.
    first_rfc = min(parse_utc(r["issued_at"]) for r in fx["rfcs"])
    assert all(parse_utc(e["occurred_at"]) < first_rfc
               for e in events_of(fx, "merchant_events", "lookup_events"))

    gold = fx["expected"]["claims"]["rfc_b_001"]
    assert gold["q1"]["statuses"] == ["supported"] and gold["q1"]["total_pence"] == 240000
    assert gold["q2"]["statuses"] == ["supported"] and gold["q2"]["total_pence"] == 18000
    assert gold["q1"]["evidence"] == [f"record:ord_100{i}@1" for i in range(1, 5)]
    assert gold["q2"]["evidence"] == ["record:rfd_2001@1"]


# --------------------------------------------------------------------------- variants


def test_supported_settlement_is_settled_before_booking(committed):
    fx = committed["supported"]
    stl = one(fx["lookup_events"], record_id="stl_88001")
    credit = one(fx["bank_txns"], txn_id="txn_9001")
    assert stl["status"] == "settled" and stl["effective_at"] == "2026-11-03T07:45:00Z"
    assert parse_utc(stl["effective_at"]) < parse_utc(credit["booked_at"])
    assert parse_utc(stl["occurred_at"]) < parse_utc(credit["booked_at"])
    assert (stl["payment_ref"], stl["settlement_ref"]) == ("PO-5001", "STL-88001")
    assert stl["settlement_ref"] in credit["refs"] and stl["payment_ref"] in credit["refs"]
    assert fx["corrections"] == []
    gold = fx["expected"]
    assert gold["links"]["txn_9001"] == {"link_state": "linked", "record_id": "po_5001",
                                         "candidates": ["po_5001"], "method": "reference"}
    assert gold["claims"]["rfc_a_001"]["q1"]["statuses"] == ["supported"]
    assert [t["ok"] for t in gold["settlement_timing"]] == [True]


def test_contradiction_settlement_is_pending_after_booking(committed):
    fx = committed["contradiction"]
    stl = one(fx["lookup_events"], record_id="stl_88001")
    credit = one(fx["bank_txns"], txn_id="txn_9001")
    assert stl["status"] == "pending" and "effective_at" not in stl
    assert stl["occurred_at"] == "2026-11-03T12:00:00Z"
    assert parse_utc(stl["occurred_at"]) > parse_utc(credit["booked_at"])
    assert credit["refs"] == []
    assert "PO-5001" not in credit["description"] and "STL-88001" not in credit["description"]
    gold = fx["expected"]
    link = gold["links"]["txn_9001"]
    assert (link["link_state"], link["record_id"], link["method"]) \
        == ("linked", "po_5001", "amount_date")
    statuses = gold["claims"]["rfc_a_001"]["q1"]["statuses"]
    assert statuses and set(statuses) <= NOT_SUPPORTED  # never supported
    assert [t["ok"] for t in gold["settlement_timing"]] == [False]


def test_ambiguous_has_two_payouts_before_booking_and_a_bare_credit(committed):
    fx = committed["ambiguous"]
    credit = one(fx["bank_txns"], txn_id="txn_9001")
    payouts = events_of(fx, "merchant_events", kind="payout")
    assert sorted(p["record_id"] for p in payouts) == ["po_5001", "po_5002"]
    assert {(p["amount_pence"], p["currency"]) for p in payouts} == {(216000, "GBP")}
    assert all(parse_utc(p["occurred_at"]) < parse_utc(credit["booked_at"]) for p in payouts)
    assert one(payouts, record_id="po_5002")["payment_ref"] == "PO-5002"
    assert credit["refs"] == []
    assert not any(p["payment_ref"] in credit["description"] for p in payouts)
    gold = fx["expected"]
    link = gold["links"]["txn_9001"]
    assert link["link_state"] == "ambiguous" and link["record_id"] is None
    assert sorted(link["candidates"]) == ["po_5001", "po_5002"]
    assert gold["claims"]["rfc_a_001"]["q1"]["statuses"] == ["unresolved"]
    # Both payouts' settlements were effective before the booking: timing cannot pick one.
    timing = gold["settlement_timing"]
    assert sorted(t["payment_ref"] for t in timing) == ["PO-5001", "PO-5002"]
    assert all(t["ok"] for t in timing)


def test_pending_correction_restates_the_settlement(committed):
    fx, base = committed["pending_correction"], committed["supported"]
    for section in ("merchant_events", "lookup_events", "bank_txns", "self_supplied", "rfcs"):
        assert fx[section] == base[section]
    (fix,) = fx["corrections"]
    original = one(fx["lookup_events"], record_id="stl_88001")
    assert (original["revision"], original["status"]) == (1, "settled")
    assert (fix["record_id"], fix["revision"], fix["corrects_revision"]) == ("stl_88001", 2, 1)
    assert fix["event_id"] == "evt_stl_88001_r2" and fix["kind"] == "settlement"
    assert fix["status"] == "pending" and fix["occurred_at"] == "2026-11-04T09:00:00Z"
    assert fix["correction_reason"] and "effective_at" not in fix
    for field in ("payment_ref", "settlement_ref", "amount_pence", "currency", "issuer_id"):
        assert fix[field] == original[field]
    q1 = fx["expected"]["claims"]["rfc_a_001"]["q1"]
    assert q1["statuses"] == ["supported"]
    after = q1["after_correction"]
    assert after["statuses"] and set(after["statuses"]) <= NOT_SUPPORTED
    assert after["earlier_claim_state"] == "invalidated"
    assert after["correction"] == "record:stl_88001@2"
    assert [(t["revision"], t["ok"]) for t in fx["expected"]["settlement_timing"]] \
        == [(1, True), (2, False)]
    assert all(committed[v]["corrections"] == [] for v in ALL if v != "pending_correction")


def test_retail_purchase_uses_the_same_record_model(committed):
    fx = committed["retail_purchase"]
    (receipt,) = fx["self_supplied"]
    (debit,) = fx["bank_txns"]
    assert receipt["kind"] == "receipt" and receipt["issuer_id"] == "northgate_retail"
    assert len(receipt["items"]) == 1
    assert (receipt["amount_pence"], receipt["currency"]) == (4599, "GBP")
    assert receipt["payment_ref"] and receipt["payment_ref"] in debit["refs"]
    assert (debit["txn_id"], debit["amount_pence"]) == ("txn_9002", -4599)
    assert fx["merchant_events"] == fx["lookup_events"] == fx["corrections"] == fx["rfcs"] == []
    gold = fx["expected"]
    assert gold["links"]["txn_9002"]["record_id"] == "self_rcpt_4001"
    assert gold["source_trust"] == {"record:self_rcpt_4001": "self_supplied",
                                    "txn:txn_9002": "bank_observed"}
    assert gold["reconciliation"]["balanced"] is True
    assert maya.build_retail_fixture() == fx


def test_retail_reconciliation_reproduces_with_core_reconcile(committed, now):
    """The retail gold matches alibi.core.reconcile when the receipt counts as an outflow."""
    from alibi.core.reconcile import compare_to_target, sum_terms

    fx = committed["retail_purchase"]
    (raw,) = fx["self_supplied"]
    receipt = EvidenceRecord.from_event(
        MerchantEvent.model_validate(raw), source_trust=SourceTrust.SELF_SUPPLIED,
        connector="self_supplied_import", received_at=now, payload_sha256=digest(raw))
    debit = BankTxn.model_validate(fx["bank_txns"][0])
    calc = compare_to_target(
        sum_terms([receipt], method="receipt_match", signs={"receipt": -1}), debit)
    gold = fx["expected"]["reconciliation"]
    assert gold["terms"] == [{"ref": t.ref.key(), "kind": t.kind, "sign": t.sign,
                              "amount_pence": t.amount_pence, "currency": t.currency}
                             for t in calc.terms]
    assert (gold["total_pence"], gold["target_ref"], gold["target_pence"],
            gold["difference_pence"], gold["balanced"]) \
        == (calc.total_pence, calc.target_ref.key(), calc.target_pence, calc.difference_pence,
            calc.balanced)
    assert (calc.total_pence, calc.target_pence, calc.difference_pence, calc.balanced) \
        == (-4599, -4599, 0, True)
    assert (gold["receipt_pence"], gold["debit_pence"]) == (4599, -4599)


# --------------------------------------------------------------------------- signing at load time


@pytest.mark.parametrize("name", maya.VARIANTS)
def test_signed_events_and_requests_verify(committed, keystore, name):
    fx = committed[name]
    registry = keystore.registry()
    for raw in events_of(fx, *SIGNED_SECTIONS):
        signed = maya.sign_event(raw, keystore)
        assert set(signed) == {"event", "signature"} and signed["event"] == raw
        entry = verify(signed["event"], signed["signature"], registry,
                       domain=SigningDomain.RECORD, issuer_id=raw["issuer_id"])
        assert (entry.kid, entry.issuer_id) == ("merchant_demo", "threadline")
    for raw in fx["rfcs"]:
        signed = maya.sign_request(raw, keystore)
        assert set(signed) == {"request", "signature"} and signed["request"] == raw
        assert signed["signature"]["kid"] == raw["issuer"]
        entry = verify(signed["request"], signed["signature"], registry,
                       domain=SigningDomain.REQUEST, issuer_id=raw["issuer"])
        assert entry.issuer_id == raw["issuer"]


def test_signatures_are_bound_to_domain_issuer_and_content(committed, keystore):
    fx = committed["supported"]
    registry = keystore.registry()
    signed_a = maya.sign_request(fx["rfcs"][0], keystore)
    with pytest.raises(SignatureError):
        verify(signed_a["request"], signed_a["signature"], registry,
               domain=SigningDomain.REQUEST, issuer_id="bank_b")
    with pytest.raises(SignatureError):
        verify(signed_a["request"], signed_a["signature"], registry, domain=SigningDomain.RECORD)
    signed = maya.sign_event(fx["merchant_events"][0], keystore)
    tampered = {**signed["event"], "amount_pence": signed["event"]["amount_pence"] + 1}
    with pytest.raises(SignatureError):
        verify(tampered, signed["signature"], registry, domain=SigningDomain.RECORD,
               issuer_id="threadline")
    with pytest.raises(SignatureError):
        verify(signed["event"], signed["signature"], registry, domain=SigningDomain.REQUEST)
    stranger = {**fx["rfcs"][0], "issuer": "bank_z", "audience": "bank_z"}
    with pytest.raises(SignatureError):
        maya.sign_request(stranger, keystore)
    # Signing copies: the loaded fixture is unchanged.
    signed["event"]["amount_pence"] = 1
    assert fx == maya.load_variant("supported")


def test_settlement_lookup_serves_the_latest_available_revision(committed, keystore):
    registry = keystore.registry()
    fx = committed["pending_correction"]
    before = maya.settlement_lookup(fx, keystore)
    after = maya.settlement_lookup(fx, keystore, include_corrections=True)
    first, latest = before("PO-5001"), after("PO-5001")
    assert (first["event"]["revision"], first["event"]["status"]) == (1, "settled")
    assert (latest["event"]["revision"], latest["event"]["status"]) == (2, "pending")
    for payload in (first, latest):
        verify(payload["event"], payload["signature"], registry, domain=SigningDomain.RECORD,
               issuer_id="threadline")
    assert before("PO-9999") is None and after("PO-9999") is None and before("") is None
    # Retries return identical signed payloads, as fresh copies.
    assert before("PO-5001") == first
    first["event"]["status"] = "tampered"
    assert before("PO-5001")["event"]["status"] == "settled"
    # The highest revision wins whatever the list order: a late, stale revision 1 listed
    # after revision 2 is never served.
    late = copy.deepcopy(fx)
    late["lookup_events"], late["corrections"] = late["corrections"] + late["lookup_events"], []
    served = maya.settlement_lookup(late, keystore)("PO-5001")["event"]
    assert (served["revision"], served["status"]) == (2, "pending")
    # ...and whatever the observation time: revision 2 still wins when observed before r1.
    early = copy.deepcopy(fx)
    early["corrections"][0]["occurred_at"] = "2026-11-03T07:00:00Z"
    served = maya.settlement_lookup(early, keystore, include_corrections=True)("PO-5001")["event"]
    assert (served["revision"], served["status"]) == (2, "pending")


def test_settlement_lookup_per_variant(committed, keystore):
    supported = maya.settlement_lookup(committed["supported"], keystore)
    assert supported("PO-5001")["event"]["status"] == "settled"
    assert supported("PO-5002") is None
    with_corrections = maya.settlement_lookup(committed["supported"], keystore,
                                              include_corrections=True)
    assert with_corrections("PO-5001") == supported("PO-5001")
    contradiction = maya.settlement_lookup(committed["contradiction"], keystore)
    assert contradiction("PO-5001")["event"]["status"] == "pending"
    ambiguous = maya.settlement_lookup(committed["ambiguous"], keystore)
    assert ambiguous("PO-5001")["event"]["record_id"] == "stl_88001"
    assert ambiguous("PO-5002")["event"]["record_id"] == "stl_88002"
    assert maya.settlement_lookup(committed["retail_purchase"], keystore)("NGR-731942") is None


def test_txn_refresh_serves_the_latest_revision(committed):
    refresh = maya.txn_refresh(committed["supported"])
    txn = refresh("txn_9001")
    assert txn == committed["supported"]["bank_txns"][0]
    txn["amount_pence"] = 1
    assert refresh("txn_9001")["amount_pence"] == 216000
    assert refresh("txn_0000") is None
    fx = copy.deepcopy(committed["supported"])
    fx["bank_txns"].append({**fx["bank_txns"][0], "revision": 2})
    assert maya.txn_refresh(fx)("txn_9001")["revision"] == 2
    fx["bank_txns"].reverse()  # revision 2 listed first: the choice is by revision, not order
    assert maya.txn_refresh(fx)("txn_9001")["revision"] == 2
    assert maya.txn_refresh(committed["retail_purchase"])("txn_9002")["amount_pence"] == -4599


# --------------------------------------------------------------------------- command line


def test_main_writes_files_and_prints_a_one_line_summary(tmp_path, capsys):
    assert maya.main(["--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    summary = json.loads(out)
    assert summary["written"] == 5 and sorted(summary["files"]) == sorted(FILE_NAMES)
    for name in ALL:
        assert summary["files"][maya.fixture_path(name).name] \
            == digest(maya.load_variant(name, tmp_path))
    assert summary["canonical"] == {"sales_pence": 240000, "refunds_pence": 18000,
                                    "fees_pence": 6000, "credit_pence": 216000, "balanced": True}

    assert maya.main(["--check", "--out", str(tmp_path)]) == 0
    assert maya.main(["--check"]) == 0  # the committed files are current
    stale = maya.fixture_path("contradiction", tmp_path)
    stale.write_text("{}\n", encoding="utf-8", newline="\n")
    capsys.readouterr()
    assert maya.main(["--check", "--out", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {"check": "stale",
                                                   "stale": ["maya_contradiction.json"]}


def test_package_reexports_lazily():
    import alibi.fixtures as package

    assert package.load_variant is maya.load_variant
    assert package.VARIANTS == maya.VARIANTS
    with pytest.raises(AttributeError):
        package.not_a_name  # noqa: B018
