import json
from pathlib import Path
import subprocess
import sys

import pytest

from agents.data.api import load_case
from agents.eval.isolated_candidate import mapped_payload, normalize_cost, sandbox


def test_mapping_does_not_mutate_input_or_pass_gold():
    case = load_case(1)
    case['privacy_policy'] = [{'rule_id': 'deny_amount', 'type': 'deny', 'fields': ['amount_pence']}]
    payload = mapped_payload(case)
    assert payload['holder_policy'] == case['privacy_policy']
    assert 'privacy_policy' not in payload
    assert 'gold' not in payload and 'family' not in payload
    assert 'gold' in case and 'privacy_policy' in case


def test_conflicting_or_nested_gold_policy_rejected():
    case = load_case(1)
    case['privacy_policy'], case['holder_policy'] = [], ['different']
    with pytest.raises(ValueError, match='conflicting'):
        mapped_payload(case)
    del case['privacy_policy']
    case['holder_policy'] = {'gold': 'must not be passed'}
    with pytest.raises(ValueError, match='nested gold'):
        mapped_payload(case)


def test_unknown_model_cost_not_presented_as_zero():
    raw = [{'cost': {'cost_usd_estimate': 0.0, 'tokens': 124, 'model_calls': 2}}]
    fixed = normalize_cost(raw, 'agents')
    assert 'cost_usd_estimate' not in fixed[0]['cost']
    assert fixed[0]['cost']['tokens'] == 124
    assert raw[0]['cost']['cost_usd_estimate'] == 0.0
    assert normalize_cost(raw, 'rules')[0]['cost']['cost_usd_estimate'] == 0.0


def test_real_sandbox_blocks_private_read_and_checkout_write(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'gold').write_text('synthetic test sentinel')
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    allowed = tmp_path / 'scratch'
    allowed.mkdir()
    program = '''import pathlib,sys
private,checkout,scratch=map(pathlib.Path,sys.argv[1:])
for action in [lambda:(private/'gold').read_text(),lambda:(checkout/'replay').write_text('x')]:
    try: action()
    except PermissionError: pass
    else: raise RuntimeError('sandbox allowed protected action')
(scratch/'replay').write_text('allowed')
'''
    subprocess.run([*sandbox((private,), (checkout,)), sys.executable, '-c', program,
                    str(private), str(checkout), str(allowed)], check=True, capture_output=True)
    assert (allowed / 'replay').read_text() == 'allowed'
    assert not (checkout / 'replay').exists()
