"""Check the independent CLI against actual packages produced by S1."""

from datetime import datetime, timezone
import json

import pytest
from cryptography.fernet import Fernet

from agents.eval.verify_package import main
from alibi import api
from alibi.core.canon import digest
from alibi.core.signing import SigningDomain


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def exports(tmp_path):
    keystore = api.default_keystore(tmp_path / "keys")
    packages = {}
    with api.open_store(tmp_path / "passport.db", master_key=Fernet.generate_key()) as store:
        fixture = api.load_fixture(store, "supported", keystore=keystore, now=NOW)
        for request, bank in (("rfc_a_001", "bank_a"), ("rfc_b_001", "bank_b")):
            api.run_investigation(store, request, settlement_lookup=fixture["settlement_lookup"],
                                  registry=keystore.registry(), now=NOW)
            draft = api.build_review(store, request, now=NOW)
            approval = api.approve(store, draft.draft_id, holder_id="holder_maya", now=NOW)
            package = api.sign_and_package(store, approval.approval_id, keystore=keystore, now=NOW)
            path = tmp_path / f"{bank}.json"
            path.write_text(package.model_dump_json(exclude_none=True), encoding="utf-8")
            packages[bank] = path
    return packages, keystore


def verify(path, keys, bank, *, when=NOW.isoformat(), options=()):
    return main([str(path), "--registry", str(keys.registry_path), "--recipient", bank, "--at", when, *options])


@pytest.mark.parametrize(("bank", "request_id"), [("bank_a", "rfc_a_001"), ("bank_b", "rfc_b_001")])
def test_native_exports_verify_independently(exports, capsys, bank, request_id):
    packages, keys = exports
    assert verify(packages[bank], keys, bank, options=("--request-id", request_id)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "verified" and result["package_signature"] == "valid"
    assert result["request_id"] == request_id
    assert result["claims"] and result["attachments"]
    for attachment in result["attachments"]:
        if attachment["redacted"]:
            assert attachment["attestation"] == "package_signer"
            assert attachment["source_signature"] == "absent"
        else:
            assert attachment["attestation"] == "issuer_signature"


def test_signed_export_refuses_wrong_recipient_request_and_exact_expiry(exports, capsys):
    packages, keys = exports
    path = packages["bank_a"]
    expiry = json.loads(path.read_text())["body"]["expires_at"]
    assert verify(path, keys, "bank_b") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "wrong_recipient"
    assert verify(path, keys, "bank_a", options=("--request-id", "other-rfc")) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "wrong_request"
    assert verify(path, keys, "bank_a", when=expiry) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "expired"


def test_native_tamper_and_duplicate_keys_are_refused(exports, capsys):
    packages, keys = exports
    path = packages["bank_a"]
    original = path.read_text()
    modified = original.replace('"audience":"bank_a"', '"audience":"bank_b"', 1)
    assert modified != original
    path.write_text(modified)
    assert verify(path, keys, "bank_b") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "bad_signature"
    path.write_text(original.replace('"audience":"bank_a"', '"audience":"bank_b","audience":"bank_a"', 1))
    assert verify(path, keys, "bank_a") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "malformed"


def test_valid_package_signature_cannot_mask_a_bad_embedded_issuer_signature(exports, capsys):
    packages, keys = exports
    path = packages["bank_b"]
    package = json.loads(path.read_text())
    source = next(row for row in package["body"]["attachments"] if row["form"] == "full_record")
    source["issuer_signature"]["sig"] = "0" * 128
    package.pop("signature")
    package["body_sha256"] = digest(package["body"])
    package["signature"] = keys.sign(package, kid="alibi_demo", domain=SigningDomain.PACKAGE).to_json_dict()
    path.write_text(json.dumps(package))
    assert verify(path, keys, "bank_b") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "source_signature_invalid"


def test_native_unsigned_full_holder_record_remains_lower_trust(exports, capsys):
    packages, keys = exports
    path = packages["bank_a"]
    package = json.loads(path.read_text())
    package["body"]["attachments"].append({
        "attachment_id": "att-holder-receipt",
        "ref": {"ref_type": "record", "ref_id": "holder-receipt", "revision": 1},
        "form": "full_record", "source_trust": "self_supplied",
        "content": {"record_id": "holder-receipt", "revision": 1, "description": "Holder's receipt"},
    })
    package.pop("signature")
    package["body_sha256"] = digest(package["body"])
    package["signature"] = keys.sign(package, kid="alibi_demo", domain=SigningDomain.PACKAGE).to_json_dict()
    assert api.verify_package(package, registry=keys.registry(), expected_audience="bank_a", now=NOW).accepted
    path.write_text(json.dumps(package))

    assert verify(path, keys, "bank_a") == 0
    result = json.loads(capsys.readouterr().out)
    holder_record = next(row for row in result["attachments"] if row["attachment_id"] == "att-holder-receipt")
    assert holder_record["trust"] == "self_supplied"
    assert holder_record["source_signature"] == "absent"
    assert holder_record["attestation"] == "package_signer"
    assert not holder_record["redacted"]


def test_wrong_key_purpose_and_unverifiable_subject_are_refused(exports, capsys):
    packages, keys = exports
    path = packages["bank_a"]
    assert verify(path, keys, "bank_a", options=("--subject", "unrelated_subject")) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "subject_unverifiable"
    registry = json.loads(keys.registry_path.read_text())
    registry["alibi_demo"]["purpose"] = "record_signing"
    keys.registry_path.write_text(json.dumps(registry))
    assert verify(path, keys, "bank_a") == 1
    assert json.loads(capsys.readouterr().err)["code"] == "wrong_key_purpose"
