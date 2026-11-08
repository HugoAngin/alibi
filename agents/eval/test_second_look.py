import json
from types import SimpleNamespace

import pytest

from agents.eval import second_look


@pytest.fixture
def public_gate(tmp_path, monkeypatch):
    workers = [dict(system=m, case_number=n, candidate='fixed', failures=[],
                    isolation_preflight='passed')
               for m in second_look.MODES for n in (1, 2)]
    (tmp_path / 'run.json').write_text(json.dumps(dict(candidate='fixed', workers=workers)))
    monkeypatch.setattr(second_look, 'load_case', lambda n: {'rfcs': [{'request_id': str(n)}]})
    monkeypatch.setattr(second_look.scorer, 'load_predictions',
                        lambda *a: SimpleNamespace(invalid={}, predictions={(1, '1'): {}, (2, '2'): {}}))
    return tmp_path


def test_public_gate_requires_exact_six_successful_workers(public_gate):
    second_look.public_gate(public_gate, 'fixed')
    path = public_gate / 'run.json'
    run = json.loads(path.read_text())
    run['workers'][0]['failures'] = ['timeout']
    path.write_text(json.dumps(run))
    with pytest.raises(ValueError, match='six isolated workers'):
        second_look.public_gate(public_gate, 'fixed')


def test_public_gate_rejects_wrong_candidate_and_invalid_predictions(public_gate, monkeypatch):
    with pytest.raises(ValueError, match='exact candidate'):
        second_look.public_gate(public_gate, 'other')
    monkeypatch.setattr(second_look.scorer, 'load_predictions',
                        lambda *a: SimpleNamespace(invalid={(1, '1'): 'schema'}, predictions={}))
    with pytest.raises(ValueError, match='schema or denominator'):
        second_look.public_gate(public_gate, 'fixed')


def test_public_gate_binds_identical_trusted_setup(public_gate):
    with pytest.raises(ValueError, match='six isolated workers'):
        second_look.public_gate(public_gate, 'fixed', trusted_issuers_sha256='expected')
    path = public_gate / 'run.json'
    run = json.loads(path.read_text())
    for worker in run['workers']:
        worker.update(trusted_setup_sha256='expected', trusted_setup_verified=True)
    path.write_text(json.dumps(run))
    second_look.public_gate(public_gate, 'fixed', trusted_issuers_sha256='expected')
    run['workers'][0]['trusted_setup_verified'] = False
    path.write_text(json.dumps(run))
    with pytest.raises(ValueError, match='six isolated workers'):
        second_look.public_gate(public_gate, 'fixed', trusted_issuers_sha256='expected')
