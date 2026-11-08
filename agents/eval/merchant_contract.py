"""Diagnose merchant mapping and issuer pinning without changing or trusting inputs.

Refusals can be intentional adversarial cases; counts are diagnostics, not a pass/fail
accuracy score. No record values, identifiers, exception messages or gold are exported.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

from agents.data.api import load_case
from alibi.connectors.case_source import KIND_MAP, MappingError, map_merchant_event
from alibi.connectors.keys import CONNECTOR_KEYS, kid_for_issuer
from alibi.eval import common, seal
from tools.lockbox import is_open


def summarize(cases):
    counts = Counter(cases=0, merchant_events=0, mapped=0, mapping_refused=0,
                     unknown_kind=0, issuer_pin_mismatch=0, matching_issuer_pin=0,
                     cases_without_usable_merchant_event=0)
    for case in cases:
        counts['cases'] += 1
        usable = 0
        entries = [*case.get('merchant_deliveries', []), *case.get('merchant_lookup', [])]
        for entry in entries:
            counts['merchant_events'] += 1
            event = entry.get('event') if isinstance(entry, dict) else None
            if not isinstance(event, dict) or event.get('kind') not in KIND_MAP:
                counts['unknown_kind'] += 1
            try:
                body = map_merchant_event(event).body
            except (MappingError, TypeError, KeyError, ValueError):
                counts['mapping_refused'] += 1
                continue
            counts['mapped'] += 1
            kid = kid_for_issuer(body.get('issuer_id'))
            if CONNECTOR_KEYS[kid][1] != body.get('issuer_id'):
                counts['issuer_pin_mismatch'] += 1
            else:
                counts['matching_issuer_pin'] += 1
                usable += 1
        counts['cases_without_usable_merchant_event'] += not usable
    return dict(counts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reused-cases', type=Path)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args(argv)
    if args.reused_cases:
        if not is_open() or not args.manifest:
            parser.error('reused-input diagnosis needs results.py lockbox reason and original manifest')
        entries = seal.case_entries(args.reused_cases)[1]
        if entries != common.load_json(args.manifest)['cases']['entries']:
            raise ValueError('input commitments differ')
        cases = common.load_sealed_cases(args.reused_cases, require_gold=False)
    else:
        cases = [load_case(n) for n in range(1, 81)]
    result = summarize(cases)
    if args.reused_cases and entries != seal.case_entries(args.reused_cases)[1]:
        raise ValueError('input commitments changed')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
