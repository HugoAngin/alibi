# Analyst disposition flow

Owner: `execution`. Source: `docs/SPEC.md` (Bank verification and analyst console, Verification and receipt, Consent and signing, demo 2:25–2:45). Security probes are in [`probes.json`](probes.json), rendered in [`PROBES.md`](PROBES.md).

The analyst is a human at `bank_a` or `bank_b`. The console never decides for them: no account is cleared, restricted or escalated automatically. All data is synthetic.

## States

```
received ──verify──▶ rejected            (terminal; reason shown, nothing displayed as evidence)
    │
    └──────────────▶ verified ──open──▶ in_review ──decide──▶ cleared
                                             │                escalated
                                             │                more_requested ──▶ follow-up RFC (new request_id, parent = this one)
                                             └── package expires before decision ──▶ expired (history kept, no new acceptance)
```

A disposition is appended, never edited. A later disposition on the same package (for example after a follow-up answer) is a new entry that references the earlier one.

## 1. Receive and verify (code, before any human sees evidence)

- [ ] Caller authenticated as one recipient. Inbox shows only that recipient's packages (SP-17, SP-20).
- [ ] Receipt bound to `package_id` and SHA-256 digest of the received bytes. Identical bytes again → existing result. Same ID, different bytes → rejected (SP-31).
- [ ] Schema valid for the package version.
- [ ] `audience` equals the authenticated recipient (SP-26).
- [ ] Not past `expires_at` (SP-27).
- [ ] Alibi package signature valid: Ed25519 over JCS, package signing domain, key ID in the pinned demo trust registry (SP-25, SP-28).
- [ ] Every attachment that *claims* issuer provenance has a valid issuer signature from a pinned, purpose-scoped merchant key. One failure blocks acceptance of the whole package (SP-29).
- [ ] `self_supplied` attachments without a signature are accepted and labelled at lower trust (SP-30).
- [ ] Redacted extracts are labelled "attested by Alibi package key", not as issuer-signed.
- [ ] Outcome written to the receipt record and audit log: actor, time, `package_id`, `request_id`, action, outcome, reason.

If any check fails the package is `rejected`, the reason is shown, and its contents are not displayed as evidence.

## 2. Review screen (what the analyst sees)

Three panels kept visually separate. Passing one never implies the others.

| Panel | Shows | Never implies |
| --- | --- | --- |
| **Package authentication** | Signature valid, key ID, audience, issued and expiry times, digest | That any claim is true |
| **Source trust** | Per attachment: `issuer_verified`, `bank_observed` or `self_supplied`, with issuer, record ID and revision | That the claim it supports is supported |
| **Claim status** | Per RFC question: `supported`, `contradicted` or `unresolved`, with rationale and evidence refs | Anything about trust or authentication |

- [ ] Evidence grouped by the RFC's question IDs, in the question's own wording.
- [ ] Each claim opens a trace: question → claim → source ID and revision → calculation → final status. No model inputs, no private holder records, no exclusion list (SP-15).
- [ ] Reconciliation recomputed **by the verifier** from the cited amounts in pence, not read from the package. Canonical case: `240000 − 18000 − 6000 = 216000`. A mismatch shows both numbers.
- [ ] Settlement timing shown: payout reference, release-to-balance time, bank-settlement time and the credit's booking time. Platform-balance release is labelled as distinct from bank settlement.
- [ ] Contradictions listed with the conflicting sources side by side.
- [ ] Gaps listed generically ("settlement record not provided"), never naming a withheld merchant or category (SP-13).
- [ ] Superseded evidence: if Maya's passport later receives a correction, the delivered package is not altered. Any follow-up shows the newer revision as a new package.

## 3. Disposition (human decision)

- [ ] Choices: **clear**, **escalate**, **request more**. No default selected, no auto-submit.
- [ ] Requires analyst identity (from the credential) and a short reason.
- [ ] Recorded fields:

```json
{
  "disposition_id": "disp_…",
  "package_id": "pkg_…",
  "package_digest": "sha256:…",
  "request_id": "rfc_…",
  "recipient": "bank_a",
  "analyst": "analyst_a1",
  "decision": "clear | escalate | request_more",
  "reason": "free text, bank side only",
  "question_ids": ["q1"],
  "requested_evidence": ["settlement record for payout <ref>"],
  "decided_at": "2026-11-08T10:00:00Z",
  "supersedes": null
}
```

- [ ] Disposition is visible to the analyst's own bank only. It is not sent to Maya automatically, and it is never an input to an automated account decision.
- [ ] Audit entry: actor, time, `request_id`, `package_id`, action `disposition`, decision.
- [ ] `clear` and `escalate` are terminal for that package. A new package needs a new disposition.

## 4. Request more → follow-up

- [ ] Creates a new RFC through the bank connection, with a **new `request_id`** and `parent_request_id` set to the original.
- [ ] Scope is structural: explicit `txn_ids` or period, question IDs and stated questions, expiry. Free text in the question cannot widen scope (SP-39).
- [ ] Follow-up runs the normal pipeline: investigation, filtered preview, **fresh holder approval**. The earlier approval cannot be reused (SP-04).
- [ ] New package delivered to the same recipient only; both packages and both dispositions appear in that recipient's history and in Maya's sharing history.

## 5. Two recipients (portability)

- [ ] `bank_a` and `bank_b` run the same console code with different credentials.
- [ ] Each sees only its own inbox, packages, attachments, receipts and dispositions (SP-17 to SP-20).
- [ ] Neither sees the other's package or Maya's private exclusion list.
- [ ] Bank B's response reuses existing source IDs (no re-import) but has its own request ID, audience, evidence selection, approval and delivery record.

## Demo cut (2:25–2:45)

1. Bank A inbox shows one received package.
2. Verify: authentication ✓, then source trust per attachment, then claim status per question.
3. Open the £2,160 claim trace; verifier's own reconciliation matches.
4. Analyst chooses **request more** for the settlement record, or **clear** in the supported control. No account state changes.
5. Optional live refusal: Bank A fetches Bank B's package → refused (SP-18).

## Open questions for lead

- Which build session owns the console UI vs the verifier code in `alibi/`. This checklist is the acceptance list for whoever does.
- ~~Whether the disposition record above should be pinned in `shared/CONTRACTS.md`.~~ Pinned by lead in f67902c (Disposition row). That row wins if the two differ.
