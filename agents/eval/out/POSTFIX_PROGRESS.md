# Post-freeze fixes and diagnostics

Tamim explicitly requested continuing fixes after the lead closed the frozen evaluation.
All original predictions, input commitments and negative frozen scores remain intact.
Reusing the held-out inputs for diagnosis makes subsequent results post-hoc development
evidence, not a new unseen comparison.

- App import resilience `a3be52a`: invalid self-supplied imports are refused and skipped,
  allowing the remaining investigation to continue. No input kinds are silently coerced.
- Public regression: all 80 normal public rules cases and all four formerly crashing
  import variants completed without worker failure. Public rules accuracy remains 35/80;
  the fix has not demonstrated a public accuracy improvement. Comparison `6d9da33c2f` PASS.
- Reused held-out runtime diagnostic: all 44 rules cases completed without worker failure,
  versus 10/44 in the original frozen rules run. Zero scheduler errors and model calls.
  Record `09a90f6cd0` PASS. No gold was scored in this runtime diagnostic.
- Separate scoring of those retained predictions, record `db5a3f9c28` PASS, still gives
  0/32 main and 0/12 adversarial safe-complete. This used gold only in the evaluator after
  all workers had ended, made no model calls, and is explicitly another post-hoc look.
- Evaluator fixes: bounded worker cleanup, retained private stderr, fixed-code failure
  diagnostics, native import preflight, and corrected wrapper-field default reporting.
- Native merchant contract diagnostic `35cb3feab7` PASS finds all 450 merchant events in
  the reused pack map successfully but mismatch the connector's selected issuer key pin.
  All 44 cases therefore lack usable merchant evidence through this connector. The check
  reads no gold, exports no record values and never changes issuer identities or trust.
  A native explicit trusted-fixture configuration patch is in preparation; arbitrary
  event-declared issuers must not become trusted automatically.
- `ISSUER_CONFIG.patch` now supplies that explicit native API. All 108 native tests pass,
  including the existing public gold checks. An independent synthetic bootstrap check
  verifies the same fixed issuer mapping and signing seed reach all three modes, rejects
  mismatched fingerprints before candidate execution and restores the three public control
  claims. The private configuration comes from the original authoring setup's static
  issuer declaration, with provenance and fingerprints; no event/gold-derived trust.
- The unresolved-evidence prototype improves public rules safe-complete from 35 to 41/80,
  but review found that it drops stale-evidence dependencies. It is being strengthened
  before application; this provisional number is not a submitted or verified result.
- Remaining app work: unresolved-answer evidence minimisation, one-pass question handling,
  same-clock draft ordering and a demo smoke port-release assertion. Cross-bank scoping
  landed at `354f9e0`; all 36 API tests pass. The separate ordering issue remains.

`POSTFIX_REUSED_RUNTIME.json` exports aggregate counts only. The complete new capture is
a separate private sibling of the frozen capture, with artifact hashes. Replaying the
record launches no workers. The original 67 returned groups remain byte-for-byte preserved;
the prior authorised continuation remains separate from this development diagnostic.
