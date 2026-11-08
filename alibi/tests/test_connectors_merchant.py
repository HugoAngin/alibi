"""The mock merchant: pushes every case delivery to the passport in arrival order, serves
lookups through the simulated clock, and signs everything with the connector key."""

import httpx
import pytest
from fastapi.testclient import TestClient

from agents.data.api import load_case
from alibi.connectors import keys, plug
from alibi.connectors.case_source import load_case_without_gold
from alibi.connectors.merchant import create_merchant_app, http_sink
from alibi.core import canon
from alibi.core.schemas import MerchantEvent
from alibi.core.signing import verify
from alibi.core.store import Store

RFC_A_TIME = "2026-11-02T09:00:00Z"
PO_VISIBLE = "2026-10-27T16:05:00Z"

# gold.ingest outcome -> the sink statuses the merchant may report for it. The core store
# answers ``duplicate`` (not ``stale``) when a lower revision is redelivered with content
# identical to the stored revision (case 23); ``stale`` is for a never-seen lower revision.
GOLD_TO_OUTCOME = {
    "accepted": {"inserted", "corrected"},
    "duplicate_ignored": {"duplicate"},
    "rejected_conflict": {"refused"},
    "stale_revision_ignored": {"stale", "duplicate"},
}


@pytest.fixture
def mkeys(tmp_path):
    return keys.connector_keystore(tmp_path / "keys", seed="merchant-tests")


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "passport.db") as s:
        yield s


def _client(mkeys, sink=None, **kwargs) -> TestClient:
    return TestClient(create_merchant_app(sink, keystore=mkeys, **kwargs))


def _seeded(mkeys, store, case_number, as_of=None):
    case = load_case_without_gold(case_number)
    sink = plug.webhook_sink(store, mkeys.registry(), subject_ref=case["subject_ref"])
    client = _client(mkeys, sink)
    params = {"case": case_number}
    if as_of is not None:
        params["as_of"] = as_of
    response = client.post("/admin/seed", params=params)
    assert response.status_code == 200, response.json()
    return client, response.json()


def _recording_sink(seen: list):
    def sink(payload, received_at):
        seen.append((payload, received_at))
        return {"status": "inserted"}
    return sink


# --------------------------------------------------------------------------- seeding


def test_identity_before_and_after_seed(mkeys):
    client = _client(mkeys)
    assert client.get("/").json() == {"service": "mock-marketplace", "issuer_id": "mock-marketplace",
                                      "kid": "merchant_mock", "case_number": None, "as_of": None}
    assert client.get("/records/rec-0001-001").status_code == 404
    assert client.get("/records/rec-0001-001").json()["code"] == "unknown_case"
    client.post("/admin/seed")
    body = client.get("/").json()
    assert body["case_number"] == 1 and body["as_of"] == RFC_A_TIME


def test_seed_case_1_pushes_twenty_deliveries_into_the_passport(mkeys, store):
    client, seeded = _seeded(mkeys, store, 1)
    assert seeded["case_number"] == 1 and seeded["subject_ref"] == "maya-001"
    assert seeded["as_of"] == RFC_A_TIME and seeded["issuer_id"] == "mock-marketplace"
    assert len(seeded["pushed"]) == 20
    assert all(p["outcome"] == "inserted" for p in seeded["pushed"])
    assert [p["seq"] for p in seeded["pushed"]] == list(range(1, 21))
    assert seeded["counts"]["inserted"] == 20 and seeded["counts"]["refused"] == 0
    records = store.list_sources("maya-001")
    assert len(records) == 20
    assert all(r.source_trust == "issuer_verified" for r in records)
    assert "rec-0001-021" not in {r.record_id for r in records}
    state = client.get("/admin/state").json()
    assert state["case_number"] == 1 and len(state["pushed"]) == 20
    assert state["corrections"] == [] and state["lookups_served"] == 0


