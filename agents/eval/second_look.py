"""One complete, explicitly post-fix second look on sealed-candidate-4.

Original frozen evidence is untouched. A completed output only replays. An
incomplete output is never retried or selectively resumed by this command.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from alibi.eval import common, scorer, seal
from alibi.llm import client
from agents.data.api import load_case
from agents.eval.isolated_candidate import LIMITS
from agents.eval.postfix_runtime import digest, files
from agents.eval.run_sealed import _parallel_capture, score_saved
from agents.eval.sealed_worker import REPO, MODES, case_payload
from tools.lockbox import is_open

DISCLOSURE = ('Post-fix second look on reused synthetic held-out cases, variants 4. '
              'This does not replace the frozen 58bf0d4 result or establish fresh unseen accuracy. '
              'All 44 cases and all three systems are attempted once; no selective retries.')
DEADLINE = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)


def public_gate(root, candidate):
    run = common.load_json(root / 'run.json')
    if run.get('candidate') != candidate or run.get('restriction_variant'):
        raise ValueError('public rehearsal must use exact candidate without case modifications')
    workers = run.get('workers', [])
    expected = {(mode, number) for mode in MODES for number in (1, 2)}
    if (len(workers) != len(expected)
            or {(w['system'], w['case_number']) for w in workers} != expected
            or any(w.get('failures') or w.get('candidate') != candidate
                   or w.get('isolation_preflight') != 'passed' for w in workers)):
        raise ValueError('public rehearsal must complete all six isolated workers')
    requests = {(n, r['request_id']) for n in (1, 2) for r in load_case(n)['rfcs']}
    for mode in MODES:
        system = scorer.load_predictions(root / f'{mode}.jsonl', mode)
        if system.invalid or set(system.predictions) != requests:
            raise ValueError('public rehearsal prediction schema or denominator failed')
    return files(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    for name in ('cases', 'gold', 'manifest', 'model-config', 'private-root',
                 'original-capture', 'public-rehearsal', 'credential'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    output = args.out.resolve()
    if output.exists():
        receipt = common.load_json(output / 'receipt.json')
        if receipt['files'] != files(output):
            raise ValueError('second-look evidence changed')
        print(json.dumps(common.load_json(output / 'metrics.json'), sort_keys=True))
        return
    if not is_open():
        parser.error('use results.py add --split lockbox --variants 4 --reason')
    if not all(getattr(args, name.replace('-', '_')) for name in
               ('cases', 'gold', 'manifest', 'model-config', 'private-root',
                'original-capture', 'public-rehearsal', 'credential')):
        parser.error('new second look requires original commitments, public gate and credential path')
    if datetime.now(timezone.utc) >= DEADLINE:
        raise ValueError('second-look hard stop has passed')
    private = args.private_root.resolve(strict=True)
    original = args.original_capture.resolve(strict=True)
    if (output.parent != original.parent or output.is_relative_to(original)
            or output.is_relative_to(private) or output.is_relative_to(REPO)
            or not args.cases.resolve().is_relative_to(private)
            or not args.gold.resolve().is_relative_to(private)):
        raise ValueError('second look needs separate private output and unchanged private input root')
    candidate = subprocess.check_output(['git', 'rev-parse', 'refs/tags/sealed-candidate-4^{commit}'],
                                       cwd=REPO, text=True).strip()
    remote = subprocess.check_output(['git', 'ls-remote', '--tags', 'origin',
                                     'refs/tags/sealed-candidate-4',
                                     'refs/tags/sealed-candidate-4^{}'], cwd=REPO, text=True)
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    if refs.get('refs/tags/sealed-candidate-4^{}', refs.get('refs/tags/sealed-candidate-4')) != candidate:
        raise ValueError('published candidate-4 tag does not match local tag')
    original_hash = digest(original / 'run.json')
    original_run = common.load_json(original / 'run.json')
    if original_run['input']['manifest'] != digest(args.manifest):
        raise ValueError('manifest is not the original evaluated pack')
    problems, _ = seal.verify_manifest(args.manifest, args.cases, args.gold, args.model_config)
    if problems:
        raise ValueError('original case, gold, model or scoring commitments changed')
    rehearsal_files = public_gate(args.public_rehearsal, candidate)
    cases = common.load_sealed_cases(args.cases, require_gold=False)
    if len(cases) != 44:
        raise ValueError('expected the complete 44-case pack')
    for case in cases:
        case_payload(case)
    attacks = {c['case_number'] for c in common.read_jsonl(args.cases / 'adversarial.jsonl')}
    if len(attacks) != 12:
        raise ValueError('expected the original twelve adversarial cases')
    client.load_api_key(args.credential)
    signature = dict(candidate=candidate, evaluator_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        original_run_sha256=original_hash, manifest_sha256=digest(args.manifest),
        model_config_sha256=digest(args.model_config), public_rehearsal_files=rehearsal_files,
        workers=4, limits=LIMITS, deadline=DEADLINE.isoformat(), variants=4,
        disclosure=DISCLOSURE, authority='Tamim and lead board 6721371675')
    output.mkdir(mode=0o700)
    common.write_json(output / 'authorization.json', signature)
    (output / 'probes.jsonl').write_text('')
    # No old candidate probe receipt is transferred to this candidate. Fresh probe
    # evidence is reported separately by execution, so the full comparison is not
    # mistaken for a new sharing/security certification.
    isolation = dict(candidate=candidate, private_root=private, credential=args.credential,
                     workers=4, deadline=DEADLINE, capture_root=output.parent)
    _parallel_capture(output, cases, isolation, signature)
    problems, _ = seal.verify_manifest(args.manifest, args.cases, args.gold, args.model_config)
    if problems or digest(original / 'run.json') != original_hash:
        raise ValueError('original commitments changed during second look')
    gold = common.read_gold_file(args.gold)
    scored = [{**case, 'gold': gold[case['case_number']]} for case in cases]
    metrics = score_saved(output, [c for c in scored if c['case_number'] not in attacks],
                          [c for c in scored if c['case_number'] in attacks], kind='frozen')
    scheduling = common.load_json(output / 'scheduling.json')
    for mode, counts in scheduling['systems'].items():
        metrics.update({f'{mode}_runtime_{key}': value for key, value in counts.items()})
    for name in ('report.md', 'deck-table.md'):
        path = output / name
        path.write_text(DISCLOSURE + '\n\n' + path.read_text())
    common.write_json(output / 'metrics.json', metrics)
    common.write_json(output / 'receipt.json', {'files': files(output)})
    print(json.dumps(metrics, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
