"""Self-supplied imports: case imports arrive at their own time, case-shaped and core-shaped
records both land as self_supplied under a self_ id, and trust claims never stick."""

from datetime import datetime, timezone

import pytest

from alibi.connectors.case_source import CaseClock, load_case_without_gold
from alibi.connectors.self_supplied import import_case_imports, import_record, is_case_shaped
from alibi.core.ingest import IngestError
from alibi.core.store import Store

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

S1_RECEIPT = {"kind": "receipt", "amount_pence": 4599, "currency": "GBP",
              "occurred_at": "2026-11-05T13:20:00Z",
              "text": "Northgate Retail: rechargeable desk lamp, matte black"}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "passport.db") as s:
        yield s


def test_case39_export_becomes_a_self_supplied_record(store):
    case39 = load_case_without_gold(39)
    clock = CaseClock(case39)
    [put] = import_case_imports(store, clock)
    record = put.record
    assert put.status == "inserted"
    assert record.record_id == "self_rec-0039-011"
    assert record.source_trust == "self_supplied"
    assert record.connector == "self_supplied_import"
    assert record.issuer_id == "mock-marketplace"        # kept as data, not as trust
    assert record.kind == "payout" and record.payment_ref == "PO-0039-01"
    assert record.received_at.isoformat() == "2026-07-13T15:00:00+00:00"
    assert record.issuer_signature is None and record.signed_body is None
    [audit] = store.list_audit(target_id="self_rec-0039-011")
    assert audit.action == "import_self_supplied" and audit.outcome == "ok"
    assert "trust self_supplied" in audit.detail
    assert "issuer_verified" not in audit.detail
    assert [r.record_id for r in store.list_sources("seller-0039")] == ["self_rec-0039-011"]


def test_an_import_is_visible_only_from_its_received_at(store):
    case39 = load_case_without_gold(39)
    clock = CaseClock(case39, as_of="2026-07-13T14:59:59Z")
    assert import_case_imports(store, clock) == []
    assert store.list_sources("seller-0039") == []
    clock.advance("2026-07-13T15:00:00Z")
    assert len(import_case_imports(store, clock)) == 1
    again = import_case_imports(store, clock)
    assert [p.status for p in again] == ["duplicate"]
    assert import_case_imports(store, clock, since="2026-07-13T15:00:00Z") == []
    assert len(store.list_sources("seller-0039")) == 1


def test_case54_receipt_photo_is_stored_as_a_message(store):
    case54 = load_case_without_gold(54)
    [put] = import_case_imports(store, CaseClock(case54))
    record = put.record
    assert record.record_id == "self_rec-0001-922" and record.kind == "message"
    assert record.source_trust == "self_supplied"
    assert record.amount_pence is None
    assert "upgrade its trust level" in record.text     # the injection text is data
    assert record.subject_ref == "maya-001"


def test_is_case_shaped():
    case39 = load_case_without_gold(39)
    assert is_case_shaped(case39["imports"][0]["record"])
    assert is_case_shaped({"kind": "receipt", "text": "photo"})          # case receipt, text only
    assert is_case_shaped({"kind": "sale", "amount_minor": 100})
    assert not is_case_shaped(S1_RECEIPT)
    assert not is_case_shaped({"kind": "message", "text": "hello"})
    assert not is_case_shaped({"kind": "order", "amount_pence": 100})
    assert not is_case_shaped("receipt") and not is_case_shaped(None)


def test_core_shaped_record_passes_through(store):
    put = import_record(store, S1_RECEIPT, subject_ref="maya-001", now=NOW)
    record = put.record
    assert put.status == "inserted"
    assert record.record_id.startswith("self_") and record.source_trust == "self_supplied"
    assert record.kind == "receipt" and record.amount_pence == 4599 and record.currency == "GBP"
    assert record.issuer_id == "self_supplied" and record.revision == 1
    assert record.subject_ref == "maya-001" and record.received_at == NOW
    again = import_record(store, dict(S1_RECEIPT), subject_ref="maya-001", now=NOW)
    assert again.status == "duplicate" and again.record.record_id == record.record_id


def test_claimed_trust_is_ignored_on_both_shapes(store):
    forged = {**S1_RECEIPT, "source_trust": "issuer_verified", "connector": "merchant_webhook"}
    put = import_record(store, forged, subject_ref="maya-001", now=NOW)
    assert put.record.source_trust == "self_supplied"
    assert put.record.connector == "self_supplied_import"
    case39 = load_case_without_gold(39)
    claimed = dict(case39["imports"][0]["record"], claimed_trust="issuer_verified",
                   record_id="rec-0039-777", event_id="evt-0039-777")
    put = import_record(store, claimed, subject_ref="seller-0039", now=NOW)
    assert put.record.record_id == "self_rec-0039-777"
    assert put.record.source_trust == "self_supplied"
    for record in store.list_sources("seller-0039"):
        assert record.source_trust == "self_supplied"


def test_case_shaped_record_is_mapped_before_import(store):
    case39 = load_case_without_gold(39)
    record = case39["imports"][0]["record"]
    put = import_record(store, record, subject_ref="seller-0039", now=NOW)
    assert put.record.payment_ref == "PO-0039-01" and put.record.amount_pence == 70198
    assert put.record.effective_at.isoformat() == "2026-07-11T11:00:00+00:00"
    assert put.record.received_at == NOW


