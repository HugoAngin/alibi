"""Load explicitly authored synthetic issuer setup, never inferred from case payloads."""
from hashlib import sha256
import json
from pathlib import Path
import re


def load(path: Path, expected_sha256: str):
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('trusted setup commitment changed')
    value = json.loads(raw)
    if value.get('schema_version') != 'alibi.eval.synthetic_issuer_setup.v1':
        raise ValueError('unknown trusted synthetic setup schema')
    issuers = value.get('trusted_issuers')
    seed = value.get('synthetic_key_seed')
    fingerprints = value.get('public_key_fingerprints')
    if (not isinstance(issuers, dict) or not issuers
            or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', k)
                   or not isinstance(v, str) or not v for k, v in issuers.items())
            or not isinstance(seed, str) or not re.fullmatch(r'[0-9a-f]{64}', seed)
            or not isinstance(fingerprints, dict) or not set(issuers) <= set(fingerprints)
            or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v)
                   for v in fingerprints.values())):
        raise ValueError('invalid trusted synthetic setup')
    # Authoring source paths and any incidental fields never reach the candidate.
    return dict(trusted_issuers=dict(issuers), synthetic_key_seed=seed,
                public_key_fingerprints=dict(fingerprints))


BOOTSTRAP = '''import sys,json,os,hashlib
from contextlib import redirect_stdout
sys.path.insert(0,sys.argv[1])
from alibi.adapter import run_case
from alibi.connectors.keys import connector_keystore
case=json.load(sys.stdin)
with open(sys.argv[3]) as f:
    setup=json.load(f)
with redirect_stdout(sys.stderr):
    keys=connector_keystore(os.path.join(os.environ['ALIBI_DATA_DIR'],'trust-preflight'),
        trusted_issuers=setup['trusted_issuers'],seed=setup['synthetic_key_seed'])
    actual={kid:hashlib.sha256(bytes.fromhex(keys.public_key_hex(kid))).hexdigest()
            for kid in sorted(keys.specs)}
    if actual != setup['public_key_fingerprints']:
        raise ValueError('trusted public-key commitment mismatch')
    rows=run_case(case,mode=sys.argv[2],trusted_issuers=setup['trusted_issuers'],
                  synthetic_key_seed=setup['synthetic_key_seed'])
print(json.dumps(rows,allow_nan=False))
'''
