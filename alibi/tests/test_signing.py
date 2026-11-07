import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alibi.core.schemas import KeyPurpose, SigningDomain
from alibi.core.signing import (
    DEMO_KEYS,
    DemoKeystore,
    SignatureError,
    TrustRegistry,
    load_registry,
    sign,
    verify,
)

RECORD = {"record_id": "po_5001", "revision": 1, "amount_pence": 216000, "currency": "GBP"}


def test_sign_and_verify_round_trip(keystore, registry):
    env = keystore.sign(RECORD, kid="merchant_demo", domain=SigningDomain.RECORD)
    assert env.alg == "Ed25519" and env.kid == "merchant_demo" and len(env.sig) == 128
    entry = verify(RECORD, env, registry, domain=SigningDomain.RECORD, issuer_id="threadline")
    assert entry.purpose == KeyPurpose.RECORD_SIGNING
    # Key order and formatting do not matter: the signed bytes are canonical JSON.
    verify(dict(reversed(list(RECORD.items()))), env.to_json_dict(), registry, domain="alibi:record:v1")


def test_tampered_payload_fails(keystore, registry):
    env = keystore.sign(RECORD, kid="merchant_demo", domain=SigningDomain.RECORD)
    with pytest.raises(SignatureError, match="does not verify"):
        verify({**RECORD, "amount_pence": 216001}, env, registry, domain=SigningDomain.RECORD)


def test_wrong_domain_fails(keystore, registry):
    env = keystore.sign(RECORD, kid="merchant_demo", domain=SigningDomain.RECORD)
    with pytest.raises(SignatureError, match="domain"):
        verify(RECORD, env, registry, domain=SigningDomain.PACKAGE)
    # Rewriting the envelope's domain cannot help: the purpose check and the bytes both fail.
    forged = env.model_copy(update={"domain": SigningDomain.PACKAGE})
    with pytest.raises(SignatureError):
        verify(RECORD, forged, registry, domain=SigningDomain.PACKAGE)


def test_wrong_purpose_key_fails(keystore, registry):
    # A merchant key used to sign in the package domain is refused by the keystore...
    with pytest.raises(SignatureError, match="may not sign"):
        keystore.sign(RECORD, kid="merchant_demo", domain=SigningDomain.PACKAGE)
    # ...and a raw signature made anyway is rejected by the pinned purpose.
    env = sign(RECORD, private_key=keystore.private_key("merchant_demo"), kid="merchant_demo",
               domain=SigningDomain.PACKAGE)
    with pytest.raises(SignatureError, match="pinned for record_signing"):
        verify(RECORD, env, registry, domain=SigningDomain.PACKAGE)


def test_wrong_issuer_fails(keystore, registry):
    env = keystore.sign(RECORD, kid="merchant_demo", domain=SigningDomain.RECORD)
    with pytest.raises(SignatureError, match="does not belong"):
        verify(RECORD, env, registry, domain=SigningDomain.RECORD, issuer_id="other_market")


def test_unknown_kid_and_foreign_key_fail(registry):
    stranger = Ed25519PrivateKey.generate()
    env = sign(RECORD, private_key=stranger, kid="mallory", domain=SigningDomain.RECORD)
    with pytest.raises(SignatureError, match="not in the trust registry"):
        verify(RECORD, env, registry, domain=SigningDomain.RECORD)
    # A foreign key claiming a pinned kid fails the signature check.
    env = sign(RECORD, private_key=stranger, kid="merchant_demo", domain=SigningDomain.RECORD)
    with pytest.raises(SignatureError, match="does not verify"):
        verify(RECORD, env, registry, domain=SigningDomain.RECORD)


def test_malformed_envelope_fails(registry):
    with pytest.raises(SignatureError, match="malformed"):
        verify(RECORD, {"alg": "RS256", "kid": "merchant_demo", "domain": "alibi:record:v1",
                        "sig": "00" * 64}, registry, domain=SigningDomain.RECORD)


def test_keystore_persists_keys_and_registry(tmp_path):
    ks = DemoKeystore(tmp_path / "keys").ensure()
    assert sorted(p.name for p in (tmp_path / "keys").iterdir()) == sorted(
        [f"{kid}.pem" for kid in DEMO_KEYS] + ["trust_registry.json"])
    again = DemoKeystore(tmp_path / "keys")
    assert again.public_key_hex("alibi_demo") == ks.public_key_hex("alibi_demo")
    loaded = load_registry(tmp_path / "keys" / "trust_registry.json")
    assert loaded.to_json() == ks.registry().to_json()
    assert "PRIVATE" not in (tmp_path / "keys" / "trust_registry.json").read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_private_key_files_are_0600(tmp_path):
    DemoKeystore(tmp_path / "keys").ensure()
    for kid in DEMO_KEYS:
        mode = stat.S_IMODE((tmp_path / "keys" / f"{kid}.pem").stat().st_mode)
        assert mode == 0o600


def test_random_keys_differ_and_seeded_keys_repeat(tmp_path):
    a = DemoKeystore(tmp_path / "a").ensure()
    b = DemoKeystore(tmp_path / "b").ensure()
    assert a.public_key_hex("merchant_demo") != b.public_key_hex("merchant_demo")
    s1 = DemoKeystore(tmp_path / "s1", seed="test-seed")
    s2 = DemoKeystore(tmp_path / "s2", seed="test-seed")
    assert s1.public_key_hex("bank_a") == s2.public_key_hex("bank_a")
    assert s1.public_key_hex("bank_a") != s1.public_key_hex("bank_b")


def test_registry_rejects_bad_entries_and_conflicting_pins(registry):
    with pytest.raises(SignatureError):
        TrustRegistry.from_json({"k": {"public_key": "zz", "purpose": "record_signing", "issuer_id": "x"}})
    with pytest.raises(SignatureError):
        TrustRegistry.from_json({"k": {"public_key": "00" * 32, "purpose": "root", "issuer_id": "x"}})
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    with pytest.raises(SignatureError, match="already pinned"):
        registry.add("merchant_demo", other, KeyPurpose.RECORD_SIGNING, "threadline")
    round_trip = TrustRegistry.from_json(registry.to_json())
    assert round_trip.to_json() == registry.to_json()
