# One-pass: settlement after the booking is unresolved (staged, not applied)

`ONE_PASS_TIMING.patch` changes `alibi/baselines/one_pass.py` (+7/−2, conflict routing only) and adds `alibi/tests/test_one_pass_timing.py`. Apply it with `patch -p1` from the repo root, after `CLASSIFICATION.patch` and then `ONE_PASS_LOOKUP.patch`. `ONE_PASS_LOOKUP.patch` is unchanged. Nothing is committed. No shared file, schema, API, store, private input or gold is touched. Public cases only.

## Semantics used

- `agents/data/README.md:79`: `q.source_of_funds` is `supported` when the credit is linked, **otherwise `unresolved` with a gap code**.
- `q.breakdown` takes the claim status.
- `alibi/eval/predictions.md:37` lists `settlement_after_credit` as an unresolved gap code.
- `shared/CONTRACTS.md:15` says `contradicted` means "evidence conflicts with the claim". A settlement that takes effect after the booking cannot fund the credit, and it does not show the source wrong at booking either.

## Bug (public Case 19, `balance_vs_settlement/settles_after_credit`)

- **Gold** for `rfc-0019-01`: both answers `unresolved` / `settlement_after_credit`.
- **One-pass** after classification + lookup: `q.source_of_funds` is `contradicted`, with gap null and 4+ citations. This happens because `CandidateResult.conflicts` routes every explicit conflict to `contradicted()`, including a payout effective 2026-06-14 after a 2026-06-11 booking.

## Change

In the conflict branch, when `_conflict_code(conflict, txn) == settlement_after_credit`, the conflict is routed to the existing `undecided()` with `replace(conflict, code=GAP_SETTLEMENT_AFTER)`. The result is unresolved, keeping the timing reason in the rationale.

Failed, pending and amount-mismatch conflicts still go to `contradicted()`. There are no id literals, no case checks, and no new lookups.

| Case 19, `FakeClient` (`claims: []`) | Before | After |
|---|---|---|
| source of funds | contradicted, gap null | **unresolved, settlement_after_credit**, no citations |
| breakdown | unresolved, settlement_after_credit | same |
| lookups | 1 | 1 |

## Tests (scratch `git archive` of HEAD + the three patches)

- `test_public_case19_settlement_after_booking_is_unresolved_not_contradicted`: fails before the fix, passes after.
- `test_holder_uploaded_message_naming_a_reference_triggers_no_new_lookup`: a holder import copying Case 19's release message but naming `PO-0019-77` leaves the lookup count and every status and gap unchanged.
- `test_one_pass_timing`, `test_one_pass_lookup` and `test_baselines_one_pass`: 26 passed.
- **Pre-existing, not caused by this patch:** `test_one_pass_classification.py::test_one_pass_breakdown_of_a_contradicted_credit_is_unresolved_and_cites_nothing` fails with classification + lookup alone. The breakdown still carries a calculation. It probably depends on the examined_refs / unresolved-evidence change.
- `UNRESOLVED_EVIDENCE.patch` (Astra's examined_refs) dry-runs cleanly on top of all three patches. That combined stack was not test-run.

## Not changed, and uncertain

- The rules engine (`investigate_rules._conflict`) also contradicts a settlement effective after the booking. That is outside this patch.
- Public Cases 34, 38, 66 and 75 are contradicted in rules but unresolved in gold. Whether the shared rule ("source is never contradicted") should also cover failed, pending and amount-mismatch conflicts in one-pass needs the owner's decision. This patch covers only settlement-after-booking, as asked.
