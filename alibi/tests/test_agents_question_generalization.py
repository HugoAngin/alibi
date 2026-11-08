"""Semantic questions and literal retrieval must survive renamed public fixture IDs."""
import json
import pytest
from alibi.adapter import run_case
from alibi.llm.fake import FakeClient
from alibi.tests.test_literal_reference_generalization import variant


def challenger(model, system, user, schema):
    payload = json.loads(user)
    ref = payload['bank'][0]['payment_references'][0]
    payouts = [r for r in payload['merchant'] if r['kind'] == 'payout']
    ready = bool(payouts)
    return {'claim_id': payload['claim']['claim_id'],
            'proposed_status': 'supported' if ready else 'unresolved',
            'bank_settlement_established': ready, 'payout_reference': ref,
            'confirming_refs': [{'record_id': r['record_id'], 'revision': r['revision'], 'why': 'Signed payout'} for r in payouts],
            'conflicting_refs': [], 'missing_evidence': [] if ready else ['settlement'],
            'next_action': {'type': 'none' if ready else 'lookup_settlement',
                            'args': {} if ready else {'payout_reference': ref}, 'reason': 'Exact observed bank reference'},
            'trace': [], 'rationale': 'Synthetic scripted lookup; code validates the result.'}


def client():
    return FakeClient({'You are the Detective': {'alerts': [], 'claims': []},
                       'You are the Challenger': challenger,
                       'You are the Curator': {'disclosures': [], 'exclusions': [], 'gaps': [], 'rationale': 'No proposal'}})


@pytest.mark.parametrize('ref', ['pay-Ab9-701', 'SETTLEMENTALPHA', 'pay_alpha_7', 'pay.alpha.7', 'Pay Group 7'])
def test_agents_preserve_exact_action_reference_and_classify_renamed_questions(tmp_path, ref):
    case = json.loads(json.dumps(variant(ref, 2)).replace('mock-marketplace', 'public-synthetic-provider'))
    for request in case['rfcs']:
        for q in request['questions']:
            if q['question_id'] == 'q.sales_refunds_in_period':
                q.update(question_id='q.period_components', text='Show documented sales and refunds over this period.')
    rows = run_case(case, 'agents', client=client(), root=tmp_path / 'agents',
                    trusted_issuers={'public_provider_key': 'public-synthetic-provider'})
    assert all(row['failure'] is None for row in rows)
    claims = {key: value for row in rows for key, value in row['claims'].items()}
    assert claims['q.origin_evidence']['status'] == 'supported'
    assert claims['q.origin_evidence']['facts']['payout_references'] == [ref]
    assert claims['q.amount_components']['status'] == 'supported'
    assert claims['q.amount_components']['facts']['breakdown_minor']['net'] == 432000
    assert claims['q.period_components']['status'] == 'supported'
    assert claims['q.period_components']['facts'] == {'sales_minor': 480000, 'sale_count': 8, 'refunds_minor': 36000, 'refund_count': 2}


def test_arbitrary_breakdown_id_works_without_a_source_question(tmp_path):
    case = variant('PayAlpha42', 3)
    case['rfcs'] = [case['rfcs'][0]]
    case['rfcs'][0]['questions'] = [q for q in case['rfcs'][0]['questions'] if q['question_id'] == 'q.amount_components']
    rows = run_case(case, 'agents', client=client(), root=tmp_path / 'only_breakdown')
    claim = rows[0]['claims']['q.amount_components']
    assert claim['status'] == 'supported'
    assert claim['facts']['breakdown_minor']['net'] == 648000


def test_unrelated_question_remains_unresolved(tmp_path):
    case = variant('PayAlpha42')
    case['rfcs'] = [case['rfcs'][0]]
    case['rfcs'][0]['questions'] = [{'question_id': 'q.unmapped_general', 'text': 'What will next year look like?'}]
    rows = run_case(case, 'agents', client=client(), root=tmp_path / 'unknown')
    assert rows[0]['claims']['q.unmapped_general']['status'] == 'unresolved'
