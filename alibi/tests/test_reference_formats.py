"""Exact spaced issuer-text references; no provider normalization or new authorization."""
import copy

from agents.data.api import load_case
from alibi.adapter import run_case


def issuer_text_case():
    case = copy.deepcopy(load_case(73))
    case.pop('gold', None)
    credit = next(d['row'] for d in case['bank_deliveries'] if d['row']['amount_minor'] > 0)
    ref = credit['payment_reference']
    credit['payment_reference'] = None
    credit['description'] = 'SECOND PLATFORM LTD'
    notice = next(d['event'] for d in case['merchant_deliveries'] if d['event']['kind'] == 'earnings_update')
    notice['text'] += '\ntransfer_ref: ' + ref
    return case


def test_exact_spaced_reference_on_bank_credit_works(tmp_path):
    rows = run_case(73, 'rules', root=tmp_path / 'bank')
    assert rows[0]['claims']['q.source_of_funds']['status'] == 'supported'


def test_exact_spaced_reference_in_verified_issuer_field_works(tmp_path):
    rows = run_case(issuer_text_case(), 'rules', root=tmp_path / 'text')
    assert rows[0]['claims']['q.source_of_funds']['status'] == 'supported'


def test_spaced_reference_is_not_invented_from_earnings_week(tmp_path):
    case = issuer_text_case()
    notice = next(d['event'] for d in case['merchant_deliveries'] if d['event']['kind'] == 'earnings_update')
    notice['text'] = notice['text'].split('\n')[0]
    rows = run_case(case, 'rules', root=tmp_path / 'absent')
    assert rows[0]['claims']['q.source_of_funds']['status'] == 'unresolved'


def test_self_supplied_spaced_field_does_not_authorize_lookup(tmp_path, keystore, registry, now):
    import pytest
    from alibi import api
    from alibi.gateway import Gateway, ScopeError
    store = api.open_store(tmp_path / 'scope.db')
    try:
        fx = api.load_fixture(store, 'supported', keystore=keystore, now=now)
        api.import_self_supplied(store, {
            'record_id': 'self_spaced_reference', 'kind': 'message',
            'occurred_at': '2026-11-03T08:00:00Z',
            'text': 'transfer_ref: SPLAT W32 2695',
        }, subject_ref=fx['subject_ref'], now=now)
        called = []
        gateway = Gateway(store, subject_ref=fx['subject_ref'], run_id='run_spaced_scope',
                          registry=registry, settlement_lookup=lambda ref: called.append(ref), now=now)
        with pytest.raises(ScopeError):
            gateway.lookup_settlement('SPLAT W32 2695')
        assert called == []
    finally:
        store.close()


def test_explicit_reference_values_preserve_literal_separators():
    from alibi.core.investigate_rules import _TEXT_REF_FIELD
    assert _TEXT_REF_FIELD.fullmatch('transfer_ref: SPLAT W32 2695').group(1) == 'SPLAT W32 2695'
    assert _TEXT_REF_FIELD.fullmatch('payment reference = SOME ID 1234').group(1) == 'SOME ID 1234'
    assert _TEXT_REF_FIELD.fullmatch('Our ref: PO/0046/02') is None
    assert _TEXT_REF_FIELD.fullmatch('0047/02;16.07.2026;872,62') is None
    assert _TEXT_REF_FIELD.fullmatch('transfer_ref: ../../foreign') is None
