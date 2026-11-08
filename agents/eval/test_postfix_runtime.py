import json
from datetime import datetime, timedelta, timezone

import pytest

from agents.eval import postfix_runtime as diagnostic


def test_capture_counts_failures_without_exporting_case_values(tmp_path, monkeypatch):
    seen = []

    def worker(case, mode, **kwargs):
        assert mode == 'rules'
        assert kwargs['capture_root'] == tmp_path.parent
        assert len(kwargs['denied_scratch']) == 3
        assert kwargs['private_root'] == tmp_path / 'private'
        assert kwargs['trusted_issuers_file'] == tmp_path / 'trusted-private.json'
        assert kwargs['trusted_issuers_sha256'] == 'a' * 64
        seen.append(case['case_number'])
        failed = case['case_number'] == 2
        return [], {'failures': ['error'] if failed else [],
                    'failure_diagnostics': {'code': 'self_supplied_import_refused'}}

    monkeypatch.setattr(diagnostic, 'run_isolated', worker)
    cases = [{'case_number': i, 'secret': 'PRIVATE_SENTINEL'} for i in (1, 2)]
    result = diagnostic.capture(cases, output=tmp_path, candidate='public-test',
        private_root=tmp_path / 'private', deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
        trusted_issuers_file=tmp_path / 'trusted-private.json', trusted_issuers_sha256='a' * 64)
    assert sorted(seen) == [1, 2]
    assert result['metrics'] == dict(cases=2, completed=1, failed=1, unstarted=0,
                                     scheduler_errors=0, model_calls=0)
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_replay_rejects_tampering_and_never_launches_workers(tmp_path, monkeypatch):
    (tmp_path / 'summary.json').write_text(json.dumps({'metrics': {'cases': 2}}))
    (tmp_path / 'receipt.json').write_text(json.dumps({'files': diagnostic.files(tmp_path)}))
    monkeypatch.setattr(diagnostic, 'run_isolated', lambda *a, **k: pytest.fail('replay launched'))
    diagnostic.main(['--out', str(tmp_path)])
    (tmp_path / 'summary.json').write_text('{}')
    with pytest.raises(ValueError, match='evidence changed'):
        diagnostic.main(['--out', str(tmp_path)])


def test_new_capture_requires_results_gate_before_input_access(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, 'is_open', lambda: False)
    with pytest.raises(SystemExit):
        diagnostic.main(['--out', str(tmp_path / 'new')])
    assert not (tmp_path / 'new').exists()
