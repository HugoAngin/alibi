"""Tests for agents/eval/sharing_delivery.py. Public synthetic data only (canonical GBP fixture)."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.eval.sharing_delivery import (
    ISSUER_RECORD_DOMAIN,
    PACKAGE_DOMAIN,
    PURPOSE_ISSUER_RECORD,
    PURPOSE_PACKAGE,
    SCHEMA_VERSION,
    V1_SCHEMA_VERSION,
    AuthError,
    BankInbox,
    ConflictError,
    TrustRegistry,
    VerificationError,
    body_digest,
    package_signing_key_from_master,
    public_key_b64,
    sign_detached,
    sign_package,
    sign_record,
    verify_detached,
    verify_package,
)

ALIBI = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
OTHER_MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def registry() -> TrustRegistry:
    reg = TrustRegistry()
    reg.add("alibi-demo-1", PURPOSE_PACKAGE, "alibi", ALIBI)
    reg.add("merchant-demo-1", PURPOSE_ISSUER_RECORD, "merchant_demo", MERCHANT)
    reg.add("merchant-other-1", PURPOSE_ISSUER_RECORD, "merchant_other", OTHER_MERCHANT)
    return reg


def payout_record(**overrides) -> dict:
    rec = {
        "schema_version": "1",
        "event_id": "evt-payout-1",
        "record_id": "payout-1",
        "revision": 1,
        "issuer_id": "merchant_demo",
        "subject_ref": "acct-maya",
        "kind": "payout",
        "occurred_at": "2026-10-03T09:00:00Z",
        "money": {"sales": 240000, "refunds": 18000, "fees": 6000, "net": 216000, "currency": "GBP"},
        "payout_ref": "PO-2026-10-03",
    }
    rec.update(overrides)
    return rec


def make_body(**overrides) -> dict:
    signed = sign_record(payout_record(), MERCHANT, "merchant-demo-1")
    body = {
        "schema_version": SCHEMA_VERSION,
        "package_id": "pkg-0001",
        "request_id": "rfc-0001",
        "audience": "bank_a",
        "case_id": "case-0001",
        "scope": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z", "txn_ids": ["txn-216000"]},
        "issued_at": "2026-11-07T10:00:00Z",
        "expires_at": "2026-11-14T10:00:00Z",
        "claims": [
            {
                "question_id": "q-credit-source",
                "status": "supported",
                "rationale": "Credit txn-216000 equals payout PO-2026-10-03: 240000 - 18000 - 6000 = 216000 GBP pence.",
                "evidence_refs": ["att-payout", "att-invoice"],
                "calculation": {"sales": 240000, "refunds": -18000, "fees": -6000, "total": 216000},
            },
            {
                "question_id": "q-source-of-funds",
                "status": "unresolved",
                "rationale": "No buyer-side evidence is available.",
                "evidence_refs": [],
                "gap": "buyer evidence unavailable",
            },
        ],
        "attachments": [
            {"attachment_id": "att-payout", "trust": "issuer_verified", "record": signed},
            {
                "attachment_id": "att-invoice",
                "trust": "self_supplied",
                "record": {"body": {"kind": "invoice", "amount": 240000, "currency": "GBP"}},
                "provenance": {"connector": "import", "retrieved_at": "2026-11-06T08:00:00Z"},
            },
        ],
    }
    body.update(overrides)
    return body


def seal(body: dict, key=ALIBI, key_id="alibi-demo-1") -> bytes:
    return sign_package(body, key, key_id, approved_body_digest=body_digest(body))


def reseal_envelope(env: dict) -> bytes:
    return rfc8785.dumps(env)


def code_of(raw: bytes, registry, recipient="bank_a", now=NOW) -> str:
    with pytest.raises(VerificationError) as exc:
        verify_package(raw, recipient=recipient, registry=registry, now=now)
    return exc.value.code


# --------------------------------------------------------------------------- signing and verification


def test_round_trip_keeps_authentication_trust_and_claims_separate(registry):
    raw = seal(make_body())
    res = verify_package(raw, recipient="bank_a", registry=registry, now=NOW)
    assert res.package_signature == "valid" and res.package_key_id == "alibi-demo-1"
    by_id = {a.attachment_id: a for a in res.attachments}
    assert by_id["att-payout"].trust == "issuer_verified"
    assert by_id["att-payout"].source_signature == "valid" and by_id["att-payout"].attested_by == "issuer"
    assert by_id["att-invoice"].trust == "self_supplied"
    assert by_id["att-invoice"].source_signature == "absent" and by_id["att-invoice"].attested_by == "package_signer"
    # A valid package signature does not make an unresolved claim true.
    assert {c["question_id"]: c["status"] for c in res.claims} == {
        "q-credit-source": "supported",
        "q-source-of-funds": "unresolved",
    }
    assert res.digest.startswith("sha256:") and json.loads(json.dumps(res.to_dict()))["recipient"] == "bank_a"


def test_sign_refuses_body_changed_after_approval():
    body = make_body()
    approved = body_digest(body)
    body["claims"][1]["status"] = "supported"
    with pytest.raises(VerificationError) as exc:
        sign_package(body, ALIBI, "alibi-demo-1", approved_body_digest=approved)
    assert exc.value.code == "approval_mismatch"


def test_sign_refuses_unlisted_body_field():
    body = make_body()
    body["private_review"] = {"excluded": 3}
    with pytest.raises(VerificationError) as exc:
        seal(body)
    assert exc.value.code == "schema"


def test_tampered_body_fails_package_signature(registry):
    env = json.loads(seal(make_body()))
    env["body"]["claims"][1]["status"] = "supported"
    assert code_of(reseal_envelope(env), registry) == "bad_signature"


def test_non_canonical_bytes_rejected(registry):
    pretty = json.dumps(json.loads(seal(make_body())), indent=2).encode()
    assert code_of(pretty, registry) == "non_canonical"


def test_duplicate_json_keys_rejected(registry):
    raw = seal(make_body())
    assert code_of(b'{"body":{},' + raw[1:-1] + b"}", registry) == "malformed"


def test_recipient_binding(registry):
    raw = seal(make_body())
    assert code_of(raw, registry, recipient="bank_b") == "wrong_recipient"


def test_expiry_is_half_open_and_future_issue_rejected(registry):
    raw = seal(make_body())
    expires = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    verify_package(raw, recipient="bank_a", registry=registry, now=expires - timedelta(seconds=1))
    assert code_of(raw, registry, now=expires) == "expired"
    assert code_of(raw, registry, now=datetime(2026, 11, 7, 9, 59, tzinfo=timezone.utc)) == "not_yet_valid"


def test_package_key_must_be_pinned_for_package_purpose(registry):
    assert code_of(seal(make_body(), key=MERCHANT, key_id="merchant-demo-1"), registry) == "wrong_key_purpose"
    stranger = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    assert code_of(seal(make_body(), key=stranger, key_id="alibi-demo-9"), registry) == "unknown_key"


def test_signing_domains_are_separate(registry):
    body = make_body()
    record_sig = sign_detached(ISSUER_RECORD_DOMAIN, body, ALIBI, "alibi-demo-1")
    assert verify_detached(ISSUER_RECORD_DOMAIN, body, record_sig, ALIBI.public_key())
    assert not verify_detached(PACKAGE_DOMAIN, body, record_sig, ALIBI.public_key())
    # Same key, same body, record-domain signature placed on a package: rejected.
    assert code_of(reseal_envelope({"body": body, "signature": record_sig}), registry) == "bad_signature"


def test_tampered_issuer_record_blocks_acceptance(registry):
    body = make_body()
    body["attachments"][0]["record"]["body"]["money"]["net"] = 226000
    assert code_of(seal(body), registry) == "source_signature_invalid"


def test_claimed_issuer_trust_requires_signature(registry):
    body = make_body()
    body["attachments"][0]["record"].pop("signature")
    assert code_of(seal(body), registry) == "source_signature_missing"


def test_issuer_key_must_own_the_record_issuer_id(registry):
    forged = sign_record(payout_record(issuer_id="merchant_demo"), OTHER_MERCHANT, "merchant-other-1")
    body = make_body()
    body["attachments"][0]["record"] = forged
    assert code_of(seal(body), registry) == "source_signature_invalid"


def test_issuer_record_signed_with_package_key_rejected(registry):
    body = make_body()
    body["attachments"][0]["record"] = sign_record(payout_record(), ALIBI, "alibi-demo-1")
    assert code_of(seal(body), registry) == "wrong_key_purpose"


def test_failed_signature_on_self_supplied_attachment_still_blocks(registry):
    body = make_body()
    rec = sign_record(payout_record(), MERCHANT, "merchant-demo-1")
    rec["body"]["revision"] = 2
    body["attachments"][1] = {"attachment_id": "att-invoice", "trust": "self_supplied", "record": rec}
    assert code_of(seal(body), registry) == "source_signature_invalid"


def test_valid_signature_never_upgrades_stated_trust(registry):
    body = make_body()
    body["attachments"][1] = {
        "attachment_id": "att-invoice",
        "trust": "self_supplied",
        "record": sign_record(payout_record(), MERCHANT, "merchant-demo-1"),
    }
    res = verify_package(seal(body), recipient="bank_a", registry=registry, now=NOW)
    assert res.attachments[1].trust == "self_supplied" and res.attachments[1].source_signature == "valid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["attachments"][1].pop("trust"),
        lambda b: b["attachments"][1].update(trust="trusted"),
        lambda b: b["claims"][0].update(status="likely"),
        lambda b: b["claims"][0].update(evidence_refs=["att-missing"]),
        lambda b: b.update(expires_at="2026-11-14T10:00:00+00:00"),
        lambda b: b.update(scope={"from": "2026-11-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}),
        lambda b: b["attachments"].append(copy.deepcopy(b["attachments"][1])),
    ],
    ids=["trust-missing", "trust-unknown", "claim-status", "dangling-ref", "non-z-time", "inverted-period", "dup-attachment"],
)
def test_schema_rejections(mutate):
    body = make_body()
    mutate(body)
    with pytest.raises(VerificationError) as exc:
        seal(body)
    assert exc.value.code == "schema"


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("attachments", "trust", {}),
        ("claims", "status", []),
        ("claims", "evidence_refs", [{}]),
    ],
)
def test_signed_record_schema_rejects_nonstring_enums_and_refs(section, field, value):
    body = make_body()
    body[section][0][field] = value
    with pytest.raises(VerificationError, match="schema"):
        seal(body)


def test_signature_envelope_strictness(registry):
    env = json.loads(seal(make_body()))
    bad_alg = copy.deepcopy(env)
    bad_alg["signature"]["algorithm"] = "ES256"
    assert code_of(reseal_envelope(bad_alg), registry) == "unsupported_algorithm"
    extra = copy.deepcopy(env)
    extra["signature"]["trust"] = "issuer_verified"
    assert code_of(reseal_envelope(extra), registry) == "schema"
    padded = copy.deepcopy(env)
    padded["signature"]["signature"] += "=="
    assert code_of(reseal_envelope(padded), registry) == "bad_encoding"
    short = copy.deepcopy(env)
    short["signature"]["signature"] = "A"
    assert code_of(reseal_envelope(short), registry) == "bad_encoding"


def test_registry_pins_one_purpose_per_key_and_round_trips():
    reg = TrustRegistry()
    reg.add("alibi-demo-1", PURPOSE_PACKAGE, "alibi", ALIBI)
    with pytest.raises(ValueError):
        reg.add("alibi-as-merchant", PURPOSE_ISSUER_RECORD, "merchant_demo", ALIBI)
    with pytest.raises(ValueError):
        reg.add("alibi-demo-1", PURPOSE_PACKAGE, "alibi", MERCHANT)
    again = TrustRegistry.from_json(json.loads(json.dumps(reg.to_json())))
    assert again.resolve("alibi-demo-1", PURPOSE_PACKAGE).owner == "alibi"
    assert again.to_json()["keys"][0]["public_key"] == public_key_b64(ALIBI)


def test_master_key_derivation_is_deterministic_and_purpose_bound():
    a = package_signing_key_from_master(b"m" * 32)
    assert public_key_b64(a) == public_key_b64(package_signing_key_from_master(b"m" * 32))
    assert public_key_b64(a) != public_key_b64(package_signing_key_from_master(b"n" * 32))
    with pytest.raises(ValueError):
        package_signing_key_from_master(b"short")


# --------------------------------------------------------------------------- bank inbox


@pytest.fixture
def bank(tmp_path, registry) -> BankInbox:
    return BankInbox(tmp_path / "bank", registry, {"cred-a": "bank_a", "cred-b": "bank_b"})


def test_delivery_receipt_binds_package_id_and_digest(bank):
    raw = seal(make_body())
    d = bank.deliver("bank_a", raw, now=NOW)
    assert d.created
    assert d.receipt.package_id == "pkg-0001" and d.receipt.request_id == "rfc-0001"
    assert d.receipt.digest == verify_package(raw, recipient="bank_a", registry=bank.registry, now=NOW).digest
    stored = bank.get("cred-a", "pkg-0001")
    assert stored.raw == raw and stored.receipt == d.receipt
    assert stored.verification["claims"][1]["status"] == "unresolved"


def test_retry_identical_bytes_returns_existing_receipt_even_after_expiry(bank):
    raw = seal(make_body())
    first = bank.deliver("bank_a", raw, now=NOW)
    later = bank.deliver("bank_a", raw, now=NOW + timedelta(days=30))
    assert not later.created and later.receipt == first.receipt
    assert len(bank.list_receipts("cred-a")) == 1


def test_reused_package_id_with_different_bytes_is_rejected(bank):
    bank.deliver("bank_a", seal(make_body()), now=NOW)
    changed = make_body()
    changed["claims"][1]["rationale"] = "Different text, same package_id."
    with pytest.raises(ConflictError) as exc:
        bank.deliver("bank_a", seal(changed), now=NOW)
    assert exc.value.code == "package_id_conflict"
    assert bank.get("cred-a", "pkg-0001").raw == seal(make_body())
    # Unsigned junk reusing the ID gets a verification error, not a conflict (no existence oracle).
    junk = json.loads(seal(changed))
    junk["body"]["case_id"] = "case-9999"
    with pytest.raises(VerificationError) as exc:
        bank.deliver("bank_a", reseal_envelope(junk), now=NOW)
    assert exc.value.code == "bad_signature"


def test_rejected_package_is_not_stored(bank):
    body = make_body()
    body["attachments"][0]["record"]["body"]["money"]["net"] = 1
    with pytest.raises(VerificationError):
        bank.deliver("bank_a", seal(body), now=NOW)
    assert bank.list_receipts("cred-a") == []
    # A correct package can still use that ID afterwards.
    assert bank.deliver("bank_a", seal(make_body()), now=NOW).created


def test_recipient_isolation(bank):
    bank.deliver("bank_a", seal(make_body()), now=NOW)
    with pytest.raises(KeyError):
        bank.get("cred-b", "pkg-0001")
    assert bank.list_receipts("cred-b") == []
    with pytest.raises(AuthError):
        bank.get("cred-x", "pkg-0001")
    with pytest.raises(VerificationError) as exc:
        bank.deliver("bank_b", seal(make_body()), now=NOW)
    assert exc.value.code == "wrong_recipient"
    with pytest.raises(AuthError):
        bank.deliver("bank_c", seal(make_body()), now=NOW)
    # Each recipient needs its own package; the same evidence for bank_b is a separate package.
    b_raw = seal(make_body(package_id="pkg-0002", request_id="rfc-0002", audience="bank_b"))
    assert bank.deliver("bank_b", b_raw, now=NOW).created
    assert [r.package_id for r in bank.list_receipts("cred-b")] == ["pkg-0002"]


def test_concurrent_identical_retries_create_one_receipt(bank):
    raw = seal(make_body())
    results, errors = [], []

    def go():
        try:
            results.append(bank.deliver("bank_a", raw, now=NOW))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(d.created for d in results) == 1
    assert len({d.receipt for d in results}) == 1


def test_inbox_is_durable_across_instances(tmp_path, registry):
    raw = seal(make_body())
    first = BankInbox(tmp_path / "bank", registry, {"cred-a": "bank_a"}).deliver("bank_a", raw, now=NOW)
    reopened = BankInbox(tmp_path / "bank", registry, {"cred-a": "bank_a"})
    again = reopened.deliver("bank_a", raw, now=NOW)
    assert not again.created and again.receipt == first.receipt
    assert not list((tmp_path / "bank" / "bank_a").glob(".tmp-*"))


# --------------------------------------------------------------------------- wire v1 compatibility
# Mirrors the exact public body the privacy worker emits (schema_version "1"), so a signer or
# verifier change that rejects, reshapes or re-keys it fails here.


def v1_body() -> dict:
    return {
        "schema_version": "1",
        "package_id": "pkg-0123456789abcdef0123",
        "request_id": "rfc-0001",
        "audience": "bank_a",
        "case_id": "rfc-0001",
        "scope": {
            "txn_ids": ["txn-0001-001"],
            "period": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
            "question_ids": ["q.income_in_period", "q.source_of_funds"],
        },
        "issued_at": "2026-11-07T10:00:00Z",
        "expires_at": "2026-11-14T10:00:00Z",
        "claims": [
            {
                "question_id": "q.income_in_period",
                "status": "supported",
                "rationale": "The credit matches the merchant payout.",
                "evidence_refs": [{"ref": "rec-0001-004", "revision": 2}, {"ref": "txn-0001-001", "revision": 1}],
                "calculation": {"terms": [{"ref": "rec-0001-004", "amount_minor": 216000}], "total_minor": 216000, "currency": "GBP"},
                "gap": None,
            },
            {
                "question_id": "q.source_of_funds",
                "status": "unresolved",
                "rationale": "No shareable answer was prepared for this question.",
                "evidence_refs": [],
                "calculation": None,
                "gap": "No shareable evidence answers this question.",
            },
        ],
        "attachments": [
            {
                "ref": "rec-0001-004", "revision": 2, "kind": "payout", "issuer_id": "merchant_demo",
                "trust": "issuer_verified", "presentation": "redacted_extract",
                "fields": {"occurred_at": "2026-10-02", "amount_minor": 216000, "currency": "GBP", "status": "paid"},
            },
            {
                "ref": "txn-0001-001", "revision": 1, "kind": "bank_txn", "issuer_id": None,
                "trust": "bank_observed", "presentation": "full_record",
                "fields": {"booked_at": "2026-10-03T08:00:00Z", "amount_minor": 216000, "currency": "GBP"},
            },
        ],
    }


def seal_v1(body: dict) -> bytes:  # privacy worker digest: bare lowercase hex of the JCS body
    return sign_package(body, ALIBI, "alibi-demo-1", approved_body_digest=hashlib.sha256(rfc8785.dumps(body)).hexdigest())


def test_v1_wire_body_signed_untransformed_and_verified(bank):
    body = v1_body()
    raw = seal_v1(body)
    env = json.loads(raw)
    assert env["body"] == body and rfc8785.dumps(env["body"]) == rfc8785.dumps(body)  # no keys added or reshaped
    assert sign_package(body, ALIBI, "alibi-demo-1", approved_body_digest=body_digest(body)) == raw  # both digest forms

    res = verify_package(raw, recipient="bank_a", registry=bank.registry, now=NOW)
    assert res.schema_version == V1_SCHEMA_VERSION and res.package_signature == "valid"
    payout, txn = res.attachments
    assert (payout.attachment_id, payout.revision, payout.trust, payout.presentation) == (
        "rec-0001-004", 2, "issuer_verified", "redacted_extract")
    assert payout.source_signature == "absent" and payout.attested_by == "package_signer"
    assert txn.trust == "bank_observed" and txn.attested_by == "package_signer"
    assert [c["status"] for c in res.claims] == ["supported", "unresolved"]
    assert bank.deliver("bank_a", raw, now=NOW).created


def test_v1_full_record_issuer_verified_fails_closed_without_valid_proof(registry):
    body = v1_body()
    body["attachments"][0]["presentation"] = "full_record"
    raw = seal_v1(body)
    assert code_of(raw, registry) == "source_signature_missing"

    signed = payout_record(record_id="rec-0001-004", revision=2, occurred_at="2026-10-02",
                           amount_minor=216000, currency="GBP", status="paid")
    proof = sign_record(signed, MERCHANT, "merchant-demo-1")
    res = verify_package(raw, recipient="bank_a", registry=registry, now=NOW, issuer_records=[proof])
    assert (res.attachments[0].source_signature, res.attachments[0].attested_by) == ("valid", "issuer")

    wrong_value = sign_record({**signed, "amount_minor": 226000}, MERCHANT, "merchant-demo-1")
    wrong_revision = sign_record({**signed, "revision": 1}, MERCHANT, "merchant-demo-1")
    forged = copy.deepcopy(proof)
    forged["body"]["status"] = "pending"
    for bad, code in ((wrong_value, "source_signature_invalid"), (forged, "source_signature_invalid"),
                      (wrong_revision, "source_signature_missing")):
        with pytest.raises(VerificationError) as exc:
            verify_package(raw, recipient="bank_a", registry=registry, now=NOW, issuer_records=[bad])
        assert exc.value.code == code


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda b: b["claims"][0]["evidence_refs"][0].update(revision=1), "reference"),  # stale revision
        (lambda b: b["claims"][0]["evidence_refs"].append({"ref": "rec-9999", "revision": 1}), "reference"),
        (lambda b: b["claims"][0]["calculation"]["terms"].append({"ref": "rec-9999", "amount_minor": 1}), "reference"),
        (lambda b: b["claims"][0].update(status="supported", evidence_refs=[], calculation=None), "reference"),
        (lambda b: b["claims"][0]["evidence_refs"][0].update(note="x"), "schema"),
        (lambda b: b["attachments"][0].update(signature={}), "schema"),  # no keys beyond the v1 wire shape
        (lambda b: b["attachments"][0].update(revision=True), "schema"),
        (lambda b: b["attachments"][0].update(issuer_id=None), "schema"),  # issuer_verified needs an issuer
        (lambda b: b["attachments"][0].update(presentation="summary"), "schema"),
        (lambda b: b["attachments"][0].update(trust=["issuer_verified"]), "schema"),
        (lambda b: b["scope"].pop("question_ids"), "schema"),
        (lambda b: b["scope"].update(period={"from": "2026-11-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}), "schema"),
        (lambda b: b["scope"].update(period=None, txn_ids=[]), "schema"),
        (lambda b: b["claims"][1].update(question_id="q.breakdown"), "schema"),  # not a requested question
        (lambda b: b["claims"][0]["calculation"].update(total_minor=216001), "schema"),
    ],
)
def test_v1_reference_scope_and_provenance_rejections(mutate, code):
    body = v1_body()
    mutate(body)
    with pytest.raises(VerificationError) as exc:
        seal_v1(body)
    assert exc.value.code == code
