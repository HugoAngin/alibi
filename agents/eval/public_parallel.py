"""Run the six public gate jobs with the canonical isolated capture scheduler."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

from agents.data.api import load_case
from agents.eval.run_sealed import _parallel_capture, score_saved
from agents.eval.second_look import public_gate
from tools.lockbox import is_open


def capture(*, candidate, private_root, credential, output, deadline, trusted_file=None):
    if is_open():
        raise ValueError('public gate requires closed lockbox')
    private_root = private_root.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(private_root) or deadline <= datetime.now(timezone.utc):
        raise ValueError('public output must be separate and deadline in the future')
    trust_digest = None
    if trusted_file is not None:
        from agents.eval.trusted_setup import load
        trust_digest = sha256(trusted_file.read_bytes()).hexdigest()
        load(trusted_file, trust_digest)
    from alibi.llm.client import load_api_key
    load_api_key(credential)
    os.umask(0o077)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = [load_case(n) for n in (1, 2)]
    isolation = dict(candidate=candidate, private_root=private_root, credential=credential,
                     workers=4, deadline=deadline, capture_root=output.parent,
                     trusted_issuers_file=trusted_file, trusted_issuers_sha256=trust_digest)
    _parallel_capture(output, cases, isolation)
    workers = [json.loads(p.read_text()) for p in sorted(
        (output / 'capture-workers').glob('*/metadata.json'))]
    (output / 'run.json').write_text(json.dumps(dict(kind='public-live', candidate=candidate,
        restriction_variant=False, workers=workers), indent=2))
    (output / 'probes.jsonl').write_text('')
    public_gate(output, candidate, trusted_issuers_sha256=trust_digest)
    return score_saved(output, cases, [], kind='public_live')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    for name in ('private-root', 'credential', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--trusted-issuers-file', type=Path)
    parser.add_argument('--deadline', required=True)
    args = parser.parse_args()
    candidate = subprocess.check_output(['git', 'rev-parse', args.candidate + '^{commit}'],
                                        text=True).strip()
    deadline = datetime.fromisoformat(args.deadline)
    if deadline.tzinfo is None:
        parser.error('deadline must include a timezone')
    result = capture(candidate=candidate, private_root=args.private_root,
        credential=args.credential, output=args.out, deadline=deadline,
        trusted_file=args.trusted_issuers_file)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
