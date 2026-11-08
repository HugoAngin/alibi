"""Explicit post-hoc development diagnostics on a previously evaluated pack.

This is not a fresh unseen score. It never reads gold or changes the original
capture. A completed run replays aggregate metrics without launching workers.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
from tempfile import TemporaryDirectory

from alibi.eval import common, seal
from agents.eval.isolated_candidate import run_isolated
from agents.eval.parallel_capture import schedule
from agents.eval.sealed_worker import REPO, case_payload
from tools.lockbox import is_open

DISCLOSURE = "Post-hoc runtime diagnostic on reused held-out inputs; not fresh unseen accuracy."


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def files(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*'))
            if p.is_file() and p.name != 'receipt.json'}


def replay(root):
    receipt = common.load_json(root / 'receipt.json')
    if receipt['files'] != files(root):
        raise ValueError('post-hoc evidence changed')
    return common.load_json(root / 'summary.json')


def prepare_credentials(mode, credential_file):
    """Load the parent's key before isolated workers deny access to the source file."""
    if mode == 'rules':
        return
    if credential_file is None or not credential_file.is_file():
        raise ValueError('live development modes require an explicit existing credential file')
    from alibi.llm.client import load_api_key
    load_api_key(credential_file)


def capture(cases, *, output, candidate, private_root, deadline, trusted_issuers_file=None,
            trusted_issuers_sha256=None, mode='rules', credential_file=None, development_patches=()):
    """Private worker artifacts; only counts and fixed failure codes exported."""
    counts = dict(cases=len(cases), completed=0, failed=0, unstarted=0, scheduler_errors=0,
                  model_calls=0 if mode == 'rules' else None)
    modes = ('agents', 'one_pass', 'rules') if mode == 'all' else (mode,)
    if mode == 'all':
        counts['expected_jobs'] = len(cases) * len(modes)
    by_system = {name: dict(cases=len(cases), completed=0, failed=0, unstarted=0)
                 for name in modes}
    if mode != 'rules' and credential_file is None:
        raise ValueError('live development modes require an explicit credential file')
    codes = {}
    with ExitStack() as stack:
        roots = [Path(stack.enter_context(TemporaryDirectory(prefix='alibi-postfix-')))
                 for _ in range(4)]
        lanes = Queue()
        for root in roots:
            lanes.put(root)
        credential = credential_file
        if credential is None:
            credential = output / 'no-model-credential'
            credential.write_text('No model credentials; rules-only diagnostic.\n')

        def run(job):
            system, case = job
            lane = lanes.get()
            try:
                worker_root = output / 'workers'
                if mode == 'all':
                    worker_root /= system
                return run_isolated(case, system, candidate=candidate, private_root=private_root,
                    credential=credential, output=worker_root / str(case['case_number']),
                    scratch_parent=lane, denied_scratch=tuple(p for p in roots if p != lane),
                    capture_root=output.parent, deadline=deadline,
                    trusted_issuers_file=trusted_issuers_file,
                    trusted_issuers_sha256=trusted_issuers_sha256,
                    development_patches=development_patches)
            finally:
                lanes.put(lane)

        def progress(outcome, aggregate):
            system = outcome['job'][0]
            if outcome['status'] == 'not_started':
                counts['unstarted'] += 1
                by_system[system]['unstarted'] += 1
            elif outcome['status'] == 'error':
                counts['scheduler_errors'] += 1
            else:
                rows, metadata = outcome['result']
                failed = bool(metadata['failures'])
                counts['failed' if failed else 'completed'] += 1
                by_system[system]['failed' if failed else 'completed'] += 1
                diagnostics = metadata.get('failure_diagnostics', {})
                code = diagnostics.get('code', 'unclassified_failure') if failed else 'completed'
                codes[code] = codes.get(code, 0) + 1
            common.write_json(output / 'progress.json', dict(counts, by_system=by_system))
            print(json.dumps({'posthoc_runtime_progress': counts, 'by_system': by_system}), flush=True)

        jobs = [(system, case) for case in cases for system in modes]
        result = schedule(jobs, run, workers=4, deadline=deadline, stop_on_error=True,
                          progress=progress)
    return dict(disclosure=DISCLOSURE, candidate=candidate, mode=mode, metrics=counts,
                development_patches=[entry for entry, _ in development_patches],
                by_system=by_system,
                failure_codes=codes, scheduling=result['metadata'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    for name in ('cases', 'manifest', 'private-root', 'original-capture'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--candidate')
    parser.add_argument('--deadline')
    parser.add_argument('--trusted-issuers-file', type=Path)
    parser.add_argument('--mode', choices=('rules', 'one_pass', 'agents', 'all'), default='rules')
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--development-patch', action='append', default=[])
    args = parser.parse_args(argv)
    os.umask(0o077)
    output = args.out.resolve()
    if output.exists():
        summary = replay(output)
        print(json.dumps(summary['metrics'], sort_keys=True))
        return
    if not is_open():
        parser.error('new diagnostic requires results.py add --split lockbox --reason')
    if not all((args.cases, args.manifest, args.private_root, args.original_capture,
                args.candidate, args.deadline)):
        parser.error('new diagnostic requires all input commitments and a deadline')
    private = args.private_root.resolve(strict=True)
    original = args.original_capture.resolve(strict=True)
    inputs = args.cases.resolve(strict=True)
    if (not inputs.is_relative_to(private) or output.is_relative_to(private)
            or output.is_relative_to(original) or output.is_relative_to(REPO)
            or original.parent != output.parent):
        raise ValueError('separate private capture sibling and original input root required')
    deadline = datetime.fromisoformat(args.deadline)
    if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
        raise ValueError('future timezone-aware deadline required')
    candidate = subprocess.check_output(['git', 'rev-parse', args.candidate + '^{commit}'],
                                       cwd=REPO, text=True).strip()
    patches = []
    if args.development_patch:
        from agents.eval.public_patch_rehearsal import committed_patches
        evaluator = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
        entries, payloads = committed_patches(args.development_patch, evaluator)
        patches = list(zip(entries, payloads))
    prepare_credentials(args.mode, args.credential_file)
    manifest = common.load_json(args.manifest)
    trust_digest = None
    if args.trusted_issuers_file is not None:
        from agents.eval.trusted_setup import load
        trust_digest = digest(args.trusted_issuers_file)
        load(args.trusted_issuers_file, trust_digest)
    initial_run_hash = digest(original / 'run.json')
    original_run = common.load_json(original / 'run.json')
    if original_run['input']['manifest'] != digest(args.manifest):
        raise ValueError('manifest is not the previously evaluated pack')
    input_files, entries = seal.case_entries(inputs)
    if entries != manifest['cases']['entries']:
        raise ValueError('case commitments changed')
    cases = common.load_sealed_cases(inputs, require_gold=False)
    for case in cases:
        case_payload(case)
    output.mkdir(mode=0o700)
    common.write_json(output / 'authorization.json', {
        'disclosure': DISCLOSURE, 'authority': 'Tamim explicitly requested continuing fixes and testing',
        'original_run_sha256': initial_run_hash, 'manifest_sha256': digest(args.manifest),
        'candidate': candidate, 'evaluator': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(), 'gold_read': False,
        'trusted_setup_sha256': trust_digest, 'mode': args.mode,
        'development_patches': [entry for entry, _ in patches]})
    summary = capture(cases, output=output, candidate=candidate, private_root=private,
                      deadline=deadline, trusted_issuers_file=args.trusted_issuers_file,
                      trusted_issuers_sha256=trust_digest, mode=args.mode,
                      credential_file=args.credential_file, development_patches=patches)
    if (seal.case_entries(inputs) != (input_files, entries)
            or digest(original / 'run.json') != initial_run_hash):
        raise ValueError('original input commitments changed during diagnostic')
    common.write_json(output / 'summary.json', summary)
    common.write_json(output / 'receipt.json', {'files': files(output)})
    print(json.dumps(summary['metrics'], sort_keys=True))


if __name__ == '__main__':
    main()
