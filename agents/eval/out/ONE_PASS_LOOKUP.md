# One-pass lookup of references named in issuer messages (staged, not applied)

`ONE_PASS_LOOKUP.patch` changes `alibi/baselines/one_pass.py` (`fixed_lookups`) and adds `alibi/tests/test_one_pass_lookup.py`. Apply it with `patch -p1` from the repo root, after `CLASSIFICATION.patch` and `UNRESOLVED_EVIDENCE.patch`. Native files remain unchanged, and no shared file, schema, API or store is touched. Public cases only.

## Cause

`fixed_lookups` only looked up message tokens equal to a payout's `payment_ref` that the passport already holds. In the team's cases the payout is lookup-only (Case 1 `rec-0001-021`), so the message "Withdrawal PO-0001-01 ... has been requested" led to 0 lookups and `no_merchant_records`.

## Change (one block)

The existing stored-payout matching is unchanged. In addition, for each **issuer-verified** message only (in material order), the patch collects reference-shaped tokens written in the text:

- 4+ characters of letters, digits or hyphens;
- at least one letter **and** one digit, so dates, amounts and plain words never count;
- not already matched to a stored payout;
- capped at `core_rules.MAX_REFERENCE_LOOKUPS` (2).

These join the same predeclared lookup loop. The strategy is the same as before:

- only references named in messages are looked up;
- every lookup still goes through `gateway.lookup_settlement`, which checks scope and signer;
- nothing a lookup returns triggers a further lookup.

There are no id literals and no verdict, gold or status logic changes.

## Public Case 1, `FakeClient` (`claims: []`), adapter `run_case(1, "one_pass")`

| Claim | Before (classification patch only) | After |
| --- | --- | --- |
| `rfc-0001-01` lookups | 0 | 1 (`PO-0001-01`) |
| `q.source_of_funds` | unresolved, `no_merchant_records`, `{}` | supported, `{payout_references: [PO-0001-01], settled_at: [2026-10-27T16:00:00Z]}` |
| `q.breakdown` | unresolved, `no_merchant_records`, `{}` | supported, `breakdown_minor {sales 240000, refunds 18000, fees 6000, net 216000}` |
| `q.sales_refunds_in_period` | supported, `{sales 240000/8, refunds 18000/2}` | unchanged |

All three now equal public gold. Case 2 stays unresolved, now with `settlement_pending_at_booking` (gold's gap) instead of `no_merchant_records`.

## Public 80 cases (offline `FakeClient`), status agreement with embedded public gold

| | source | breakdown | period |
| --- | --- | --- | --- |
| classification patch only | 50/80 | 49/80 | 14/14 |
| + this patch | **60/80** | **60/80** | 14/14 |
| rules (reference) | 58/80 | 60/80 | 14/14 |

44 claims changed:

- 22 become supported, and all 22 match public gold on status and facts.
- The 10 Case 2-style unresolved claims (2, 20, 52, 58, 59) now carry gold's gap.
- Injection cases 51-60 make ≤2 lookups each, and every source status equals gold. Cases 51 and 53 also look up an injected token such as `MAYA-002`; it returns nothing usable.
- Maya fixtures with no reference in the message still make 0 lookups (the existing `test_maya_message_names_no_reference_so_zero_lookups_and_unresolved` passes).

## Tests (`uv run --frozen`, `ALIBI_DATA_DIR` set to a temp dir)

```
uv run --frozen python -m pytest -q alibi/tests/test_one_pass_lookup.py alibi/tests/test_one_pass_classification.py alibi/tests/test_baselines_one_pass.py
```

- New file on the classification-only base: 4 failed.
- With this patch: 4 passed.
- With the patch applied after `classify-combined.patch`: the new tests plus classification, one-pass, adapter, facts, runner, investigate_rules and core-fix suites give **185 passed**.
- Full suite not rerun.

## Limitations

- **Case 19** (`balance_vs_settlement/settles_after_credit`): source becomes `contradicted`, while gold says unresolved `settlement_after_credit`. Before this patch it was unresolved `no_merchant_records`, which matched on status only.
  - The verdict comes from one-pass's existing candidate rule (a settlement effective after the booking is a conflict) on a genuinely retrieved, issuer-signed payout.
  - This patch leaves that rule alone; it needs an owner decision.
- **Cases 41, 48, 49 and 50** (hidden reference) and **Case 44** stay unresolved with more specific gaps (`reference_not_carried`, `settlement_not_found`), while gold says supported. They are not regressions.
- **Self-supplied messages** never trigger the new lookups, but there is no dedicated test for that guard.
- **Post-hoc result:** this changes one-pass after the frozen run. Label any rerun as public-only and post-hoc; the original unseen result is unchanged.
