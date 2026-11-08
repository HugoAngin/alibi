# Consent matrix

Owner: `execution`. Expected backend behaviour for every way an approval can stop being valid, then the HTTP flows that exercise it. Source: `docs/SPEC.md` (Consent and signing), `shared/CONTRACTS.md` (Approval, Disposition, RFC scope), probes SP-01 to SP-09 in [`PROBES.md`](PROBES.md). All data is synthetic.

**Routes are provisional.** The S2/S5 route contract was not on `main` or the board when this was written (lead's 00:21Z post queues it for Arnav). Holder and bank routes below are the form actions in the S4 template contract (`alibi/app/UI.md` on `s4a`). Routes marked ⚠ are placeholders until S2 publishes them. Swap the table in [Routes](#routes) and nothing else changes.

Canonical IDs (data case 1): subject `maya-001`, credit `txn-0001-004` (216000 GBP pence), `rfc-0001-01` (bank_a, `txn_ids: [txn-0001-004]`, `period: null`), `rfc-0001-02` (bank_b, October 2026 period, `txn_ids: []`). The follow-up below is `rfc-0001-03` with `parent_request_id: rfc-0001-01`.

## What an approval is bound to

An approval record stores: SHA-256 of the canonical (JCS) package body, `audience`, `request_id`, `policy_version`, the `(record_id, revision)` of every cited source, `valid_from`, `valid_until`, approving holder. Dispatch recomputes all of them **at dispatch time**, under the same lock as revision updates (SP-08), and refuses on the first mismatch. Order of checks, so the reason is deterministic:

1. approval exists for this `request_id` → `approval_missing`
2. request not expired → `request_expired`
3. approval inside its window → `approval_expired`
4. `request_id` matches → `approval_request_mismatch`
5. `audience` matches the request's authenticated audience → `approval_audience_mismatch`
6. `policy_version` is current → `policy_version_changed`
7. cited source revisions are current → `source_revision_changed`
8. body digest matches → `approval_digest_mismatch`

## Matrix

HTTP status for every refusal is `409 Conflict` with a JSON body `{code, reason, expected_digest?, actual_digest?}`. The dispatch-result page prints `reason` verbatim (S4 `dispatch_refused`). Each refusal leaves the approval `invalidated` with `invalidated_reason = code`. Nothing reaches any bank inbox.

| # | Trigger | Setup | Request | Code | `reason` text (verbatim) | Audit entry `action` / `outcome` / `detail` | Probe |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | No approval | Draft ready, never approved | dispatch `rfc-0001-01` | `approval_missing` | `dispatch refused: no holder approval recorded for request rfc-0001-01.` | `dispatch.refused` / `refused` / `approval_missing` | SP-01 |
| C2 | Body edit: item removed | Approve, then remove an optional item | dispatch | `approval_digest_mismatch` | `dispatch refused: approved digest sha256:<a12> does not match body digest sha256:<b12>; the body changed after approval (<item_id> removed). Review and approve the revised body.` | `dispatch.refused` / `refused` / `approval_digest_mismatch: body changed after approval.` | SP-02 |
| C3 | Body edit: draft regenerated | Approve, rerun the investigation (same evidence, new rationale text) | dispatch | `approval_digest_mismatch` | as C2, with `(draft regenerated)` | as C2 | SP-02 |
| C4 | Recipient change | Approval for `rfc-0001-01` (bank_a), dispatch the same body addressed to bank_b | dispatch with `audience=bank_b` | `approval_audience_mismatch` | `dispatch refused: approval is for bank_a; this dispatch is addressed to bank_b. Each recipient needs its own approval.` | `dispatch.refused` / `refused` / `approval_audience_mismatch` | SP-03 |
| C5 | Audience forged at approval | POST approve with `audience=bank_b` for an `rfc-0001-01` body | approve | `approval_audience_mismatch` (on approve, not dispatch) | `approval refused: request rfc-0001-01 is addressed to bank_a, not bank_b.` | `approval.refused` / `refused` / `audience_mismatch` | SP-20 |
| C6 | Stale preview | Preview rendered with digest A; body changes; Maya clicks Approve (posts digest A) | approve | `approval_digest_not_current` | `approval refused: the preview you approved (sha256:<a12>) is no longer the current draft (sha256:<b12>). Review the current draft.` | `approval.refused` / `refused` / `digest_not_current` | SP-02 |
| C7 | Policy change | Approve; confirm a new rule or retire one (policy v3 → v4) | dispatch | `policy_version_changed` | `dispatch refused: privacy rules changed after approval (v3 to v4). The draft was re-filtered; review and approve it again.` | `approval.invalidated` / `ok` / `policy v3 -> v4`, then `dispatch.refused` / `refused` / `policy_version_changed` | SP-07 |
| C8 | Relevant source revision | Approve; merchant sends a correction to a cited record (e.g. `rec-0001-021` r1 → r2) | dispatch | `source_revision_changed` | `dispatch refused: cited record rec-0001-021 changed from r1 to r2 after approval. The draft is stale; review the updated draft.` | `record.corrected` / `ok` / `… 1 draft invalidated`, `approval.invalidated` / `ok` / `source_revision_changed`, then `dispatch.refused` | SP-06 |
| C9 | Unrelated source revision (control) | Approve; correction arrives for a record **not** cited in this body | dispatch | none, **200** | — | `dispatch.sent` / `ok` | SP-06 |
| C10 | Approval expiry | Approve; wait past `valid_until` (15 min in the demo; a test clock is fine) | dispatch | `approval_expired` | `dispatch refused: approval expired at <valid_until>. Review and approve again.` | `dispatch.refused` / `refused` / `approval_expired` | SP-05 |
| C11 | Request expiry | RFC `expires_at` passes before dispatch | dispatch | `request_expired` | `dispatch refused: request rfc-0001-01 expired at <expires_at>. The bank must send a new request.` | `dispatch.refused` / `refused` / `request_expired` | SP-05 |
| C12 | Reuse on second request | Approval for `rfc-0001-01`; dispatch for `rfc-0001-02` (bank_b) | dispatch `rfc-0001-02` | `approval_missing` | `dispatch refused: no holder approval recorded for request rfc-0001-02.` (approvals are looked up by request ID, so the other one is never found) | `dispatch.refused` / `refused` / `approval_missing` | SP-03 |
| C13 | Reuse on follow-up | Approval for `rfc-0001-01`; dispatch for follow-up `rfc-0001-03` | dispatch `rfc-0001-03` | `approval_missing` | as C12 with `rfc-0001-03` | as C12 | SP-04 |
| C14 | Retry, identical bytes (control) | Dispatch succeeded; network retry with the same `package_id` | dispatch | none, **200** with the original receipt | — | `dispatch.deduplicated` / `ok` / `same package_id and digest` (no second `dispatch.sent`, one sharing-history row) | SP-09 |
| C15 | Retry, different bytes | Same `package_id`, different body | dispatch | `package_id_reused` | `dispatch refused: package <package_id> was already sent with a different digest.` | `dispatch.refused` / `refused` / `package_id_reused` | SP-09 |
| C16 | Valid (control) | Approve the current draft, dispatch inside the window | dispatch | none, **200** | — | `approval.recorded` / `ok` / `Approved digest sha256:<a12> for bank_a, valid 15 min.`, then `dispatch.sent` / `ok` / `Approval revalidated; body signed with alibi-demo-2026.` | SP-32 |

Notes:

- `<a12>` and `<b12>` are the first 12 hex characters of a digest, matching the S4 sample's style.
- C9 matters as much as C8: invalidating on *every* correction would make the demo's reapproval meaningless. Only sources cited in the body count.
- After any refusal, the next approve must post the **current** digest. Re-posting the old one returns C6.
- Audit entries carry `actor`, `role`, `at`, `request_id`, `event_id` (approval, package or event ID), `action`, `outcome`, `detail`, matching the S4 audit sample. Details never contain body content, only IDs, codes and digest prefixes.

## Flows

Actors: `maya` (holder session), `bank_a` / `bank_b` (recipient credentials), `analyst_a1`. Each step: actor, method, path, body, expected status and the key fields to assert.

### F1. Tampered edit refused, then fresh approval (demo 1:50–2:25)

| # | Actor | Request | Expect |
| --- | --- | --- | --- |
| 1 | bank_a | ⚠ `POST /connect/bank/rfcs` body `rfc-0001-01` | `201`, audit `rfc.received` |
| 2 | maya | `POST /requests/rfc-0001-01/run` | `200`, request status `draft` |
| 3 | maya | `GET /requests/rfc-0001-01` | `review.approval.digest` = D1, `status: pending` |
| 4 | maya | `POST /requests/rfc-0001-01/approve` `digest=D1&audience=bank_a&policy_version=v3` | `200`, `approval.status: approved`, audit `approval.recorded` |
| 5 | maya | `POST <included[i].remove_action>` (remove one optional item) | `200`, new digest D2 |
| 6 | maya | `POST /requests/rfc-0001-01/dispatch` | `409`, `code: approval_digest_mismatch`, `expected_digest` D1, `actual_digest` D2 (C2) |
| 7 | bank_a | `GET /bank/bank_a/inbox` | `200`, no new package |
| 8 | maya | `POST /requests/rfc-0001-01/approve` `digest=D2&audience=bank_a&policy_version=v3` | `200` |
| 9 | maya | `POST /requests/rfc-0001-01/dispatch` | `200`, receipt with `package_id` P1 and digest D2, audit `dispatch.sent` |
| 10 | bank_a | `GET /bank/bank_a/packages/P1` | `200`, `authentication.status: valid` |

### F2. Request more → follow-up RFC → fresh consent

Continues from F1 step 10.

| # | Actor | Request | Expect |
| --- | --- | --- | --- |
| 1 | analyst_a1 | `POST /bank/bank_a/packages/P1/disposition` `disposition=request_more&note=Need the settlement record for PO-0001-01` | `200`, disposition stored with `package_digest` D2; `analyst` from the credential, not the form; audit `disposition.recorded` |
| 2 | analyst_a1 | `POST /bank/bank_a/requests` `parent_request_id=rfc-0001-01&period_from=…&period_to=…&question=Provide the settlement record for payout PO-0001-01.` | `201`, new `request_id` `rfc-0001-03`, `parent_request_id: rfc-0001-01`, `audience: bank_a`. Scope is the posted fields only (SP-39) |
| 3 | system | delivery of `rfc-0001-03` through the bank connection | audit `rfc.received` for `rfc-0001-03` with parent |
| 4 | maya | `GET /passport` | requests list shows `rfc-0001-03` as `new`, linked to its parent |
| 5 | maya | `POST /requests/rfc-0001-03/run` | `200`, draft D3. **No new import**: source record count and IDs equal those before step 1 |
| 6 | maya | `POST /requests/rfc-0001-03/dispatch` (before approving) | `409`, `approval_missing` for `rfc-0001-03` (C13). The `rfc-0001-01` approval is not reused |
| 7 | maya | `POST /requests/rfc-0001-03/approve` `digest=D3&audience=bank_a&policy_version=v3` | `200` |
| 8 | maya | `POST /requests/rfc-0001-03/dispatch` | `200`, package P2 |
| 9 | bank_a | `GET /bank/bank_a/inbox` | P1 and P2; `requests_sent` shows `rfc-0001-03` with parent `rfc-0001-01` |
| 10 | analyst_a1 | `POST /bank/bank_a/packages/P2/disposition` `disposition=clear&note=…` | `200`. P1's `request_more` disposition is unchanged (append-only) |
| 11 | analyst_a1 | `POST /bank/bank_a/packages/P2/disposition` again | `409`, `disposition_final` (clear is terminal) |
| 12 | maya | `GET /passport` | sharing history has two bank_a rows (P1, P2) with distinct request IDs |

### F3. Second recipient reuse (demo 2:45–3:10)

| # | Actor | Request | Expect |
| --- | --- | --- | --- |
| 1 | bank_b | ⚠ `POST /connect/bank/rfcs` body `rfc-0001-02` (October period) | `201` |
| 2 | maya | `POST /requests/rfc-0001-02/run` | `200`, draft D4; source IDs reused, no import |
| 3 | maya | `POST /requests/rfc-0001-02/dispatch` | `409`, `approval_missing` (C12) |
| 4 | maya | `POST /requests/rfc-0001-02/approve` `digest=D2&audience=bank_b&policy_version=v3` (bank_a's digest) | `409`, `approval_digest_not_current` (C6) |
| 5 | maya | `POST /requests/rfc-0001-02/approve` `digest=D4&audience=bank_b&policy_version=v3` | `200` |
| 6 | maya | `POST /requests/rfc-0001-02/dispatch` | `200`, package P3 |
| 7 | bank_b | `GET /bank/bank_b/inbox` | P3 only |
| 8 | bank_b | `GET /bank/bank_b/packages/P1` | `404` (no existence leak), audit `access.refused` (SP-18) |
| 9 | bank_a | `GET /bank/bank_a/inbox` | P1, P2 only |

## Routes

| Use | Path | Source |
| --- | --- | --- |
| Run investigation | `POST /requests/<request_id>/run` | S4 `investigation.run_action` (exact path set by S4) |
| Remove item | `POST <review.included[].remove_action>` | S4 |
| Approve | `POST /requests/<request_id>/approve`, fields `digest`, `audience`, `policy_version` | S4 sample |
| Dispatch | `POST /requests/<request_id>/dispatch` | S4 `retry_action` sample |
| Bank inbox | `GET /bank/<recipient>/inbox` | FRONTEND.md |
| Package | `GET /bank/<recipient>/packages/<package_id>` | S4 sample |
| Disposition | `POST /bank/<recipient>/packages/<package_id>/disposition`, fields `disposition`, `analyst_name`, `note` | S4 sample |
| Follow-up | `POST /bank/<recipient>/requests`, fields `parent_request_id`, `period_from`, `period_to`, `question` | S4 sample |
| RFC intake | ⚠ `POST /connect/bank/rfcs` | placeholder until S2 |
| Merchant correction | ⚠ `POST /connect/merchant/events` | placeholder until S2 |

Open for lead: the follow-up form only carries a period, but `rfc-0001-01` is transaction-scoped. Per the RFC scope row, the follow-up form needs a `txn_ids` option or must inherit the parent's `txn_ids` when the period is left empty.
