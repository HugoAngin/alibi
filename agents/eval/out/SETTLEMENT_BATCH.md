# Verified settlement batch support

Snapshot of the first retrieval-worker patch, confined to issuer-verified payouts explicitly sharing the settlement reference carried by the bank credit. Apply SETTLEMENT_BATCH.patch, then BATCH_DEPENDENCIES.patch. They must be reviewed and applied together: the follow-on prevents reuse of a secondary batch member for another credit and retains private correction dependencies even when component arithmetic is incomplete or wrong.

The combined focused rules, batch and safety suites pass119 tests. Four new safety regressions fail without the dependency/allocation follow-on and pass with it. Source signatures, subject/issuer scopes, ambiguity refusal and bank-specific private IDs remain protected. Do not sum unrelated same-amount payouts, or add self-supplied records to issuer-verified batches.

A disposable public80 rules run with the batch snapshot and earlier pending-gap patch returned47safe-complete versus43before batch support, with0forbidden items. Formal hash-bound recording follows the safety patch integration; this is development evidence, not the candidate5 capture or fresh unseen accuracy.