# Cases 4, 5, 6, 41 and 50 carry deliveries whose received_at is earlier than their
# predecessors'; gold.ingest indexes the list by position, so pushes must follow list order.
@pytest.mark.parametrize("case_number", [1, 2, 4, 5, 6, 21, 23, 27, 28, 30, 41, 50])
def test_pushed_outcomes_agree_with_gold(mkeys, store, case_number):
    gold = [g for g in load_case(case_number)["gold"]["ingest"] if g["connector"] == "merchant"]
    gold.sort(key=lambda g: g["index"])
    case = load_case_without_gold(case_number)
    last = max(d["received_at"] for d in case["merchant_deliveries"])
    _, seeded = _seeded(mkeys, store, case_number, as_of=last)
    pushed = seeded["pushed"]
    assert len(pushed) == len(gold) == len(case["merchant_deliveries"])
    for want, got in zip(gold, pushed):
        assert got["event_id"] == want["id"]
        assert got["outcome"] in GOLD_TO_OUTCOME[want["outcome"]], (want, got)


def test_pushes_follow_list_order_not_received_at_order(mkeys):
    case = load_case_without_gold(4)
    times = [d["received_at"] for d in case["merchant_deliveries"]]
    assert times != sorted(times)                    # the case really is out of time order
    seen = []
    client = _client(mkeys, _recording_sink(seen))
    seeded = client.post("/admin/seed", params={"case": 4, "as_of": max(times)}).json()
    want = [d["event"]["event_id"] for d in case["merchant_deliveries"]]
    assert [p["event_id"] for p in seeded["pushed"]] == want
    assert [payload["event"]["event_id"] for payload, _ in seen] == want
    assert [received_at for _, received_at in seen] == times


def test_case_28_conflicting_bytes_are_refused_and_the_first_revision_stays(mkeys, store):
    _, seeded = _seeded(mkeys, store, 28, as_of="2026-08-19T16:05:00Z")
    conflict = [p for p in seeded["pushed"] if p["event_id"] == "evt-0028-001"]
    assert [p["outcome"] for p in conflict] == ["inserted", "refused"]
    assert "evt-0028-001" in conflict[1]["reason"]
    active = store.get_source("rec-0028-001")
    assert active.revision == 1 and active.event_id == "evt-0028-001"
    assert len(store.source_history("rec-0028-001")) == 1
    assert seeded["counts"]["refused"] == 1


def test_case_23_stale_revision_does_not_displace_the_correction(mkeys, store):
    _, seeded = _seeded(mkeys, store, 23, as_of="2026-07-16T05:05:00Z")
    by_event = {p["event_id"]: p for p in seeded["pushed"]}
    assert by_event["evt-0023-010"]["outcome"] == "corrected"
    assert by_event["evt-0023-011"]["outcome"] in {"stale", "duplicate"}
    assert store.get_source("rec-0023-009").revision == 2
    assert [r.revision for r in store.source_history("rec-0023-009")] == [1, 2]


# --------------------------------------------------------------------------- corrections


