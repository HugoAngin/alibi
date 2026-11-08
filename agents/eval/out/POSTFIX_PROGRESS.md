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
- Evaluator fixes: bounded worker cleanup, retained private stderr, fixed-code failure
  diagnostics, native import preflight, and corrected wrapper-field default reporting.
- Remaining app work: unresolved-answer evidence minimisation, one-pass question handling,
  intermittent cross-bank approval scoping, and a demo smoke port-release assertion.

`POSTFIX_REUSED_RUNTIME.json` exports aggregate counts only. The complete new capture is
a separate private sibling of the frozen capture, with artifact hashes. Replaying the
record launches no workers. The original 67 returned groups remain byte-for-byte preserved;
the prior authorised continuation remains separate from this development diagnostic.
