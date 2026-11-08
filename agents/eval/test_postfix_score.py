import json

import pytest

from agents.data.api import load_case, without_gold
from agents.eval.postfix_score import score
from agents.eval.sealed_worker import failure_rows


@pytest.fixture
def public_capture(tmp_path):
    cases = [load_case(1), load_case(2)]
    source = tmp_path / 'cases'
    source.mkdir()
    for case, filename in zip(cases, ('main.jsonl', 'adversarial.jsonl')):
        (source / filename).write_text(json.dumps(without_gold(case)) + '\n')
    gold = tmp_path / 'gold.json'
    gold.write_text(json.dumps({str(c['case_number']): c['gold'] for c in cases}))
    capture = tmp_path / 'capture'
    for case in cases:
        folder = capture / 'workers' / str(case['case_number'])
        folder.mkdir(parents=True)
        (folder / 'predictions.json').write_text(json.dumps(failure_rows(case, 'rules', 'error', 'synthetic failure')))
    return capture, source, gold


def test_failures_remain_in_both_denominators(public_capture):
    result = score(*public_capture)
    assert set(result) == {'main', 'adversarial'}
    for metrics in result.values():
        assert metrics['safe_complete_rate'] == 0
        assert metrics['cases'] == 1


def test_duplicate_prediction_is_rejected(public_capture):
    capture, source, gold = public_capture
    path = next((capture / 'workers').glob('*/predictions.json'))
    rows = json.loads(path.read_text())
    path.write_text(json.dumps(rows + [rows[0]]))
    with pytest.raises(ValueError, match='denominator'):
        score(capture, source, gold)


@pytest.mark.parametrize('mode', ['agents', 'one_pass', 'rules'])
def test_nested_development_modes_keep_main_and_adversarial_denominators(public_capture, mode):
    capture, source, gold = public_capture
    folders = list((capture / 'workers').iterdir())
    for folder in folders:
        target = capture / 'workers' / mode / folder.name
        target.mkdir(parents=True)
        (target / 'predictions.json').write_bytes((folder / 'predictions.json').read_bytes())
    result = score(capture, source, gold, mode=mode, nested=True)
    assert all(metrics['cases'] == 1 and metrics['safe_complete'] == 0
               for metrics in result.values())
    next((capture / 'workers' / mode).glob('*/predictions.json')).unlink()
    with pytest.raises(ValueError, match='denominator'):
        score(capture, source, gold, mode=mode, nested=True)
