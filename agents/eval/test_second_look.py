import json
from types import SimpleNamespace

import pytest

from agents.eval import second_look
from agents.eval.postfix_runtime import files


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


def test_replay_never_launches_and_refuses_changed_artifact(tmp_path, monkeypatch):
    (tmp_path / 'metrics.json').write_text('{"cases": 44}')
    (tmp_path / 'receipt.json').write_text(json.dumps({'files': files(tmp_path)}))
    monkeypatch.setattr(second_look, '_parallel_capture', lambda *a: pytest.fail('launched worker'))
    second_look.main(['--out', str(tmp_path)])
    (tmp_path / 'metrics.json').write_text('{}')
    with pytest.raises(ValueError, match='evidence changed'):
        second_look.main(['--out', str(tmp_path)])


def test_incomplete_run_is_preserved_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(second_look, '_parallel_capture', lambda *a: pytest.fail('retried'))
    with pytest.raises(second_look.common.EvalError, match='missing file'):
        second_look.main(['--out', str(tmp_path)])


def test_new_capture_requires_recorded_authority_before_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(second_look, 'is_open', lambda: False)
    with pytest.raises(SystemExit):
        second_look.main(['--out', str(tmp_path / 'new')])
    assert not (tmp_path / 'new').exists()
