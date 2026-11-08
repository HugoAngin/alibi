"""Capture commitments and replay are enforced without rerunning a candidate."""
import json
from pathlib import Path
import subprocess

import pytest

from agents.eval import public_patch_rehearsal as rehearsal


@pytest.fixture
def retained(tmp_path, monkeypatch):
    cases = [{'case_number': 1, 'rfcs': [{'request_id': 'public-request'}]}]
    reports = {'rules': {'metrics': {'cases': 1, 'safe_complete': 0}, 'safe_cases': []}}
    monkeypatch.setattr(rehearsal, 'public_cases', lambda: cases)
    monkeypatch.setattr(rehearsal, 'score_files', lambda *args: reports)
    (tmp_path / 'rules.json').write_text('[]\n')
    (tmp_path / 'report.json').write_bytes(rehearsal.encoded(reports))
    receipt = {'schema_version': rehearsal.VERSION, 'status': 'complete', 'modes': ['rules'],
               'base_sha': 'a' * 40, 'patches': [],
               'harness_sha256': rehearsal.digest(Path(rehearsal.__file__).read_bytes()),
               'scorer_sha256': rehearsal.seal.scoring_sha256(),
               'public_inputs_sha256': rehearsal.digest(rehearsal.encoded(cases)),
               'files': {name: rehearsal.digest((tmp_path / name).read_bytes())
                         for name in ('rules.json', 'report.json')}}
    (tmp_path / 'receipt.json').write_bytes(rehearsal.encoded(receipt))
    return tmp_path, rehearsal.digest((tmp_path / 'receipt.json').read_bytes()), reports


def test_replay_checks_hashes_without_running_a_worker(retained, monkeypatch):
    root, commitment, reports = retained
    monkeypatch.setattr(rehearsal.subprocess, 'run', lambda *a, **k: pytest.fail('replay launched a worker'))
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert rehearsal.replay(root, commitment)[1] == reports
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


@pytest.mark.parametrize('changed', ['rules.json', 'report.json', 'receipt.json'])
def test_replay_refuses_modified_capture_bytes(retained, changed):
    root, commitment, _ = retained
    with (root / changed).open('ab') as handle:
        handle.write(b' ')
    with pytest.raises(ValueError, match='changed'):
        rehearsal.replay(root, commitment)


def test_missing_public_requests_are_never_dropped_from_denominator(tmp_path):
    (tmp_path / 'rules.json').write_text('[]')
    cases = [{'case_number': 1, 'rfcs': [{'request_id': 'missing'}]}]
    with pytest.raises(ValueError, match='denominator'):
        rehearsal.score_files(tmp_path, ['rules'], cases)


def test_patch_inputs_must_be_committed_and_cannot_modify_scorer(tmp_path):
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    path = tmp_path / 'agents/eval/out/fix.patch'
    path.parent.mkdir(parents=True)
    raw = '--- a/alibi/eval/scorer.py\n+++ b/alibi/eval/scorer.py\n@@ -1 +1 @@\n-old\n+new\n'
    path.write_text(raw)
    subprocess.run(['git', 'add', str(path.relative_to(tmp_path))], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'Public test patch'], cwd=tmp_path, check=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=tmp_path, text=True).strip()
    with pytest.raises(ValueError, match='never scorer'):
        rehearsal.committed_patches([str(path)], commit, repo=tmp_path)
    path.write_text(raw + '\n')
    with pytest.raises(ValueError, match='differs from its committed'):
        rehearsal.committed_patches([str(path)], commit, repo=tmp_path)
