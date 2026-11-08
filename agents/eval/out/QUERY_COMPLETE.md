# Complete payout-query integration after public73

Apply this increment after `PUBLIC_ACCURACY_BUNDLE.patch` (SHA256 `c8fa7625f00bdd88852d783519dc33d7270fdb5ab55737e91e421a871a39310f`) plus `GAP_DIAGNOSTICS_COMPAT.patch`, or after the equivalent `PUBLIC_ACCURACY_INCREMENT` on native `1e871aa`. This is against that fixed public73 tree, **not moving main**. Do not also apply the separate query implementation/safety artifacts. Existing frozen and candidate-5 captures remain unchanged.

Includes the complete connector-backed query implementation and QUERY_SAFETY: scoped order/date lookup, signature verification, active-revision query rechecks, atomic answer ingestion, and private message/order/payout revision dependencies on success, empty results and connector failure. API/one-pass signature overlaps were resolved by preserving both `holder_claim` and `payout_query` forwarding. Batch allocation and existing text dependencies are retained.

Also fixes agent retrieval telemetry: merchant-backed covering/date actions mark their Material lookup entry `connector_query`; those entries count like connector retrievals, while passport-only fallbacks and refused/budget/skipped actions keep their previous counts. The focused agent-action test asserts two executed mock query calls count twice and an out-of-material refusal adds no call. Existing material/fallback controls pass.

Validation: **155 focused tests passed** (7.73 seconds): query safety/implementation, text safety, unresolved dependencies, gateway, API claim parity, agent foundation/material/actions and adapter. No full suite was repeated. Clean application to the fixed73 source succeeds; applying to an independent baseline copy produces the tested output bytes exactly.

Actual public80 with unchanged v2 scorer and every94RFC retained:

| Mode | Safe-complete | Runtime failures | Forbidden | Unnecessary |
| --- | ---: | ---: | ---: | ---: |
| rules | **75/80** | 0 | 0 | 0 |
| one-pass, empty FakeClient proposal | **50/80** | 0 | 0 | 12 |

Rules gains only42 and43; remaining rules failures39,40,45,46,47. FakeClient is an offline control, not a live-model result. These are unregistered public development diagnostics until root captures the committed patch stack through the formal rehearsal. No private inputs/gold, real model calls, scorer/gold edits, trust-policy relaxation or shared native edits.

Patch SHA256: `e54593eec6730b8e941f59edf91b64a2590af2c367ee001d9271b65daf5eec22`.
Tested source and retained public diagnostics: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-query-complete-jl40dus8` (`.baseline/public-results`).
Clean apply byte check: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-query-apply-check-r828fitj`.

Exact inputs:
- `PUBLIC_PAYOUT_QUERY_AFTER_ANSWER.patch`: `9dc63d673412196a932bf59461a36fddc0ec2474c39baa518c097ea343ed5876`
- `QUERY_SAFETY.patch`: `113c4c8e3ae74b99621f8e026135afded8862364655a803ce9a813bbc90c91a9`

The agent telemetry edit is additional to those two inputs. Neither ONEPASS_GAPS nor optional REFERENCE_FORMATS is included; root owns any later integration.

```sh
uv run --frozen python -m pytest -q alibi/tests/test_query_safety.py alibi/tests/test_payout_query.py alibi/tests/test_text_lookup_safety.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_gateway.py alibi/tests/test_api_claim_parity.py alibi/tests/test_agents_foundation.py alibi/tests/test_adapter.py
```
