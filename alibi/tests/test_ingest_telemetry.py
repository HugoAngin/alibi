import copy
import json
from alibi.adapter import run_case
from alibi.connectors.case_source import load_case_without_gold


def public_case_with_import():
    case = load_case_without_gold(1)
    record = copy.deepcopy(case['merchant_deliveries'][0]['event'])
    record['record_id'] = 'public-import-control'
    record['event_id'] = 'public-import-control-event'
    case['imports'] = [{'received_at': case['rfcs'][0]['issued_at'], 'record': record}]
    return case


def test_unsupported_import_is_counted_without_raw_values(tmp_path):
    case = public_case_with_import()
    bad = copy.deepcopy(case['imports'][0])
    bad['record']['kind'] = 'PRIVATE_KIND_SENTINEL'
    bad['record']['record_id'] = 'PRIVATE_RECORD_SENTINEL'
    bad['record']['text'] = 'PRIVATE_TEXT_SENTINEL'
    case['imports'].append(bad)
    rows = run_case(case, 'rules', root=tmp_path / 'with')
    telemetry = rows[0]['ingest_telemetry']
    assert telemetry['imports']['skipped'] >= 1
    assert telemetry['imports']['reasons']['unsupported_kind'] >= 1
    assert telemetry['available']['issuer_verified'] > 0
    encoded = json.dumps(telemetry)
    assert 'PRIVATE_' not in encoded
    assert case['subject_ref'] not in encoded
    assert all(k not in encoded for k in ['record_id', 'event_id', 'reason_message', 'seed', 'signature'])


def test_valid_control_metadata_is_optional_and_does_not_change_answers(tmp_path):
    case = public_case_with_import()
    yes = run_case(case, 'rules', root=tmp_path / 'yes')
    no = run_case(case, 'rules', root=tmp_path / 'no', ingest_telemetry=False)
    assert yes[0]['ingest_telemetry']['merchant']['accepted'] > 0
    assert yes[0]['ingest_telemetry']['imports']['accepted'] > 0
    assert all('ingest_telemetry' not in p for p in no)
    assert [p['claims'] for p in yes] == [p['claims'] for p in no]


def test_future_import_not_counted_before_visibility_and_issuer_refusal_is_aggregate(tmp_path):
    case = public_case_with_import()
    bad = copy.deepcopy(case['imports'][0])
    bad['record']['kind'] = 'PRIVATE_FUTURE_KIND'
    bad['received_at'] = case['rfcs'][1]['issued_at']
    case['imports'].append(bad)
    case['merchant_deliveries'][0]['event']['issuer_id'] = 'PRIVATE_UNKNOWN_ISSUER'
    rows = run_case(case, 'rules', root=tmp_path)
    first, second = [p['ingest_telemetry'] for p in rows]
    assert first['imports']['refused'] == 0
    assert second['imports']['refused'] == 1
    assert first['merchant']['reasons']['untrusted_issuer'] == 1
    assert 'PRIVATE_' not in json.dumps([first, second])
