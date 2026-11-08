"""Public synthetic variants: exact connector lookup keys must keep observed casing."""
import json
import pytest
from agents.data.api import load_case
from alibi.adapter import run_case


def variant(ref, scale=1):
    case = load_case(1)
    case.pop('gold', None)
    raw = json.dumps(case).replace('PO-0001-01', ref).replace('maya-001', 'holder-zeta-921')
    raw = raw.replace('rec-0001-', 'rentry-921-').replace('txn-0001-', 'credit-921-')
    raw = raw.replace('rfc-0001-', 'request-921-')
    case = json.loads(raw)
    def amounts(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == 'amount_minor' and type(value) is int:
                    obj[key] = value * scale
                elif key == 'stated_minor':
                    obj[key] = {k: v * scale for k, v in value.items()}
                else:
                    amounts(value)
        elif isinstance(obj, list):
            for item in obj:
                amounts(item)
    amounts(case)
    for request in case['rfcs']:
        for q in request['questions']:
            if q['question_id'] == 'q.source_of_funds':
                q.update(question_id='q.origin_evidence', text='Where did this credit come from?')
            elif q['question_id'] == 'q.breakdown':
                q.update(question_id='q.amount_components', text='Explain how this credit was calculated.')
    return case


@pytest.mark.parametrize('ref,scale', [('pay-Ab9-701', 1), ('wire-z7-9', 3), ('TrNs-712', 2)])
def test_changed_identity_amount_and_literal_reference_remain_supported(tmp_path, ref, scale):
    rows = run_case(variant(ref, scale), 'rules', root=tmp_path / 'variants')
    assert all(row['failure'] is None for row in rows)
    row = next(row for row in rows if row['request_id'] == 'request-921-01')
    source = row['claims']['q.origin_evidence']
    breakdown = row['claims']['q.amount_components']
    assert source['status'] == 'supported'
    assert source['facts']['payout_references'] == [ref]
    assert breakdown['status'] == 'supported'
    assert breakdown['facts']['breakdown_minor']['net'] == 216000 * scale


def test_description_only_reference_keeps_its_observed_case(tmp_path):
    case = variant('ref-Mix42-911')
    for delivery in case['bank_deliveries']:
        if delivery['row']['amount_minor'] > 0:
            delivery['row']['payment_reference'] = None
    row = run_case(case, 'rules', root=tmp_path / 'description')[0]
    assert row['claims']['q.origin_evidence']['status'] == 'supported'
    assert row['claims']['q.origin_evidence']['facts']['payout_references'] == ['ref-Mix42-911']


def test_lookup_does_not_guess_another_casing(tmp_path):
    case = variant('ref-Mix42-911')
    for delivery in case['bank_deliveries']:
        row = delivery['row']
        if row['amount_minor'] > 0:
            row['payment_reference'] = 'REF-MIX42-911'
            row['description'] = 'CREDIT REF-MIX42-911'
    row = run_case(case, 'rules', root=tmp_path / 'mismatched')[0]
    assert row['claims']['q.origin_evidence']['status'] == 'unresolved'
