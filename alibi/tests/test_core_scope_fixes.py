"""Track 1b-i regressions: request ids scoped by issuer, follow-up scope containment, the
case_ref subject binding, verify_package defaults from the stored RFC, merchant_mock in the
demo keys and ``stale`` for a redelivered superseded revision."""

import copy
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from alibi import api
from alibi.core import canon
from alibi.core.ingest import IngestError, REFUSAL_SCOPE_WIDER, ingest_merchant_event, receive_rfc
from alibi.core.package import CASE_BINDING_DOMAIN, case_binding, package_signing_payload
from alibi.core.schemas import KeyPurpose, RequestForContext, SigningDomain
from alibi.core.signing import DEMO_KEYS, DemoKeystore
from alibi.core.store import (
    SCHEMA_VERSION,
    IdempotencyConflict,
    NotFound,
    Store,
    _KEY_CHECK,
)
from alibi.fixtures import maya

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SUBJ = maya.SUBJECT_REF
PERIOD = maya.RFC_B_PERIOD


def rfc(**over) -> RequestForContext:
    data = {
        "issuer": "bank_a", "request_id": "rfc_shared_001", "subject_ref": SUBJ,
        "audience": "bank_a", "txn_ids": ["txn_9001"],
        "questions": [{"question_id": "q1", "text": "What is the source of this credit?"}],
        "issued_at": "2026-11-04T09:00:00Z", "expires_at": "2026-11-11T09:00:00Z",
    }
    data.update(over)
    return RequestForContext.model_validate(data)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db" / "passport.db", master_key=Fernet.generate_key()) as s:
        yield s


@pytest.fixture
def passport(tmp_path, keystore):
    """The supported fixture loaded through the API, with the fixture's two RFCs stored."""
    with api.open_store(tmp_path / "data" / "passport.db", master_key=Fernet.generate_key()) as s:
        fixture = api.load_fixture(s, "supported", keystore=keystore, now=NOW)
        yield SimpleNamespace(store=s, keys=keystore, registry=keystore.registry(), fixture=fixture)


def intake(passport, request, *, accounts=None, now=NOW):
    return api.receive_rfc(passport.store, maya.sign_request(request, passport.keys),
                           authenticated_issuer=request["issuer"], registry=passport.registry,
                           account_refs=accounts, now=now)


def deliver(passport, request_id):
    api.run_investigation(passport.store, request_id,
                          settlement_lookup=passport.fixture["settlement_lookup"],
                          registry=passport.registry, now=NOW)
    draft = api.build_review(passport.store, request_id, now=NOW)
    approval = api.approve(passport.store, draft.draft_id, holder_id="holder_maya", now=NOW)
    return api.sign_and_package(passport.store, approval.approval_id, keystore=passport.keys, now=NOW)


def resign(raw: dict, keystore) -> dict:
    raw["body_sha256"] = canon.digest(raw["body"])
    raw["signature"] = keystore.sign(package_signing_payload(raw), kid="alibi_demo",
                                     domain=SigningDomain.PACKAGE).to_json_dict()
    return raw


# --------------------------------------------------------------------------- store: (issuer, request_id)


def test_two_banks_can_hold_the_same_request_id(store):
    a = rfc()
    b = rfc(issuer="bank_b", audience="bank_b", txn_ids=None, period=PERIOD)
    assert store.save_request(a) == "inserted"
    assert store.save_request(b) == "inserted"
    # The bare id resolves to the request received first; the issuer reaches the other.
    assert store.get_request("rfc_shared_001") == a
    assert store.get_request("rfc_shared_001", issuer="bank_a") == a
    assert store.get_request("rfc_shared_001", issuer="bank_b") == b
    with pytest.raises(NotFound):
        store.get_request("rfc_shared_001", issuer="bank_c")
    assert store.list_requests(issuer="bank_b") == [b]
    assert store.list_requests(audience="bank_a") == [a]
    assert [r.issuer for r in store.list_requests(subject_ref=SUBJ)] == ["bank_a", "bank_b"]
    # Each bank's own id is still idempotent, and a conflict names only that bank's request.
    assert store.save_request(b) == "duplicate"
    with pytest.raises(IdempotencyConflict) as exc:
        store.save_request(rfc(issuer="bank_b", audience="bank_b", txn_ids=None, period=PERIOD,
                              expires_at="2026-11-12T09:00:00Z"))
    assert "bank_a" not in str(exc.value) and "bank_b" not in str(exc.value)


