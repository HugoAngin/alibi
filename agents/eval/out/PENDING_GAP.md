# Pending payout gap precision

Staged native patch; apply after UNRESOLVED_EVIDENCE.patch. When a matched payout is already known to be pending but no later settlement record is available, the rules engine now reports settlement_pending_at_booking rather than the generic settlement_not_found. The answer stays unresolved: the existing detail explicitly says no settlement was found. This does not assert the initial pending status persisted, accept a foreign issuer, or turn an unknown status into pending. Supported and contradicted paths are unchanged.

Two focused regressions cover pending versus unknown payout states with no lookup, empty disclosure and retained private dependencies. Three existing gap assertions are updated for the more precise known-pending diagnostic; their unresolved status, foreign-issuer refusal and missing-settlement assertions remain. Combined rules and core-fix suites:104 passed.

Exploratory public80 rules scoring in a disposable combined patch tree improves41 to43 safe-complete, with0forbidden and47unnecessary items. This count is development evidence pending formal results.py registration; no hidden inputs or gold changes. It fixes only a subset of pending cases: cases with no matched payout still need proper reference retrieval.
