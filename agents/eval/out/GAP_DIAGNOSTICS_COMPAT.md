# Gap diagnostic test compatibility

Apply after `GAP_DIAGNOSTICS.patch` or the public accuracy bundle/increment. Test-only change: the adapter's between-RFC delivery test must expect `balance_release_not_settlement` when the only available issuer evidence is its existing balance-release message. Source status remains unresolved. Every check for clock boundaries, one lookup, unseen IDs, observed-input counts and later-record visibility remains unchanged.

The full native combined suite found this stale assertion. The complete adapter suite passes after the test correction: **21 passed** (3.39 seconds). No native behavior, scorer, fixtures or gold changed. Original bundle/increment artifacts and their public73 capture are untouched.

Patch SHA256: `7a68c8a417867ad85039f96874790acfb11ea3ade5ce77e8fb03e629f058a5ba`.
