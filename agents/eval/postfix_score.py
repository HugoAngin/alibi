"""Score retained post-hoc rules predictions; no candidate executions or fresh-unseen claim."""
import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from alibi.eval import common, scorer, seal
from agents.eval.postfix_runtime import DISCLOSURE, digest, files, replay
from agents.eval.sealed_worker import REPO
from tools.lockbox import is_open


def score(capture, cases, gold_path):
    gold = common.read_gold_file(gold_path)
    inputs = common.load_sealed_cases(cases, require_gold=False)
    attacks = {case['case_number'] for case in common.read_jsonl(cases / 'adversarial.jsonl')}
    expected = {(case['case_number'], r['request_id']) for case in inputs for r in case['rfcs']}
    rows = []
    for path in sorted((capture / 'workers').glob('*/predictions.json')):
        rows.extend(common.load_json(path))
    keys = [(row['case_number'], row['request_id']) for row in rows]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError('post-hoc prediction denominator is incomplete or duplicated')
    scored_cases = [{**case, 'gold': gold[case['case_number']]} for case in inputs]
    with TemporaryDirectory(prefix='alibi-postfix-score-') as temp:
        predictions = Path(temp) / 'rules.jsonl'
        predictions.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        system = scorer.load_predictions(predictions, 'rules')
        return {name: scorer.run(subset, [system], {})[0].metrics for name, subset in (
            ('main', [c for c in scored_cases if c['case_number'] not in attacks]),
            ('adversarial', [c for c in scored_cases if c['case_number'] in attacks]))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('capture', 'cases', 'gold', 'manifest', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    output = args.out.resolve()
    if output.exists():
        result = replay(output)
    else:
        if not is_open():
            parser.error('post-hoc scoring requires results.py add --split lockbox --reason')
        if (output.parent != args.capture.resolve().parent
                or output.is_relative_to(args.capture.resolve())
                or output.is_relative_to(args.gold.resolve().parent)
                or output.is_relative_to(REPO)):
            raise ValueError('score output must be a separate private capture sibling')
        runtime = replay(args.capture)
        authorization = common.load_json(args.capture / 'authorization.json')
        manifest = common.load_json(args.manifest)
        if digest(args.manifest) != authorization['manifest_sha256']:
            raise ValueError('diagnostic input manifest changed')
        if seal.case_entries(args.cases)[1] != manifest['cases']['entries']:
            raise ValueError('input cases changed')
        gold_digest, entries = seal.gold_entries(args.gold)
        if entries != manifest['gold']['entries']:
            raise ValueError('gold commitments changed')
        # Score only completed captures. A partial attempt remains recorded and cannot be
        # made complete by silently dropping absent predictions.
        result = {'disclosure': DISCLOSURE, 'candidate': runtime['candidate'],
                  'metrics': score(args.capture, args.cases, args.gold),
                  'capture_receipt_sha256': digest(args.capture / 'receipt.json')}
        if seal.gold_entries(args.gold) != (gold_digest, entries):
            raise ValueError('gold changed during scoring')
        output.mkdir(mode=0o700)
        common.write_json(output / 'summary.json', result)
        common.write_json(output / 'receipt.json', {'files': files(output)})
    print(json.dumps({f'{split}_{name}': value for split, metrics in result['metrics'].items()
                      for name, value in metrics.items() if type(value) in (int, float)},
                     sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
