from copy import deepcopy
import json

from agents.data.api import load_case
from agents.eval.merchant_contract import summarize


def test_public_case_maps_and_matches_pinned_issuer():
    result = summarize([load_case(1)])
    assert result['mapped'] == result['merchant_events']
    assert result['matching_issuer_pin'] == result['merchant_events']
    assert result['cases_without_usable_merchant_event'] == 0


def test_unknown_issuer_is_counted_without_granting_trust_or_leaking_values():
    case = deepcopy(load_case(1))
    for entry in case['merchant_deliveries'] + case['merchant_lookup']:
        entry['event']['issuer_id'] = 'PRIVATE_SENTINEL'
    original = deepcopy(case)
    result = summarize([case])
    assert case == original
    assert result['cases_without_usable_merchant_event'] == 1
    assert result['mapping_refused'] + result['issuer_pin_mismatch'] == result['merchant_events']
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_unknown_event_kind_is_diagnosed_without_coercion():
    case = deepcopy(load_case(1))
    for entry in case['merchant_deliveries'] + case['merchant_lookup']:
        entry['event']['kind'] = 'PRIVATE_SENTINEL'
    result = summarize([case])
    assert result['unknown_kind'] == result['merchant_events']
    assert result['mapping_refused'] == result['merchant_events']
    assert result['cases_without_usable_merchant_event'] == 1
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
