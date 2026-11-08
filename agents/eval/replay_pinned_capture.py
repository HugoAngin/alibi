"""Replay a completed capture using its recorded evaluator revision, never new workers."""
from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
PREFIX = ['uv', 'run', '--frozen', 'python', '-m', 'agents.eval.run_sealed']


def replay_command(record, capture):
    parts = shlex.split(record['command'])
    if len(parts) < 9 or parts[0] != 'env' or not parts[1].startswith('ALIBI_EVAL_ENV_FILE=') or parts[2:8] != PREFIX:
        raise ValueError('record is not the declared frozen capture command')
    args = parts[8:]
    if args.count('--out') != 1 or Path(args[args.index('--out') + 1]).resolve() != capture:
        raise ValueError('record output does not match the retained capture')
    return parts[1].split('=', 1)[1], args


DRIVER = '''import json,os,sys
from agents.eval import run_sealed
config=json.load(open(sys.argv[1]))
os.environ['ALIBI_EVAL_ENV_FILE']=config['credential']
def forbidden(*args,**kwargs):
    raise AssertionError('replay cannot launch any candidate worker')
run_sealed._parallel_capture=forbidden
run_sealed.run_case_worker=forbidden
run_sealed.main(config['args'])
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--record', required=True)
    args = parser.parse_args(argv)
    capture = args.capture.resolve()
    if capture.is_relative_to(REPO):
        raise ValueError('capture must be outside source checkout')
    marker = capture / 'run.json'
    before = marker.read_bytes()  # A missing completion marker fails before any execution.
    run = json.loads(before)
    launch = run['input']['evaluator_commit']
    candidate = run['input']['candidate']
    if not all(re.fullmatch('[0-9a-f]{40}', x) for x in (launch, candidate)):
        raise ValueError('invalid pinned revision')
    records = [json.loads(line) for line in (REPO / 'agents/eval/out/results.jsonl').read_text().splitlines()]
    matching = [r for r in records if r['id'] == args.record]
    if len(matching) != 1:
        raise ValueError('expected one original capture record')
    credential, command = replay_command(matching[0], capture)
    tag = command[command.index('--candidate-ref') + 1]
    if tag != 'sealed-candidate-5':
        raise ValueError('this replay is scoped to candidate five')
    def git(*values, cwd=REPO):
        return subprocess.check_output(['git', *values], cwd=cwd, stderr=subprocess.PIPE)
    objects = git('rev-parse', '--path-format=absolute', '--git-path', 'objects').decode().strip()
    archive = git('archive', launch)
    with TemporaryDirectory(prefix='alibi-pinned-replay-') as temp:
        root = Path(temp) / 'source'
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root, filter='data')
        git('init', '-q', cwd=root)
        (root / '.git/objects/info/alternates').write_text(objects + '\n')
        git('update-ref', 'HEAD', launch, cwd=root)
        git('read-tree', launch, cwd=root)
        git('update-ref', 'refs/tags/' + tag, candidate, cwd=root)
        config = Path(temp) / 'replay.json'
        config.write_text(json.dumps({'credential': credential, 'args': command}))
        config.chmod(0o600)
        completed = subprocess.run([sys.executable, '-c', DRIVER, str(config)], cwd=root,
                                   text=True, capture_output=True)
        if marker.read_bytes() != before:
            raise ValueError('completion marker changed during replay')
        if completed.returncode:
            sys.stderr.write(completed.stderr)
            raise RuntimeError('pinned replay failed; retained capture was not replaced')
        metrics = json.loads(completed.stdout.splitlines()[-1])
        print(json.dumps({'replay_evaluator': launch, 'capture_run_sha256': sha256(before).hexdigest(),
                          'original_record': args.record, 'new_workers': 0}, sort_keys=True))
        print(json.dumps(metrics, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
