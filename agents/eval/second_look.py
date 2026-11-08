"""Public rehearsal gate for run_sealed --post-fix-second-look.

This module never opens private inputs or launches a second capture. The canonical
capture command owns the exclusive candidate-4 lock and original manifest checks.
"""
import argparse
import json
from pathlib import Path

from alibi.eval import common, scorer
from agents.data.api import load_case
from agents.eval.postfix_runtime import files
from agents.eval.sealed_worker import MODES


def public_gate(root, candidate, *, trusted_issuers_sha256=None):
    run = common.load_json(root / 'run.json')
    if run.get('candidate') != candidate or run.get('restriction_variant'):
        raise ValueError('public rehearsal must use exact candidate without case modifications')
    workers = run.get('workers', [])
    expected = {(mode, number) for mode in MODES for number in (1, 2)}
    if (len(workers) != len(expected)
            or {(w['system'], w['case_number']) for w in workers} != expected
            or any(w.get('failures') or w.get('candidate') != candidate
                   or w.get('trusted_setup_sha256') != trusted_issuers_sha256
                   or (trusted_issuers_sha256 is not None and not w.get('trusted_setup_verified'))
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
    parser.add_argument('--public-rehearsal', type=Path, required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--trusted-setup-sha256')
    args = parser.parse_args(argv)
    commitment = public_gate(args.public_rehearsal, args.candidate,
                             trusted_issuers_sha256=args.trusted_setup_sha256)
    print(json.dumps({'public_gate_passed': True, 'candidate': args.candidate,
                      'files': commitment}, sort_keys=True))


if __name__ == '__main__':
    main()
