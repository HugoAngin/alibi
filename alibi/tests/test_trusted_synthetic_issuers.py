"""Public synthetic controls for explicitly configured mock-service issuer pins.

No private pack, answer key, model, environment credential or capture is used.
"""

import copy

import pytest

from agents.data.api import load_case, without_gold
from alibi.adapter import _World, run_case, run_cases
from alibi.connectors import keys
from alibi.connectors.case_source import CaseClock, map_merchant_event
from alibi.connectors.clients import ClientError
from alibi.connectors.plug import webhook_sink
from alibi.core.schemas import KeyPurpose, SigningDomain
from alibi.core.signing import DemoKeystore, SignatureError, TrustRegistry
from alibi.core.store import Store


ISSUER = "public-synthetic-config-control"
KID = "public_synthetic_config_control"
TRUSTED = {KID: ISSUER}


def public_case(issuer=None):
    case = without_gold(load_case(1))
    if issuer is not None:
        for group in ("merchant_deliveries", "merchant_lookup"):
            for entry in case[group]:
                entry["event"]["issuer_id"] = issuer
    return case


def material(rows):
    return [{"request_id": row["request_id"], "failure": row["failure"],
             "links": row["links"], "lookups": row["retrieval"]["lookups"],
             "claims": {qid: {key: claim[key] for key in
                               ("status", "gap", "facts", "evidence_refs")}
                        for qid, claim in row["claims"].items()}}
            for row in rows]


@pytest.fixture(scope="module")
def public_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("trusted-synthetic-issuers")
    renamed = public_case(ISSUER)
    # A field supplied by the case must not grant trust.
    renamed["trusted_issuers"] = TRUSTED
    before = copy.deepcopy(renamed)
    runs = {
        "default": run_case(public_case(), "rules", root=root / "default"),
        "empty": run_case(public_case(), "rules", root=root / "empty", trusted_issuers={}),
        "unknown": run_case(renamed, "rules", root=root / "unknown"),
        "configured": run_case(renamed, "rules", root=root / "configured", trusted_issuers=TRUSTED),
    }
    assert renamed == before
    return runs


def test_explicit_setup_restores_public_reference_behavior(public_runs):
    assert material(public_runs["configured"]) == material(public_runs["default"])
    assert sum(c["status"] == "supported" for p in public_runs["configured"]
               for c in p["claims"].values()) == 3
    assert all(p["cost"]["model_calls"] == 0 for p in public_runs["configured"])


def test_unknown_issuer_does_not_gain_trust_from_case_fields(public_runs):
    rows = public_runs["unknown"]
    assert len(rows) == 2
    assert all(p["failure"] is None for p in rows)
    assert all(c["status"] != "supported" for p in rows for c in p["claims"].values())
    assert not any(ref.startswith("merchant:") for p in rows for c in p["claims"].values()
                   for ref in c["evidence_refs"])


def test_default_public_behavior_is_unchanged(public_runs):
    assert material(public_runs["default"]) == material(public_runs["empty"])


def test_unknown_pushes_and_lookup_are_refused_without_aborting_bootstrap(tmp_path):
    case = public_case(ISSUER)
    world = _World(case, tmp_path)
    try:
        clock = CaseClock(case, case["rfcs"][0]["issued_at"])
        world.seed(clock)
        assert world.store.list_sources(case["subject_ref"]) == []
        deliveries = world.merchant.deliveries()
        assert deliveries and all(d["outcome"] == "refused" for d in deliveries)
        with pytest.raises(ClientError) as err:
            world.merchant.lookup(record_id=case["merchant_deliveries"][0]["event"]["record_id"])
        assert err.value.code == "untrusted_issuer"
        assert err.value.status == 422
    finally:
        world.close()


def test_configuration_is_copied_and_does_not_change_global_defaults(tmp_path):
    original = dict(keys.CONNECTOR_KEYS)
    configured = dict(TRUSTED)
    keystore = keys.connector_keystore(tmp_path / "keys", trusted_issuers=configured)
    configured[KID] = "changed-after-setup"
    assert keystore.registry().get(KID).issuer_id == ISSUER
    assert keys.CONNECTOR_KEYS == original
    assert KID not in keys.connector_keystore(tmp_path / "other").specs


@pytest.mark.parametrize("configured", [
    {"merchant_mock": ISSUER}, {"bank_a": ISSUER}, {"alibi_demo": ISSUER},
    {"new_kid": keys.CASE_ISSUER}, {"first": ISSUER, "second": ISSUER},
    {"../escape": ISSUER}, {"/absolute": ISSUER}, {"a/b": ISSUER},
    {"safe": ""}, {"safe": " bad "}, {"safe": "bad\nissuer"},
    {"safe": (KeyPurpose.PACKAGE_SIGNING, ISSUER)}, [ISSUER],
])
def test_invalid_configuration_is_rejected_before_creating_keys(tmp_path, configured):
    with pytest.raises(ValueError):
        keys.connector_keystore(tmp_path / "keys", trusted_issuers=configured)
    assert not (tmp_path / "keys").exists()


