from hashlib import sha256
import json

import pytest

from agents.eval.trusted_setup import load


@pytest.fixture
def setup_file(tmp_path):
    path = tmp_path / 'trusted.json'
    value = dict(schema_version='alibi.eval.synthetic_issuer_setup.v1',
                 trusted_issuers={'fixture': 'public-fixture'}, synthetic_key_seed='a' * 64,
                 public_key_fingerprints={'fixture': 'b' * 64},
                 provenance={'private_source': 'PRIVATE_SENTINEL'})
    path.write_text(json.dumps(value))
    return path, value


def test_trusted_setup_strips_provenance_and_binds_bytes(setup_file):
    path, value = setup_file
    digest = sha256(path.read_bytes()).hexdigest()
    result = load(path, digest)
    assert set(result) == {'trusted_issuers', 'synthetic_key_seed', 'public_key_fingerprints'}
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    path.write_text(json.dumps({**value, 'synthetic_key_seed': 'c' * 64}))
    with pytest.raises(ValueError, match='commitment changed'):
        load(path, digest)


@pytest.mark.parametrize('change', [
    {'trusted_issuers': {}}, {'trusted_issuers': {'../../escape': 'issuer'}},
    {'synthetic_key_seed': 'PRIVATE_SENTINEL'}, {'public_key_fingerprints': {}},
])
def test_invalid_trust_fails_without_echoing_values(setup_file, change):
    path, value = setup_file
    path.write_text(json.dumps({**value, **change}))
    with pytest.raises(ValueError) as error:
        load(path, sha256(path.read_bytes()).hexdigest())
    assert 'PRIVATE_SENTINEL' not in str(error.value)
