Integration: apply after the immutable SETTLEMENT_BATCH.patch snapshot (same initial retrieval batch implementation). Do not apply both SETTLEMENT_BATCH and a later retrieval patch that already contains it.

# Batch dependency and allocation safety follow-on

Apply after `SETTLEMENT_BATCH.patch` and the existing `UNRESOLVED_EVIDENCE` native changes. This patch does not edit the retrieval artifact, scorer, data, trust pins or consent checks. No models or private cases were used.

The batch implementation discarded component refs when a member's sum failed or another covered component was absent. It also checked exclusive allocation using only the primary payout. The follow-on retains the relevant, actually examined component revisions before either error path, and checks every member in a linked batch's persisted `candidates` for competing current or earlier credits. Only rules reference links treat multiple candidates as allocated batch members. Historical agent links can retain unchosen alternatives, so other link methods preserve their previous primary-only allocation behavior; a fifth regression guards this compatibility. Ambiguous links reserve no allocation. Other recipients' transaction IDs remain private; the refusal counts previous credits without naming them.

Validation: all four new public Case 9 regression tests **fail against the unmodified retrieval patch** and **pass with this follow-on**. They exercise wrong-sum and missing-component corrections invalidating unresolved claims, drafts and approvals while keeping attachments empty, secondary-member reuse in one RFC, and secondary-member reuse in a later Bank B RFC without leaking Bank A's transaction ID. The new tests plus settlement-batch, core-investigation, unresolved-dependency and rules suites: **120 passed**, no failures or xfails (4.32 seconds).

Scope: rules batch behavior and the rules allocator's reading of persisted links. This does not add batch support or new allocation logic to the other investigators. New arrival of a previously missing record still requires reinvestigation; this patch tracks revisions of existing examined records. Source-versus-breakdown semantics are intentionally outside this safety patch.

Patch SHA256: `663d2c643c785a55c118a52256c07be138f71a74dc1be99abaf2b5460fc93270`.
Disposable tested tree: `/var/folders/h5/vyz923lx1t7_ll1y6mb_9xbm0000gn/T/alibi-batch-safety-82lvylk3`.

Reproduction from an archive with the prerequisite patches applied:

```sh
uv run --frozen python -m pytest -q alibi/tests/test_batch_dependencies.py alibi/tests/test_settlement_batch.py alibi/tests/test_core_investigation_fixes.py alibi/tests/test_unresolved_dependencies.py alibi/tests/test_investigate_rules.py
```
