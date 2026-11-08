"""Typed exact reference shapes across offline modes, public-derived fixtures only."""
import json
import pytest
from alibi.adapter import run_case
from alibi.llm.fake import FakeClient
from alibi.tests.test_literal_reference_generalization import variant

NO_PROPOSAL = {'claims': [], 'disclosures': [], 'rationale': 'Public synthetic control'}

@pytest.mark.parametrize('ref', ['pay-Ab9-701', 'SETTLEMENTALPHA', 'pay_alpha_7', 'pay.alpha.7', 'Pay Group 7'])
@pytest.mark.parametrize('mode', ['rules', 'one_pass'])
def test_typed_literal_reference_shape_with_new_issuer(tmp_path, mode, ref):
    case = json.loads(json.dumps(variant(ref, 2)).replace('mock-marketplace', 'public-synthetic-provider'))
    rows = run_case(case, mode, client=FakeClient({'*': NO_PROPOSAL}),
                    trusted_issuers={'public_provider_key': 'public-synthetic-provider'}, root=tmp_path / 'run')
    assert all(row['failure'] is None for row in rows)
    first = next(row for row in rows if row['request_id'] == 'request-921-01')
    assert first['claims']['q.origin_evidence']['status'] == 'supported'
    assert first['claims']['q.origin_evidence']['facts']['payout_references'] == [ref]
    assert first['claims']['q.amount_components']['facts']['breakdown_minor']['net'] == 432000
