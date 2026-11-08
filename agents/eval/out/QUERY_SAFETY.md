# Payout query safety follow-on

Apply after the new payout-query implementation and `TEXT_LOOKUP_SAFETY` (the latter is already in the public accuracy bundle). Neither worker source nor shared native files were edited. This is a separate safety patch; it does not replace the existing public73 bundle or record.

- Revalidate the active returned payout's covered-record list or occurred/effective UTC day after stale/duplicate ingestion selects a newer stored revision. A matching stale signed response cannot return an active revision outside its query. Matching newer revisions still return normally.
- Wrap ingestion of the entire query answer in the store's existing re-entrant transaction. Each signed ingest uses a nested savepoint; any later signature or active-scope refusal rolls back all earlier answer writes, including source revisions and their transaction-scoped invalidations. The gateway refusal trace remains outside the rolled-back ingestion. No signature checking is bypassed.
- Before every chosen clue query, retain its signed message revision and, for covering queries, the signed order revision used to translate the order number. Retain accepted returned payout revisions too. These join `text_dependencies`, which the previous safety patch merges into private `Claim.examined_refs`. This applies on success, empty answers and connector failure, without adding clue attachments or exposing IDs in recipient bodies.

Validation: the twelve new controls give **10 failures and 2 passes** against the copied query implementation plus the existing text safety prerequisite. With this follow-on, all **52 focused tests pass** (query safety/query implementation/text safety/unresolved dependencies/gateway). They cover both stale query keys, matching active positive controls, a valid new insertion or revision followed by a bad signature (neither survives), and public42/43 clue/order corrections invalidating claims, drafts and approvals after successful, empty and failed queries. No models, private inputs/gold/captures, scorer or trust-policy changes.

```sh
uv run --frozen python -m pytest -q alibi/tests/test_query_safety.py alibi/tests/test_payout_query.py alibi/tests/test_text_lookup_safety.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_gateway.py
```

Patch SHA256: `113c4c8e3ae74b99621f8e026135afded8862364655a803ce9a813bbc90c91a9`.
Tested tree: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-query-safety-8uxz1mgb`.
Prerequisite source hashes:
- `alibi/gateway.py`: `e66a6154ebd4c6f870c7353aea2e088e2948f5b8e37e5869d1459720a93c2143`
- `alibi/core/investigate_rules.py`: `24a07aecb5cca23d983cf08e33edd0322922b6116c5126ea4dc9ee3a32a1b602`