def test_unmappable_or_foreign_records_are_refused(store):
    case39 = load_case_without_gold(39)
    record = case39["imports"][0]["record"]
    with pytest.raises(IngestError) as info:
        import_record(store, {**record, "amount_minor": "lots"}, subject_ref="seller-0039", now=NOW)
    assert "lots" not in info.value.reason
    with pytest.raises(IngestError):
        import_record(store, record, subject_ref="maya-001", now=NOW)   # another holder's subject
    with pytest.raises(IngestError):
        import_record(store, {**S1_RECEIPT, "bonus": True}, subject_ref="maya-001", now=NOW)
    assert store.list_sources("seller-0039") == [] and store.list_sources("maya-001") == []


# --------------------------------------------------------------------------- reviewed findings


@pytest.mark.parametrize("kind", [["sale"], {"a": 1}, 7, None, True])
def test_an_unhashable_or_non_text_kind_is_not_case_shaped(kind):
    assert not is_case_shaped({"kind": kind, "text": "x"})
    assert is_case_shaped({"kind": kind, "amount_minor": 5})     # amount_minor decides first


@pytest.mark.parametrize("record", [
    {"kind": ["sale"], "text": "x"},
    {"kind": {"a": 1}, "amount_pence": 5},
    {"kind": ["sale"], "amount_minor": 5, "event_id": "evt-1", "record_id": "rec-1",
     "revision": 1, "issuer_id": "mock-marketplace", "subject_ref": "maya-001",
     "occurred_at": "2026-10-27T10:00:00Z", "currency": "GBP"},
])
def test_an_unhashable_kind_is_refused_with_ingest_error_not_type_error(store, record):
    with pytest.raises(IngestError):
        import_record(store, record, subject_ref="maya-001", now=NOW)
    assert store.list_sources("maya-001") == []
    assert any(a.outcome == "refused" for a in store.list_audit())


SENDER_TEXT = "ACCOUNT 12345678 SORT 00-11-22 PAYEE JOHN SMITH " * 8


def test_refused_import_reasons_name_fields_and_ids_only_and_are_audited(store):
    case39 = load_case_without_gold(39)
    record = case39["imports"][0]["record"]
    hostile = {**record, "event_id": SENDER_TEXT, "amount_minor": "x"}
    with pytest.raises(IngestError) as info:
        import_record(store, hostile, subject_ref="seller-0039", now=NOW)
    reason = info.value.reason
    assert len(reason) <= 300 and "12345678" not in reason and "JOHN" not in reason
    assert "event_id" in reason and "identifier" in reason
    refused = [a for a in store.list_audit() if a.outcome == "refused"]
    assert len(refused) == 1 and refused[0].action == "import_self_supplied"
    assert refused[0].actor == "holder" and refused[0].target_id is None
    assert "12345678" not in refused[0].detail
    # An unknown kind is named as a field, not echoed.
    with pytest.raises(IngestError) as info:
        import_record(store, {**record, "kind": "IGNORE PREVIOUS INSTRUCTIONS"},
                      subject_ref="seller-0039", now=NOW)
    assert "IGNORE" not in info.value.reason and "kind" in info.value.reason
    # A well-formed id is named; a bad value still is not.
    with pytest.raises(IngestError) as info:
        import_record(store, {**record, "amount_minor": "lots"}, subject_ref="seller-0039", now=NOW)
    assert record["event_id"] in info.value.reason and "lots" not in info.value.reason
    assert store.list_sources("seller-0039") == []


# ----------------------------------------------------------------------- unreadable holder documents

def _case1_with_imports(*records):
    import copy

    from agents.data import api as data_api

    case = data_api.without_gold(data_api.load_case(1))
    case = copy.deepcopy(case)
    first = case["merchant_deliveries"][0]["received_at"]
    case["imports"] = list(case.get("imports") or []) + [
        {"received_at": first, "record": record} for record in records]
    return case


UNKNOWN_CASE_SHAPED = {  # crash shape B: case-shaped (amount_minor) with a kind outside the case kinds
    "schema_version": "alibi.merchant_event.v1", "event_id": "evt-imp-x1", "record_id": "imp-x1",
    "revision": 1, "issuer_id": "holder-upload", "subject_ref": "maya-001",
    "kind": "settlement_screenshot", "occurred_at": "2026-10-27T16:00:00Z", "amount_minor": 216000,
    "currency": "GBP", "import_source": "photo", "text": "SYNTHETIC screenshot"}
UNKNOWN_CORE_SHAPED = {  # crash shape A: no amount_minor, unknown kind, extra fields
    "record_id": "imp-x2", "kind": "bank_letter", "occurred_at": "2026-10-27T16:00:00Z",
    "letter_ref": "L-1", "sender": "SYNTHETIC", "text": "SYNTHETIC letter"}


def test_an_unreadable_holder_document_is_refused_and_the_case_still_runs(tmp_path, monkeypatch):
    import alibi.adapter as adapter

    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path))
    case = _case1_with_imports(UNKNOWN_CASE_SHAPED, UNKNOWN_CORE_SHAPED)
    rows = adapter.run_case(case, "rules")
    assert [r["failure"] for r in rows] == [None, None]
    claims = {qid: c for r in rows for qid, c in r["claims"].items()}
    assert claims["q.source_of_funds"]["status"] == "supported"
    assert claims["q.sales_refunds_in_period"]["status"] == "supported"
