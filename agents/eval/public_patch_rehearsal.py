"""Public-only patch rehearsal with committed inputs and hash-bound, read-only replay.

Capture: --base <40-char SHA> --patch agents/eval/out/X.patch --out <new directory>
Replay: --replay <directory> --receipt-sha256 <printed receipt digest>
Only public cases 1..80 run; optional one-pass uses FakeClient, never a provider.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

from agents.data.api import load_case
from alibi.eval import scorer, seal

REPO = Path(__file__).resolve().parents[2]
VERSION = 'alibi.eval.public_patch_rehearsal.v1'
MODES = ('rules', 'one_pass_fake')


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def public_cases():
    cases = [load_case(n) for n in range(1, 81)]
    if [case['case_number'] for case in cases] != list(range(1, 81)):
        raise ValueError('public case denominator changed')
    return cases


def git(*args, repo=REPO) -> bytes:
    return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.PIPE)


def committed_patches(paths, evaluator_sha, *, repo=REPO):
    """Read exact committed patch bytes; reject dirty artifacts and scoring/data edits."""
    entries, payloads = [], []
    for supplied in paths:
        path = (repo / supplied).resolve()
        name = path.relative_to(repo.resolve()).as_posix()
        if not name.startswith('agents/eval/out/') or not name.endswith('.patch'):
            raise ValueError('patches must be explicit evaluator out/*.patch artifacts')
        raw = git('show', f'{evaluator_sha}:{name}', repo=repo)
        if path.read_bytes() != raw:
            raise ValueError('patch differs from its committed artifact: ' + name)
        changed = subprocess.run(['git', 'apply', '--numstat', '-'], input=raw, cwd=repo,
                                 capture_output=True, check=True).stdout.decode().splitlines()
        targets = [line.split('\t', 2)[2] for line in changed]
        if not targets or any(not target.startswith('alibi/') or target.startswith(
                ('alibi/eval/', 'alibi/fixtures/')) for target in targets):
            raise ValueError('patch may change native alibi code/tests only, never scorer or fixtures')
        entries.append({'path': name, 'sha256': digest(raw)})
        payloads.append(raw)
    if len({entry['path'] for entry in entries}) != len(entries):
        raise ValueError('duplicate patch')
    return entries, payloads


RUNNER = r'''import hashlib,json,os,sys
from pathlib import Path
from alibi.adapter import run_cases
from agents.data.api import load_case,without_gold
from alibi.llm.fake import FakeClient
from alibi.llm.client import GeminiClient
def offline(*args,**kwargs):
    raise AssertionError('live model access forbidden in public patch rehearsal')
GeminiClient.for_run=offline
GeminiClient.generate=offline
mode=sys.argv[1]
root=Path(sys.argv[2])
cases=[load_case(number) for number in range(1,81)]
actual=hashlib.sha256(json.dumps(cases,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
if actual!=sys.argv[4]:
    raise ValueError('base public inputs differ from committed evaluator inputs')
client=FakeClient({'*':{'claims':[],'disclosures':[],'rationale':'Offline public rehearsal.'}}) if mode=='one_pass_fake' else None
native_mode='one_pass' if client else 'rules'
run_cases([without_gold(case) for case in cases],native_mode,client=client,
          out=root/'predictions',root=root/'stores',log=lambda _:None)
rows=[json.loads(line) for line in (root/'predictions'/f'{native_mode}.jsonl').read_text().splitlines()][1:]
Path(sys.argv[3]).write_text(json.dumps(rows,sort_keys=True,allow_nan=False)+'\n')
'''


def score_files(root, modes, cases):
    expected = {(case['case_number'], rfc['request_id']) for case in cases for rfc in case['rfcs']}
    reports = {}
    for mode in modes:
        rows = json.loads((root / f'{mode}.json').read_text())
        actual = [(row.get('case_number'), row.get('request_id')) for row in rows]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError('capture does not contain the exact 80-case public request denominator')
        score = scorer.run(cases, [scorer.build_system(mode, rows)], {})[0]
        reports[mode] = {'metrics': score.metrics,
                         'safe_cases': [case.case_number for case in score.cases if case.safe_complete]}
    return reports


def numeric_metrics(reports):
    metrics = {key: value for key, value in reports['rules']['metrics'].items()
               if type(value) in (int, float)}
    for mode, report in reports.items():
        metrics.update({f'{mode}_{key}': value for key, value in report['metrics'].items()
                        if type(value) in (int, float)})
    metrics['external_model_calls'] = 0
    return metrics


def replay(root: Path, receipt_sha256: str | None = None):
    raw = (root / 'receipt.json').read_bytes()
    if receipt_sha256 is not None and digest(raw) != receipt_sha256:
        raise ValueError('capture receipt commitment changed')
    receipt = json.loads(raw)
    if receipt.get('schema_version') != VERSION or receipt.get('status') != 'complete':
        raise ValueError('capture is incomplete or has an unknown schema')
    if receipt['modes'] not in [['rules'], ['rules', 'one_pass_fake']]:
        raise ValueError('unknown capture mode')
    if receipt['harness_sha256'] != digest(Path(__file__).read_bytes()):
        raise ValueError('replay harness differs from capture')
    if receipt['scorer_sha256'] != seal.scoring_sha256():
        raise ValueError('scorer differs from capture')
    cases = public_cases()
    if receipt['public_inputs_sha256'] != digest(encoded(cases)):
        raise ValueError('public inputs differ from capture')
    required = {f'{mode}.json' for mode in receipt['modes']} | {'report.json'}
    if set(receipt['files']) != required:
        raise ValueError('capture file commitments are incomplete')
    for name, expected in receipt['files'].items():
        if digest((root / name).read_bytes()) != expected:
            raise ValueError('retained capture file changed: ' + name)
    reports = score_files(root, receipt['modes'], cases)
    if reports != json.loads((root / 'report.json').read_text()):
        raise ValueError('replayed scores differ from retained report')
    return receipt, reports


def capture(base: str, patches, out: Path, *, fake_one_pass=False, timeout=600):
    if not re.fullmatch('[0-9a-f]{40}', base):
        raise ValueError('base must be an explicit full commit SHA')
    if git('rev-parse', '--verify', base + '^{commit}').decode().strip() != base:
        raise ValueError('base does not identify that commit')
    evaluator = git('rev-parse', 'HEAD').decode().strip()
    entries, payloads = committed_patches(patches, evaluator)
    modes = list(MODES if fake_one_pass else MODES[:1])
    if out.exists():
        receipt, reports = replay(out)
        if receipt['base_sha'] != base or receipt['patches'] != entries or receipt['modes'] != modes:
            raise ValueError('existing capture is bound to different inputs')
        return receipt, reports
    cases = public_cases()
    out.mkdir(parents=True, exist_ok=False)
    binding = {'schema_version': VERSION, 'base_sha': base, 'evaluator_sha': evaluator,
               'patches': entries, 'modes': modes, 'harness_sha256': digest(Path(__file__).read_bytes()),
               'scorer_sha256': seal.scoring_sha256(), 'public_inputs_sha256': digest(encoded(cases)),
               'scope': 'Post-hoc public development; one_pass_fake is offline, not Gemini performance.'}
    (out / 'started.json').write_bytes(encoded(binding) + b'\n')
    with TemporaryDirectory(prefix='alibi-public-patches-') as temp:
        source = Path(temp) / 'source'
        source.mkdir()
        archive = Path(temp) / 'candidate.tar'
        with archive.open('wb') as handle:
            subprocess.run(['git', 'archive', base], cwd=REPO, stdout=handle, check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter='data')
        for raw in payloads:
            subprocess.run(['git', 'apply', '-'], cwd=source, input=raw, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Neither patches nor a moving development checkout may substitute the scoring inputs.
        for name in seal.SCORING_FILES:
            if (source / 'alibi/eval' / name).read_bytes() != (REPO / 'alibi/eval' / name).read_bytes():
                raise ValueError('base scorer differs from evaluator scorer')
        env = {k: v for k, v in os.environ.items() if k not in
               ('GEMINI_API_KEY', 'ALIBI_MASTER_KEY', 'ALIBI_REPLAY', 'PYTHONPATH', 'LOCKBOX_OPEN')}
        for mode in modes:
            runtime = Path(temp) / ('runtime-' + mode)
            env['ALIBI_DATA_DIR'] = str(runtime)
            with (out / (mode + '.stderr.log')).open('w') as stderr:
                subprocess.run([sys.executable, '-c', RUNNER, mode, str(runtime),
                                str((out / f'{mode}.json').resolve()), binding['public_inputs_sha256']], cwd=source, env=env,
                               stdout=subprocess.DEVNULL, stderr=stderr, check=True, timeout=timeout)
    reports = score_files(out, modes, cases)
    (out / 'report.json').write_bytes(encoded(reports) + b'\n')
    names = [*(f'{mode}.json' for mode in modes), 'report.json']
    receipt = {**binding, 'status': 'complete',
               'files': {name: digest((out / name).read_bytes()) for name in names}}
    (out / 'receipt.json').write_bytes(encoded(receipt) + b'\n')
    return replay(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--patch', action='append', default=[])
    parser.add_argument('--out', type=Path)
    parser.add_argument('--fake-one-pass', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--receipt-sha256')
    args = parser.parse_args(argv)
    if args.replay:
        if not args.receipt_sha256 or args.base or args.patch or args.out or args.fake_one_pass:
            parser.error('replay requires its pinned receipt digest and no capture options')
        receipt, reports = replay(args.replay, args.receipt_sha256)
        root = args.replay
    else:
        if not args.base or not args.out or args.receipt_sha256:
            parser.error('capture requires --base and --out')
        root = args.out.resolve()
        receipt, reports = capture(args.base, args.patch, root, fake_one_pass=args.fake_one_pass,
                                   timeout=args.timeout)
    print(json.dumps({'base_sha': receipt['base_sha'], 'patches': receipt['patches'],
                      'receipt_sha256': digest((root / 'receipt.json').read_bytes()),
                      'capture': str(root), 'scope': receipt['scope']}, sort_keys=True))
    print(json.dumps(numeric_metrics(reports), sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
