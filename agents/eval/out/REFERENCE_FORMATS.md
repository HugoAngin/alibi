# Exact issuer reference fields

Optional public-derived robustness fix after full retrieval integration. Preserves the literal spaced reference in an explicit issuer field; no separator rewriting, prefix guessing, or gateway authorization changes. The public80 result is unchanged. A public73-derived test fails before and passes after; adjacent tests also preserve refusal for unsigned holder text. Independent worker reports13 focused passes. Not yet integrated with the final73/80 bundle or new payout query; keep separate until combined tests pass.

Cases46/47 remain unresolved: the exact provider contract does not define slash/bare-token equivalence. Accepting slash transport alone does not establish equality with the provider key.
