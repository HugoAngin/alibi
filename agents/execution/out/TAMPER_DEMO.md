# Tamper demo: the live control moment

Owner: `execution`. About 30 seconds on stage, inside demo slot 1:50–2:25 or 2:25–2:45. It shows that a package changed after signing is rejected by the bank's verifier, using the real `alibi` signing and verifier (`agents.lead.api.sign_and_package`, `verify_package`). All data and keys are synthetic.

## Run it

```bash
uv run --frozen python -m agents.execution.tamper_demo
```

It builds Maya's supported package in a throwaway store under `data/alibi/tamper_demo/` (git-ignored), then runs three checks. Last line: `{"tamper_demo_ok": 1, ...}`. Exit code 1 if any check goes the wrong way.

To tamper with a package the app exported on stage instead:

```bash
uv run --frozen python -m agents.execution.tamper_demo --file path/to/exported.json
```

That verifies the file, writes `<name>_tampered.json` with the first `216000` changed to `261000`, and verifies the copy. It uses the pinned registry in `data/alibi/keys/`.

## What to flip, exactly

In the exported JSON, the first occurrence of the bytes `216000` is the credit amount inside a signed attachment (`"amount_pence":216000`). In the reference build that is **byte offset 4589 to 4594**. The offset is stable across runs; the package hash is not, because the demo keys are generated fresh each run. Change `216000` to `261000`: same length, so nothing else in the file moves.

By hand, in any editor: search for `216000` and change it to `261000`, then save and verify again.

## Output from the reference run (2026-11-08)

```
package: data/alibi/tamper_demo/package.json  sha256 61bd87fffb25fe1a  11741 bytes

1. Bank A verifies the exported package: ACCEPTED
  authentication valid: True
  sources checked: 9 (1 at lower trust)
  claims: q1 supported

2. Tamper: bytes 4589..4594  216000 -> 261000
   context: ...ontent":{"amount_pence":216000,"curr...

   Bank A verifies the tampered package: REJECTED
  authentication valid: False
  reason: body_sha256 does not match the package body
  reason: Alibi package signature: signature does not verify

3. Same package re-addressed and sent to Bank B: REJECTED
  authentication valid: False
  reason: body_sha256 does not match the package body
  reason: Alibi package signature: signature does not verify
```

## What to say

1. "This is the exact package Bank A received. The verifier accepts it, and it shows trust per source separately: one source is at lower trust."
2. "Now I change £2,160 to £2,610 in the file, six bytes." Show the edit.
3. "Verify again: rejected. The body no longer matches its digest, and Alibi's signature fails. The bank never sees the altered numbers as evidence."
4. Optional: "And if someone forwards Bank A's package to Bank B, Bank B rejects it too."

If asked: the signature proves the package is intact and came from Alibi's key. It does not prove the explanation is true. That is why source trust and claim status are shown separately (judge Q&A 2 and 3).

## Related

- Probes SP-25 (tampering) and SP-26 (wrong recipient): `out/PROBES.md`, with the local reference run in `out/verifier_reference_run.json`.
- Consent refusal (the other live control, C2): `out/consent_matrix.md` flow F1.
