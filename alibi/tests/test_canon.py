import pytest

from alibi.core.canon import CanonError, canonical_bytes, digest, make_id, parse_json, to_plain
from alibi.core.schemas import EvidenceRef, Period


def test_jcs_sorts_keys_and_strips_whitespace():
    assert canonical_bytes({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'


def test_digest_ignores_key_order_and_formatting():
    assert digest(parse_json('{"a": 1, "b": {"d": 2, "c": 3}}')) == \
        digest(parse_json('{"b":{"c":3,"d":2},"a":1}'))
    assert len(digest({"a": 1})) == 64


def test_models_canonicalise_like_their_json_dict():
    period = Period.model_validate({"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    assert canonical_bytes(period) == canonical_bytes(period.to_json_dict())
    nested = {"refs": [EvidenceRef(ref_type="record", ref_id="po_5001", revision=2)]}
    assert to_plain(nested) == {"refs": [{"ref_type": "record", "ref_id": "po_5001", "revision": 2}]}
    assert canonical_bytes(nested) == b'{"refs":[{"ref_id":"po_5001","ref_type":"record","revision":2}]}'


def test_non_canonicalisable_values_raise():
    with pytest.raises(CanonError):
        canonical_bytes({"a": 2 ** 60})  # outside the I-JSON safe integer range
    with pytest.raises(CanonError):
        canonical_bytes({"a": object()})


def test_parse_json_rejects_duplicate_keys_and_constants():
    with pytest.raises(CanonError):
        parse_json('{"amount_pence": 1, "amount_pence": 999}')
    with pytest.raises(CanonError):
        parse_json('{"a": NaN}')
    with pytest.raises(CanonError):
        parse_json('{"a": ')


def test_make_id_is_deterministic_and_prefixed():
    assert make_id("clm", "run1", "q1") == make_id("clm", "run1", "q1")
    assert make_id("clm", "run1", "q1") != make_id("clm", "run1", "q2")
    assert make_id("clm", "run1").startswith("clm_")