def test_admin_correct_pushes_a_new_signed_revision(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    response = client.post("/admin/correct", json={
        "record_id": "rec-0001-005", "changes": {"status": "pending"},
        "correction_reason": "refund reopened by the platform"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["outcome"] == "corrected" and body["revision"] == 2
    assert body["event_id"] == "evt-0001-005-r2" and body["received_at"] == RFC_A_TIME
    record = body["record"]
    assert record["event"]["status"] == "pending" and record["event"]["corrects_revision"] == 1
    assert record["event"]["correction_reason"] == "refund reopened by the platform"
    assert record["extra"]["case_kind"] == "refund" and "extra" in record
    active = store.get_source("rec-0001-005")
    assert active.revision == 2 and active.corrects_revision == 1 and active.status == "pending"
    history = store.source_history("rec-0001-005")
    assert [r.revision for r in history] == [1, 2] and history[0].status == "completed"
    listed = client.get("/records/rec-0001-005").json()
    assert [r["event"]["revision"] for r in listed["records"]] == [1, 2]
    state = client.get("/admin/state").json()
    assert state["corrections"][0]["record_id"] == "rec-0001-005"
    assert state["corrections"][0]["revision"] == 2 and len(state["pushed"]) == 21


def test_admin_correct_refuses_identity_changes_and_unknown_records(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    for field in ("record_id", "issuer_id", "subject_ref", "kind", "event_id"):
        response = client.post("/admin/correct", json={
            "record_id": "rec-0001-005", "changes": {field: "other"}, "correction_reason": "x"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_correction"
        assert field in response.json()["reason"]
    missing = client.post("/admin/correct", json={
        "record_id": "rec-0001-999", "changes": {"status": "pending"}, "correction_reason": "x"})
    assert missing.status_code == 404 and missing.json()["code"] == "unknown_record"
    no_reason = client.post("/admin/correct", json={
        "record_id": "rec-0001-005", "changes": {"status": "pending"}, "correction_reason": ""})
    assert no_reason.status_code == 400 and no_reason.json()["code"] == "invalid_correction"
    bad_value = client.post("/admin/correct", json={
        "record_id": "rec-0001-005", "changes": {"amount_minor": 12.5}, "correction_reason": "x"})
    assert bad_value.status_code == 400 and bad_value.json()["code"] == "invalid_correction"
    assert store.get_source("rec-0001-005").revision == 1


def test_admin_correct_refuses_a_scalar_covers_record_ids(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    for value in (5, True, 1.5):
        response = client.post("/admin/correct", json={
            "record_id": "rec-0001-021", "changes": {"covers_record_ids": value},
            "correction_reason": "x"})
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_correction"
        assert "rec-0001-021" in response.json()["reason"]
        assert "5" not in response.json()["reason"].replace("rec-0001-021", "")
    assert client.get("/admin/state").json()["corrections"] == []
    assert len(client.get("/records/rec-0001-021").json()["records"]) == 1


def test_correction_of_a_payout_can_unsettle_it(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    response = client.post("/admin/correct", json={
        "record_id": "rec-0001-021", "changes": {"status": "pending", "settled_at": None},
        "correction_reason": "settlement reversed"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["outcome"] == "inserted"       # the lookup-only payout was never pushed before
    assert body["record"]["event"]["status"] == "pending"
    assert "effective_at" not in body["record"]["event"]
    assert body["record"]["extra"]["settled_at"] is None
    found = client.get("/settlements", params={"payout_reference": "PO-0001-01"}).json()
    assert sorted(r["event"]["revision"] for r in found["records"]) == [1, 2]


# --------------------------------------------------------------------------- lookups


def _verify(envelope, mkeys):
    entry = verify(envelope["event"], envelope["signature"], mkeys.registry(),
                   domain="alibi:record:v1", issuer_id="mock-marketplace")
    assert entry.kid == "merchant_mock"


def test_lookup_of_po_0001_01_appears_at_its_availability(mkeys, store):
    client, seeded = _seeded(mkeys, store, 1, as_of="2026-10-27T16:04:59Z")
    assert len(seeded["pushed"]) == 20
    empty = client.get("/settlements", params={"payout_reference": "PO-0001-01"})
    assert empty.status_code == 200 and empty.json()["records"] == []
    assert client.get("/records/rec-0001-021").status_code == 404
    assert client.get("/records/rec-0001-021").json()["code"] == "not_found"

    advanced = client.post("/admin/advance", params={"as_of": RFC_A_TIME})
    assert advanced.status_code == 200 and advanced.json()["pushed"] == []
    found = client.get("/settlements", params={"payout_reference": "PO-0001-01"}).json()
    assert found["as_of"] == RFC_A_TIME and found["query"] == {"payout_reference": "PO-0001-01"}
    [envelope] = found["records"]
    assert envelope["event"]["record_id"] == "rec-0001-021"
    assert envelope["event"]["kind"] == "payout" and envelope["event"]["payment_ref"] == "PO-0001-01"
    assert envelope["event"]["effective_at"] == "2026-10-27T16:00:00Z"
    assert envelope["extra"]["case_kind"] == "payout"
    assert envelope["extra"]["payout_reference"] == "PO-0001-01"
    assert envelope["signature"]["kid"] == "merchant_mock"
    _verify(envelope, mkeys)
    MerchantEvent.model_validate(envelope["event"])

    for params in ({"covers": "rec-0001-001"}, {"payout_date": "2026-10-27"},
                   {"from": "2026-10-26", "to": "2026-10-28"}, {"payout_reference": "PO-0001-01"}):
        response = client.get("/payouts", params=params)
        assert response.status_code == 200, response.json()
        assert [r["event"]["record_id"] for r in response.json()["records"]] == ["rec-0001-021"]
        assert response.json()["query"] == params
    assert client.get("/payouts", params={"payout_date": "2026-10-26"}).json()["records"][0][
        "event"]["record_id"] == "rec-0001-021"            # initiated that day
    assert client.get("/payouts", params={"payout_date": "2026-10-25"}).json()["records"] == []
    one = client.get("/records/rec-0001-021").json()
    assert [r["event"]["record_id"] for r in one["records"]] == ["rec-0001-021"]
    assert client.get("/records/rec-0001-999").status_code == 404
    assert client.get("/records/rec-0001-999").json()["code"] == "not_found"
    # The lookup-only payout never entered the passport through a lookup.
    assert "rec-0001-021" not in {r.record_id for r in store.list_sources("maya-001")}


def test_case_2_pending_payout_appears_five_minutes_after_status_as_of(mkeys, store):
    client, _ = _seeded(mkeys, store, 2, as_of="2026-10-29T06:34:59Z")
    assert client.get("/settlements", params={"payout_reference": "PO-0002-01"}).json()["records"] == []
    client.post("/admin/advance", params={"as_of": "2026-10-29T06:35:00Z"})
    [envelope] = client.get("/settlements", params={"payout_reference": "PO-0002-01"}).json()["records"]
    assert envelope["event"]["record_id"] == "rec-0002-021"
    assert envelope["event"]["status"] == "pending"
    assert "effective_at" not in envelope["event"]
    assert envelope["extra"]["status_as_of"] == "2026-10-29T06:30:00Z"
    assert envelope["extra"]["settled_at"] is None
    _verify(envelope, mkeys)


def test_lookup_results_are_deduplicated_by_event_id(mkeys, store):
    client, _ = _seeded(mkeys, store, 27, as_of="2026-12-31T00:00:00Z")
    found = client.get("/records/rec-0027-001").json()
    assert len(found["records"]) == 1
    assert client.get("/admin/state").json()["pushed"][13]["outcome"] == "duplicate"


def test_payout_query_validation(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)

    def code(params):
        response = client.get("/payouts", params=params)
        return response.status_code, response.json()["code"]

    assert code({}) == (400, "invalid_query")
    assert code({"covers": "rec-0001-001", "payout_date": "2026-10-27"}) == (400, "invalid_query")
    assert code({"from": "2026-10-01"}) == (400, "invalid_query")
    assert code({"from": "2026-10-01", "to": "2026-11-01"}) == (400, "invalid_query")   # 32 days
    assert code({"from": "2026-10-28", "to": "2026-10-26"}) == (400, "invalid_query")
    assert code({"payout_date": "27/10/2026"}) == (400, "invalid_time")
    assert code({"from": "2026-10-xx", "to": "2026-10-28"}) == (400, "invalid_time")
    ok = client.get("/payouts", params={"from": "2026-10-01", "to": "2026-10-31"})
    assert ok.status_code == 200 and len(ok.json()["records"]) == 1
    assert client.get("/settlements").json()["code"] == "invalid_query"
    assert client.get("/settlements", params={"payout_reference": ""}).json()["code"] == "invalid_query"


def test_payout_query_empty_keys_are_refused_not_crashed(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    for params in ({"covers": ""}, {"payout_reference": ""}, {"payout_date": ""},
                   {"from": "", "to": ""}, {"from": "2026-10-26", "to": ""}):
        response = client.get("/payouts", params=params)
        assert response.status_code == 400, (params, response.text)
        assert response.json()["code"] == "invalid_query", params
        assert "must not be empty" in response.json()["reason"]
    assert client.get("/admin/state").json()["lookups_served"] == 0


def test_payout_date_in_any_iso_spelling_matches_the_same_day(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    for spelling in ("2026-10-27", "20261027", "2026-W44-2"):
        response = client.get("/payouts", params={"payout_date": spelling})
        assert response.status_code == 200, response.text
        assert [r["event"]["record_id"] for r in response.json()["records"]] == ["rec-0001-021"]
        assert response.json()["query"] == {"payout_date": spelling}


def test_payout_range_at_the_end_of_the_calendar_is_empty_not_an_error(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    for params in ({"from": "9999-12-31", "to": "9999-12-31"}, {"from": "9999-12-01", "to": "9999-12-31"}):
        response = client.get("/payouts", params=params)
        assert response.status_code == 200, (params, response.text)
        assert response.json()["records"] == []


def test_admin_lookups_records_every_served_query(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    assert client.get("/admin/lookups").json() == {"lookups": []}
    client.get("/settlements", params={"payout_reference": "PO-0001-01"})
    client.get("/payouts", params={"covers": "rec-0001-001"})
    client.get("/records/rec-0001-001")
    client.get("/records/rec-0001-999")                     # a miss is not a served lookup
    client.get("/payouts", params={"covers": "rec-0001-001", "payout_date": "x"})   # refused
    lookups = client.get("/admin/lookups").json()["lookups"]
    assert [(l["route"], l["query"], l["record_ids"]) for l in lookups] == [
        ("/settlements", {"payout_reference": "PO-0001-01"}, ["rec-0001-021"]),
        ("/payouts", {"covers": "rec-0001-001"}, ["rec-0001-021"]),
        ("/records/{record_id}", {"record_id": "rec-0001-001"}, ["rec-0001-001"]),
    ]
    assert all(l["at"] == RFC_A_TIME for l in lookups)
    assert client.get("/admin/state").json()["lookups_served"] == 3


# --------------------------------------------------------------------------- retry and clock


def test_retry_resends_identical_bytes_and_the_passport_reports_duplicate(mkeys, store):
    client, seeded = _seeded(mkeys, store, 1)
    original = seeded["pushed"][0]
    response = client.post("/admin/retry", json={"event_id": original["event_id"]})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["event_id"] == "evt-0001-001" and body["same_bytes"] is True
    assert body["payload_sha256"] == original["payload_sha256"]
    assert body["outcome"] == "duplicate"
    assert len(store.list_sources("maya-001")) == 20
    state = client.get("/admin/state").json()
    assert state["pushed"][-1]["seq"] == 21 and state["pushed"][-1]["outcome"] == "duplicate"
    unknown = client.post("/admin/retry", json={"event_id": "evt-0001-999"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_event"
    assert client.post("/admin/retry", json={}).json()["code"] == "invalid_request"
    assert client.post("/admin/retry", json=[1]).status_code == 400


def test_advance_pushes_only_newly_visible_deliveries(mkeys, store):
    client, seeded = _seeded(mkeys, store, 1, as_of="2026-10-26T08:04:59Z")
    assert len(seeded["pushed"]) == 19
    assert len(store.list_sources("maya-001")) == 19
    advanced = client.post("/admin/advance", params={"as_of": "2026-10-28T00:00:00Z"})
    assert advanced.status_code == 200, advanced.json()
    body = advanced.json()
    assert body["as_of"] == "2026-10-28T00:00:00Z"
    assert [p["record_id"] for p in body["pushed"]] == ["rec-0001-020"]
    assert body["pushed"][0]["seq"] == 20 and body["pushed"][0]["outcome"] == "inserted"
    assert len(store.list_sources("maya-001")) == 20
    again = client.post("/admin/advance", params={"as_of": "2026-10-28T00:00:00Z"})
    assert again.status_code == 200 and again.json()["pushed"] == []
    back = client.post("/admin/advance", params={"as_of": "2026-10-27T00:00:00Z"})
    assert back.status_code == 409 and back.json()["code"] == "clock_backwards"
    assert client.get("/").json()["as_of"] == "2026-10-28T00:00:00Z"


def test_fractional_second_clock_is_floored_so_pushes_and_lookups_agree(mkeys, store):
    # agents.data.api.lookup compares the clock as text, pending() as a datetime; a fractional
    # second would push rec-0001-020 (received 08:05:00Z) and then hide it from the lookups.
    client, seeded = _seeded(mkeys, store, 1, as_of="2026-10-26T08:05:00.000001Z")
    assert seeded["as_of"] == "2026-10-26T08:05:00Z"
    assert len(seeded["pushed"]) == 20 and seeded["pushed"][-1]["record_id"] == "rec-0001-020"
    assert client.get("/records/rec-0001-020").status_code == 200
    assert client.get("/").json()["as_of"] == "2026-10-26T08:05:00Z"

    advanced = client.post("/admin/advance", params={"as_of": "2026-10-27T16:05:00.500Z"})
    assert advanced.status_code == 200, advanced.json()
    assert advanced.json()["as_of"] == PO_VISIBLE
    found = client.get("/settlements", params={"payout_reference": "PO-0001-01"}).json()
    assert [r["event"]["record_id"] for r in found["records"]] == ["rec-0001-021"]
    assert found["as_of"] == PO_VISIBLE
    # Within the same second the clock does not move, so this is a no-op, not clock_backwards.
    same = client.post("/admin/advance", params={"as_of": "2026-10-27T16:05:00.250Z"})
    assert same.status_code == 200 and same.json()["as_of"] == PO_VISIBLE
    back = client.post("/admin/advance", params={"as_of": "2026-10-27T16:04:59.999Z"})
    assert back.status_code == 409 and back.json()["code"] == "clock_backwards"
    assert back.json()["reason"].endswith("2026-10-27T16:04:59Z")


def test_seed_and_advance_refusals(mkeys):
    client = _client(mkeys)
    unknown = client.post("/admin/seed", params={"case": 1000})
    assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_case"
    assert client.post("/admin/seed", params={"case": 999999}).json()["code"] == "unknown_case"
    bad = client.post("/admin/seed", params={"case": 1, "as_of": "yesterday"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_time"
    naive = client.post("/admin/seed", params={"case": 1, "as_of": "2026-11-02T09:00:00"})
    assert naive.status_code == 400 and naive.json()["code"] == "invalid_time"
    assert client.get("/").json()["case_number"] is None
    assert client.post("/admin/advance", params={"as_of": RFC_A_TIME}).json()["code"] == "unknown_case"
    assert client.post("/admin/seed", params={"case": "one"}).json()["code"] == "invalid_request"
    client.post("/admin/seed")
    bad_advance = client.post("/admin/advance", params={"as_of": "soon"})
    assert bad_advance.status_code == 400 and bad_advance.json()["code"] == "invalid_time"
    assert client.post("/admin/advance").json()["code"] == "invalid_request"


def test_reseeding_resets_state(mkeys, store):
    client, _ = _seeded(mkeys, store, 1)
    client.get("/records/rec-0001-001")
    reseeded = client.post("/admin/seed", params={"case": 2}).json()
    assert reseeded["case_number"] == 2 and len(reseeded["pushed"]) == 20
    state = client.get("/admin/state").json()
    assert state["case_number"] == 2 and len(state["pushed"]) == 20
    assert state["lookups_served"] == 0
    assert client.post("/admin/retry", json={"event_id": "evt-0001-001"}).status_code == 404


# --------------------------------------------------------------------------- sinks


def test_without_a_sink_pushes_are_not_delivered_and_lookups_still_work(mkeys):
    client = _client(mkeys)
    seeded = client.post("/admin/seed", params={"case": 1}).json()
    assert len(seeded["pushed"]) == 20
    assert all(p["outcome"] == "not_delivered" and p["reason"] is None for p in seeded["pushed"])
    assert seeded["counts"]["not_delivered"] == 20 and seeded["counts"]["inserted"] == 0
    found = client.get("/settlements", params={"payout_reference": "PO-0001-01"}).json()
    assert [r["event"]["record_id"] for r in found["records"]] == ["rec-0001-021"]
    assert len(client.get("/records/rec-0001-001").json()["records"]) == 1
    retried = client.post("/admin/retry", json={"event_id": "evt-0001-001"}).json()
    assert retried["outcome"] == "not_delivered" and retried["same_bytes"] is True


def test_pushed_payloads_are_bare_signed_events(mkeys):
    seen = []
    client = _client(mkeys, _recording_sink(seen))
    seeded = client.post("/admin/seed", params={"case": 1}).json()
    assert len(seen) == 20 and seeded["counts"]["inserted"] == 20
    for (payload, received_at), entry in zip(seen, seeded["pushed"]):
        assert set(payload) == {"event", "signature"}
        assert "extra" not in payload
        MerchantEvent.model_validate(payload["event"])
        assert payload["signature"]["kid"] == "merchant_mock"
        assert received_at == entry["received_at"]
        assert canon.digest(payload) == entry["payload_sha256"]
        _verify(payload, mkeys)
    assert [p["event"]["record_id"] for p, _ in seen][:2] == ["rec-0001-001", "rec-0001-002"]
    assert seen[0][1] == "2026-10-05T10:05:00Z"


def test_a_sink_that_raises_or_misbehaves_is_reported_as_error(mkeys):
    def broken(payload, received_at):
        raise RuntimeError("secret detail that must not leak")

    client = _client(mkeys, broken)
    seeded = client.post("/admin/seed", params={"case": 1}).json()
    assert seeded["counts"]["error"] == 20
    assert seeded["pushed"][0]["reason"] == "sink raised RuntimeError"
    client = _client(mkeys, lambda payload, received_at: {"status": "whatever"})
    seeded = client.post("/admin/seed", params={"case": 1}).json()
    assert seeded["counts"]["error"] == 20


def test_http_sink_maps_responses_and_connection_errors(mkeys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        mode = request.url.host
        if mode == "ok":
            return httpx.Response(200, json={"status": "inserted", "record_id": "rec-0001-001",
                                             "revision": 1, "token": "never-copied"})
        if mode == "refused":
            return httpx.Response(409, json={"code": "idempotency_conflict",
                                             "reason": "event evt-0001-001 was already received"})
        if mode == "down":
            raise httpx.ConnectError("connection refused")
        if mode == "html":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(500, json={"detail": "boom"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    payload = {"event": {"event_id": "evt-0001-001"}, "signature": {"kid": "merchant_mock"}}

    ok = http_sink("http://ok/webhooks/merchant", client=http)(payload, "2026-10-05T10:05:00Z")
    assert ok == {"status": "inserted", "record_id": "rec-0001-001", "revision": 1}
    assert calls[-1].method == "POST"
    assert calls[-1].url.params["received_at"] == "2026-10-05T10:05:00Z"
    assert calls[-1].url.path == "/webhooks/merchant"
    assert canon.parse_json(calls[-1].content) == payload

    refused = http_sink("http://refused/w", client=http)(payload, "2026-10-05T10:05:00Z")
    assert refused == {"status": "refused", "reason": "event evt-0001-001 was already received",
                       "code": "idempotency_conflict"}
    down = http_sink("http://down/w", client=http)(payload, "2026-10-05T10:05:00Z")
    assert down["status"] == "error" and "ConnectError" in down["reason"]
    html = http_sink("http://html/w", client=http)(payload, "2026-10-05T10:05:00Z")
    assert html["status"] == "error"
    server = http_sink("http://server/w", client=http)(payload, "2026-10-05T10:05:00Z")
    assert server == {"status": "error", "reason": "HTTP 500"}


def test_an_http_sink_carries_each_delivery_time_and_passport_url_wires_one(mkeys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["received_at"])
        return httpx.Response(200, json={"status": "inserted"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = _client(mkeys, http_sink("http://passport.test/webhooks/merchant", client=http))
    seeded = client.post("/admin/seed", params={"case": 1}).json()
    assert seeded["counts"]["inserted"] == 20
    assert calls == [p["received_at"] for p in seeded["pushed"]]
    app = create_merchant_app(keystore=mkeys, passport_url="http://127.0.0.1:1/webhooks/merchant")
    assert app.state.merchant.sink is not None
