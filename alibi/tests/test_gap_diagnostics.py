"""Offline public diagnostics only: verdicts and minimum disclosure stay unchanged."""
import json

import pytest

from alibi.adapter import run_case
from alibi.connectors.case_source import load_case_without_gold
from alibi.core.store import Store


@pytest.mark.parametrize('number,gap,dependency', [
    (17, 'balance_release_not_settlement', 'rec-0017-012'),
    (18, 'balance_release_not_settlement', 'rec-0018-010'),
    (19, 'settlement_after_credit', 'rec-0019-011'),
    (25, 'settlement_failed', 'rec-0025-011'),
    (32, 'no_merchant_records', None),
])
def test_public_gap_names_observed_obstacle_without_support_or_disclosure(tmp_path, number, gap, dependency):
    root = tmp_path / str(number)
    rows = run_case(load_case_without_gold(number), 'rules', root=root)
    assert rows and all(row['failure'] is None for row in rows)
    for row in rows:
        for claim in row['claims'].values():
            assert claim['status'] == 'unresolved'
            assert claim['gap'].split(':')[0] == gap
            assert claim['evidence_refs'] == []
    with Store(root / f'case_{number}' / 'passport.db') as store:
        claims = store.list_claims()
        assert claims and all(c.examined_refs for c in claims)
        if dependency:
            assert any(dependency in {r.ref_id for r in c.examined_refs} for c in claims)
        for package in store.list_packages():
            body = package.body.to_json_dict()
            assert body['attachments'] == []
            assert 'examined_refs' not in json.dumps(body)
