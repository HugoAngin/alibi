Exact reference casing: public synthetic generalization fix

Base: supplied finalpublic75 scratch /var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-query-complete-jl40dus8.
Own working copy: /tmp/new-evidence-generalization-work.
No committed candidate SHA is asserted for this composed scratch; BASE_SHA256.txt pins the exact owning source file before this patch.

Bug:
fetch_by_credit_reference uppercases every explicit bank reference and every extracted description token before calling the provider. Gateway/connector lookup and returned-reference validation use exact strings. A legitimate lowercase or mixed-case reference therefore cannot retrieve its matching record. The issuer-text fallback skips the same reference because its uppercase form already appears on the bank credit, so it does not recover the missed lookup.

Reproduction:
Public case1 with embedded gold removed. Rename subject, record IDs, transaction IDs and request IDs. Replace the payout reference consistently across observed evidence and provider record with pay-Ab9-701, wire-z7-9, or TrNs-712. Scale all monetary rows and holder stated amounts by 1, 3, or 2. Replace standard source/breakdown question IDs with arbitrary IDs and use semantically equivalent wording. All three fail before: unresolved source despite consistent signed settlement evidence and exact matching provider key.

Fix:
Send explicit txn.refs and description reference tokens using exactly their observed casing. Keep the two-query bound, existing known-reference comparison, source availability and gateway authorization. Require returned payout.payment_ref to equal the exact query. No reference prefix invention, separator normalization, issuer-specific logic, hidden IDs, scorer edits or new source-trust allowance. Description tokens are attempted in their observed order after explicit refs; prior implementation sorted uppercase description tokens. Parent owns broader regression checks.

Tests:
Before: three failures (all new variant controls), with no infrastructure failures.
After: 32 passed in 2.65 seconds: five new tests plus existing gateway and one-pass classification tests. All three scaled/renamed variants now have supported source/breakdown, the exact literal payout reference in facts and the correct scaled net amount. Description-only mixed-case variant also passes. A bank reference with different casing from the provider's literal value stays unresolved: the patch does not guess case variants. Existing gateway checks cover foreign subject, self-supplied and wrong issuer behavior.

Command from working copy:
UV_PROJECT_ENVIRONMENT=/Users/tamim/code/agentverse_wtt/.venv uv run --frozen --no-sync python -m pytest -q alibi/tests/test_literal_reference_generalization.py alibi/tests/test_gateway.py alibi/tests/test_one_pass_classification.py

Artifacts:
LITERAL_REFERENCE.patch: minimal incremental lead-owned rules change + native-path tests.
test_literal_reference_generalization.py: standalone copy of new tests.
before-tests.log / after-tests.log: failing-before and passing-after evidence.
BASE_SHA256.txt: exact base source file hash.

Scope and limits:
Rules-mode integration and shared gateway/classification guards only. No live Gemini or private/heldout evidence accessed. No 80-case rerun; coordinator owns the supplied 75-case result and integration regression. No claim about unseen benchmark improvement without its separate post-hoc run. Issuer identity was not mutated in these tests; subject/record/transaction/request identities, reference case, amounts and question wording were varied. Shared source and supplied scratch remained unchanged. No Git commands or process modifications.
