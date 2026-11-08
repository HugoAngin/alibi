from datetime import datetime, timedelta, timezone
import json

import pytest

from agents.eval import public_parallel as module


def test_open_lockbox_refuses_before_creating_public_output(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'is_open', lambda: True)
    with pytest.raises(ValueError, match='closed lockbox'):
        module.capture(candidate='fixed', private_root=tmp_path, credential=tmp_path/'key',
                       output=tmp_path/'output', deadline=datetime.now(timezone.utc))
    assert not (tmp_path/'output').exists()


def test_incomplete_worker_set_preserved_but_never_scored(tmp_path, monkeypatch):
    private = tmp_path/'private'
    private.mkdir()
    output = tmp_path/'public'
    monkeypatch.setattr(module, 'is_open', lambda: False)
    monkeypatch.setattr('alibi.llm.client.load_api_key', lambda _: None)
    def incomplete(output, cases, isolation):
        assert isolation['workers'] == 4
        folder = output/'capture-workers'/'rules-1'
        folder.mkdir(parents=True)
        (folder/'metadata.json').write_text(json.dumps(dict(system='rules', case_number=1,
            candidate='fixed', failures=[], isolation_preflight='passed')))
    monkeypatch.setattr(module, '_parallel_capture', incomplete)
    monkeypatch.setattr(module, 'score_saved', lambda *a, **kw: pytest.fail('must not score'))
    with pytest.raises(ValueError, match='six isolated workers'):
        module.capture(candidate='fixed', private_root=private, credential=tmp_path/'key',
            output=output, deadline=datetime.now(timezone.utc)+timedelta(minutes=1))
    assert (output/'run.json').exists()
    with pytest.raises(FileExistsError):
        module.capture(candidate='fixed', private_root=private, credential=tmp_path/'key',
            output=output, deadline=datetime.now(timezone.utc)+timedelta(minutes=1))