def test_signing_refuses_unknown_issuer_wrong_pin_and_wrong_purpose(tmp_path):
    body = map_merchant_event(public_case(ISSUER)["merchant_deliveries"][0]["event"]).body
    default = keys.connector_keystore(tmp_path / "default")
    configured = keys.connector_keystore(tmp_path / "configured", trusted_issuers=TRUSTED)
    wrong_purpose = DemoKeystore(tmp_path / "wrong", specs={KID: (KeyPurpose.PACKAGE_SIGNING, ISSUER)})
    with pytest.raises(SignatureError):
        keys.sign_merchant_event(body, default)
    with pytest.raises(SignatureError):
        keys.sign_merchant_event(body, configured, kid=keys.MERCHANT_KID)
    with pytest.raises(SignatureError):
        keys.sign_merchant_event(body, wrong_purpose, kid=KID)


def test_ingest_still_rejects_wrong_issuer_and_wrong_signature_domain(tmp_path):
    case = public_case(ISSUER)
    entry = case["merchant_deliveries"][0]
    body = map_merchant_event(entry["event"]).body
    keystore = keys.connector_keystore(tmp_path / "keys", trusted_issuers=TRUSTED)
    with Store(tmp_path / "passport.db") as store:
        sink = webhook_sink(store, keystore.registry(), subject_ref=case["subject_ref"])
        wrong_pin = keystore.sign(body, kid=keys.MERCHANT_KID, domain=SigningDomain.RECORD)
        wrong_domain = keystore.sign(body, kid="alibi_demo", domain=SigningDomain.PACKAGE)
        for signature in (wrong_pin, wrong_domain):
            outcome = sink({"event": body, "signature": signature.to_json_dict()}, entry["received_at"])
            assert outcome["status"] == "refused"
        assert store.list_sources(case["subject_ref"]) == []
        payload = keys.sign_merchant_event(body, keystore)
        assert payload["signature"]["kid"] == KID
        assert sink(payload, entry["received_at"])["status"] == "inserted"


def test_run_cases_passes_explicit_configuration(tmp_path):
    # This public test seed is deliberately not a real harness secret.
    seed = "public-test-only-synthetic-key-seed"
    summary = run_cases([public_case(ISSUER)], "rules", out=tmp_path / "out",
                        root=tmp_path / "stores", trusted_issuers=TRUSTED,
                        synthetic_key_seed=seed, log=lambda _: None)
    assert summary["requests"] == 2 and summary["failures"] == 0
    import json
    rows = [json.loads(line) for line in (tmp_path / "out" / "rules.jsonl").read_text().splitlines()][1:]
    assert sum(c["status"] == "supported" for p in rows for c in p["claims"].values()) == 3
    expected = keys.connector_keystore(tmp_path / "expected", seed=seed, trusted_issuers=TRUSTED).registry()
    actual = TrustRegistry.load(tmp_path / "stores/case_1/keys/trust_registry.json")
    assert actual.to_json() == expected.to_json()


def test_trusted_seed_binds_the_same_registry_in_fresh_worlds(tmp_path):
    seed = "public-test-only-synthetic-key-seed"
    first = _World(public_case(ISSUER), tmp_path / "first", trusted_issuers=TRUSTED,
                   synthetic_key_seed=seed)
    second = _World(public_case(ISSUER), tmp_path / "second", trusted_issuers=TRUSTED,
                    synthetic_key_seed=seed)
    other = _World(public_case(ISSUER), tmp_path / "other", trusted_issuers=TRUSTED,
                   synthetic_key_seed="different-public-test-seed")
    try:
        assert first.registry.to_json() == second.registry.to_json()
        assert first.registry.get(KID).public_key != other.registry.get(KID).public_key
    finally:
        for world in (first, second, other):
            world.close()


def test_case_cannot_supply_the_synthetic_key_seed(tmp_path):
    case = public_case()
    case["synthetic_key_seed"] = "untrusted-case-value"
    world = _World(case, tmp_path)
    try:
        assert world.keystore.seed is None
    finally:
        world.close()


@pytest.mark.parametrize("seed", ["", 123, {}])
def test_invalid_trusted_seed_is_rejected(tmp_path, seed):
    with pytest.raises(ValueError, match="synthetic_key_seed"):
        _World(public_case(), tmp_path, trusted_issuers=TRUSTED, synthetic_key_seed=seed)