def test_schema_2_database_is_migrated_to_issuer_scoped_requests(tmp_path):
    key = Fernet.generate_key()
    fernet = Fernet(key)
    db = tmp_path / "old.db"
    old = rfc()
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);
        CREATE TABLE requests (
            request_id TEXT PRIMARY KEY, subject_ref TEXT NOT NULL, audience TEXT NOT NULL,
            payload BLOB NOT NULL
        );
    """)
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", ("2",))
    conn.execute("INSERT INTO meta VALUES ('key_check', ?)", (fernet.encrypt(_KEY_CHECK),))
    conn.execute("INSERT INTO requests VALUES (?, ?, ?, ?)",
                 (old.request_id, old.subject_ref, old.audience,
                  fernet.encrypt(canon.canonical_bytes(old))))
    conn.commit()
    conn.close()

    with Store(db, master_key=key) as store:
        assert store.get_request("rfc_shared_001") == old
        assert store.get_request("rfc_shared_001", issuer="bank_a") == old
        assert store.save_request(rfc(issuer="bank_b", audience="bank_b")) == "inserted"
        assert len(store.list_requests()) == 2
        [row] = store._q("SELECT value FROM meta WHERE key = 'schema_version'")
        assert row["value"] == SCHEMA_VERSION == "3"
    with Store(db, master_key=key) as store:   # reopening a migrated file is a no-op
        assert [r.issuer for r in store.list_requests()] == ["bank_a", "bank_b"]


def test_redelivered_superseded_revision_is_stale_not_duplicate(store, keystore, registry):
    fx = maya.load_variant("supported")
    original = next(e for e in fx["merchant_events"] if e["record_id"] == "po_5001")
    ingest_merchant_event(store, registry, maya.sign_event(original, keystore), now=NOW)
    correction = {**copy.deepcopy(original), "revision": 2, "event_id": "evt_po_5001_r2",
                  "corrects_revision": 1,
                  "correction_reason": "Reissued with the final settlement reference."}
    assert ingest_merchant_event(store, registry, maya.sign_event(correction, keystore),
                                 now=NOW).status == "corrected"
    # The same revision 1 content arrives again under a new event id.
    redelivered = {**copy.deepcopy(original), "event_id": "evt_po_5001_r1_again"}
    result = ingest_merchant_event(store, registry, maya.sign_event(redelivered, keystore), now=NOW)
    assert (result.status, result.active, result.record.revision) == ("stale", False, 1)
    assert store.get_source("po_5001").revision == 2
    assert [r.revision for r in store.source_history("po_5001")] == [1, 2]
    # An identical replay of an event already received is a duplicate delivery, active or not.
    assert ingest_merchant_event(store, registry, maya.sign_event(redelivered, keystore),
                                 now=NOW).status == "duplicate"
    assert ingest_merchant_event(store, registry, maya.sign_event(correction, keystore),
                                 now=NOW).status == "duplicate"


# --------------------------------------------------------------------------- ingest: request ids per bank


def test_receive_rfc_scopes_external_request_ids_by_issuer(passport):
    bank_a, bank_b = maya.build_fixture("supported")["rfcs"]
    collision = {**bank_b, "request_id": bank_a["request_id"]}
    stored = intake(passport, collision)
    assert stored.issuer == "bank_b" and stored.request_id == bank_a["request_id"]
    assert passport.store.get_request(bank_a["request_id"]).issuer == "bank_a"
    assert passport.store.get_request(bank_a["request_id"], issuer="bank_b") == stored
    # Bank A changing its own request is refused without naming Bank B.
    with pytest.raises(IngestError, match="different content") as exc:
        intake(passport, {**bank_a, "expires_at": "2026-11-12T09:00:00Z"},
               accounts=[maya.ACCOUNT_REF])
    assert "bank_b" not in str(exc.value)
    # Bank B resending its colliding request is a duplicate, not a conflict.
    assert intake(passport, collision) == stored


# --------------------------------------------------------------------------- ingest: follow-up containment


def followup(parent, request_id="rfc_follow", **over):
    return {**copy.deepcopy(parent), "request_id": request_id,
            "parent_request_id": parent["request_id"], "issued_at": "2026-11-06T09:00:00Z", **over}


@pytest.mark.parametrize("case", ["earlier_from", "later_to", "period_to_txns", "txns_to_period",
                                  "extra_txn", "txn_outside_period"])
def test_followup_outside_the_parent_scope_is_refused(passport, case):
    bank_a, bank_b = maya.build_fixture("supported")["rfcs"]
    accounts = None
    if case == "earlier_from":
        request = followup(bank_b, period={**PERIOD, "from": "2026-09-01T00:00:00Z"})
    elif case == "later_to":
        request = followup(bank_b, period={**PERIOD, "to": "2026-11-02T00:00:00Z"})
    elif case == "period_to_txns":
        request = followup(bank_b, period=None, txn_ids=[maya.CREDIT_TXN_ID])
        accounts = [maya.ACCOUNT_REF]
    elif case == "txns_to_period":
        request = followup(bank_a, txn_ids=None, period=PERIOD)
    elif case == "extra_txn":
        api.ingest_bank_txns(passport.store, [{**maya.build_fixture("supported")["bank_txns"][0],
                                               "txn_id": "txn_9002"}],
                             subject_ref=SUBJ, connection_id=maya.CONNECTION_ID, now=NOW)
        request = followup(bank_a, txn_ids=[maya.CREDIT_TXN_ID, "txn_9002"])
        accounts = [maya.ACCOUNT_REF]
    else:  # a period parent, a follow-up naming a txn booked before the period
        credit = maya.build_fixture("supported")["bank_txns"][0]
        api.ingest_bank_txns(passport.store, [{**credit, "txn_id": "txn_old",
                                               "booked_at": "2026-09-15T10:00:00Z"}],
                             subject_ref=SUBJ, connection_id=maya.CONNECTION_ID, now=NOW)
        request = followup(bank_b, txn_ids=["txn_old"])
        accounts = [maya.ACCOUNT_REF]
    with pytest.raises(IngestError, match=REFUSAL_SCOPE_WIDER):
        intake(passport, request, accounts=accounts)
    assert all(r.request_id != "rfc_follow" for r in passport.store.list_requests())
    refused = [a for a in passport.store.list_audit(target_id="rfc_follow") if a.outcome == "refused"]
    assert len(refused) == 1 and REFUSAL_SCOPE_WIDER in refused[0].detail


def test_followup_inside_the_parent_scope_is_accepted(passport):
    bank_a, bank_b = maya.build_fixture("supported")["rfcs"]
    narrower = intake(passport, followup(bank_b, request_id="rfc_b_narrow",
                                         period={"from": "2026-10-10T00:00:00Z",
                                                 "to": "2026-10-20T00:00:00Z"}))
    assert narrower.parent_request_id == bank_b["request_id"]
    same = intake(passport, followup(bank_a, request_id="rfc_a_same"), accounts=[maya.ACCOUNT_REF])
    assert same.txn_ids == bank_a["txn_ids"]
    # A follow-up to a period request may pin transactions booked inside that period.
    credit = maya.build_fixture("supported")["bank_txns"][0]
    api.ingest_bank_txns(passport.store, [{**credit, "txn_id": "txn_in_period",
                                           "booked_at": "2026-10-15T10:00:00Z"}],
                         subject_ref=SUBJ, connection_id=maya.CONNECTION_ID, now=NOW)
    pinned = intake(passport, followup(bank_b, request_id="rfc_b_pinned",
                                       txn_ids=["txn_in_period"]), accounts=[maya.ACCOUNT_REF])
    assert pinned.period is not None and pinned.txn_ids == ["txn_in_period"]


# --------------------------------------------------------------------------- package: case_ref binds the subject


def test_case_binding_is_deterministic_and_subject_specific():
    ref = case_binding("bank_b", "rfc_b_001", SUBJ)
    assert ref == "case_" + canon.digest([CASE_BINDING_DOMAIN, "bank_b", "rfc_b_001", SUBJ])
    assert ref == case_binding("bank_b", "rfc_b_001", SUBJ)
    assert len({ref, case_binding("bank_a", "rfc_b_001", SUBJ),
                case_binding("bank_b", "rfc_b_002", SUBJ),
                case_binding("bank_b", "rfc_b_001", "subj_other")}) == 4
    assert len(ref) == 69   # a valid Ident: "case_" + 64 hex, under 128 characters


def test_package_case_ref_binds_subject_even_when_every_attachment_is_redacted(passport):
    request = maya.build_fixture("supported")["rfcs"][1]
    request_id = request["request_id"]
    api.record_policy(passport.store, SUBJ, [
        {"type": "deny", "rule_id": "deny_subject", "fields": ["subject_ref"]},
    ], confirmed=True, now=NOW)
    package = deliver(passport, request_id)
    assert package.body.attachments
    assert all(a.form == "redacted_extract" for a in package.body.attachments)
    assert all("subject_ref" not in a.content for a in package.body.attachments)
    assert package.body.case_ref == case_binding("bank_b", request_id, SUBJ)
    options = {"expected_audience": "bank_b", "expected_request_id": request_id,
               "registry": passport.registry, "now": NOW}
    honest = api.verify_package(package, expected_subject=SUBJ, **options)
    assert honest.accepted and honest.errors == []
    other = api.verify_package(package, expected_subject="subj_other", **options)
    assert not other.accepted and not other.authentication.valid
    assert any("not bound to the expected subject" in r for r in other.authentication.reasons)
    # Without a subject the package still verifies, as before.
    assert api.verify_package(package, **options).accepted


def test_followup_package_shares_the_parent_case_ref(passport):
    bank_b = maya.build_fixture("supported")["rfcs"][1]
    intake(passport, followup(bank_b, request_id="rfc_b_002"))
    parent, child = deliver(passport, bank_b["request_id"]), deliver(passport, "rfc_b_002")
    assert child.body.case_ref == parent.body.case_ref == case_binding("bank_b", "rfc_b_001", SUBJ)
    assert api.verify_package(child, expected_audience="bank_b", registry=passport.registry,
                              now=NOW, expected_subject=SUBJ).accepted


def test_verify_package_defaults_subject_and_request_from_the_stored_rfc(passport):
    request = maya.build_fixture("supported")["rfcs"][1]
    package = deliver(passport, request["request_id"])
    options = {"expected_audience": "bank_b", "registry": passport.registry, "now": NOW}
    assert api.verify_package(package, store=passport.store, **options).accepted
    # A signed package whose case_ref binds another subject passes without the store...
    forged = resign({**package.to_json_dict(),
                     "body": {**package.to_json_dict()["body"],
                              "case_ref": case_binding("bank_b", request["request_id"], "subj_other")}},
                    passport.keys)
    assert api.verify_package(forged, **options).accepted
    # ...and fails once the stored RFC supplies the expected subject.
    bound = api.verify_package(forged, store=passport.store, **options)
    assert not bound.accepted
    assert any("not bound to the expected subject" in r for r in bound.authentication.reasons)
    # Explicit values win over the store, and an unknown request leaves no defaults.
    explicit = api.verify_package(package, store=passport.store, expected_subject="subj_other", **options)
    assert not explicit.accepted
    with api.open_store(passport.store.path.parent / "empty.db", master_key=Fernet.generate_key()) as empty:
        assert api.verify_package(forged, store=empty, **options).accepted


# --------------------------------------------------------------------------- signing: merchant_mock


def test_demo_keys_carry_the_team_case_issuer(tmp_path):
    assert DEMO_KEYS["merchant_mock"] == (KeyPurpose.RECORD_SIGNING, "mock-marketplace")
    from alibi.connectors.keys import CONNECTOR_KEYS
    from alibi.app.config import APP_KEYS
    assert DEMO_KEYS.items() <= CONNECTOR_KEYS.items()  # one key spec: the core's keys plus the case issuers
    assert CONNECTOR_KEYS == APP_KEYS                   # the connectors and the app share it
    assert DEMO_KEYS.items() <= APP_KEYS.items()       # the app adds kids, none clash
    keystore = DemoKeystore(tmp_path / "keys").ensure()
    entry = api.default_keystore(tmp_path / "keys").registry().get("merchant_mock")
    assert (entry.purpose, entry.issuer_id) == (KeyPurpose.RECORD_SIGNING, "mock-marketplace")
    assert keystore.registry().merged(DemoKeystore(tmp_path / "keys", specs=APP_KEYS).registry()).kids() \
        == sorted(APP_KEYS)
