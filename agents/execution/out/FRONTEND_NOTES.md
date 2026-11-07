# Frontend notes: analyst screens

Owner: `execution`. Phase 1 review (docs/FRONTEND.md) of `analyst/package.html` and `analyst/inbox.html` on `s4a` at 6466584, against [`ANALYST_FLOW.md`](ANALYST_FLOW.md) and [`PROBES.md`](PROBES.md). Rendered with `alibi.app.preview_render`; `pytest alibi` 22 passed as baseline. No template edits made; Phase 2 edits wait for the S4 merge (~00:45).

Who acts: **T** = template (research, Elio), **C** = stylesheet (data, Hugo), **L** = route, variable or sample data (`need` to lead). Ordered by value to the judges.

## Must fix

### 1. Rejected packages still show their contents as evidence — package.html, inbox.html — T
ANALYST_FLOW §1 and probes SP-25 to SP-29: if verification fails, nothing in the package is shown as evidence. Today the page renders claim status, evidence, reconciliation and the disposition form whatever `authentication.status` is.

- package.html: when `authentication.status != "valid"`, render the summary and the Package authentication panel, then one refusal block (`banner banner--error`-style, not a toast) saying "Package rejected: <status>. Its contents are not shown as evidence." with the failed rows of `authentication.checks`. Skip `#source-trust`, `#claim-status`, `#evidence`, `#reconciliation`, `#contradictions`, `#gaps`, `#disposition`, `#followup`.
- `replayed` is not a failure (SP-31: identical bytes return the existing result). Treat it like `valid` and add one line "Duplicate receipt; showing the original result."
- inbox.html: for packages whose `authentication` is not `valid`, show "—" in the Claims column instead of the three counts.

Uses existing variables only. Tests: the render tests may assert those section ids exist; keep the valid-package path unchanged.

### 2. The three verification panels look identical — package.html, alibi.css — T + C
FRONTEND.md item 3: judges check that a valid signature is not shown as "everything is true". On screen, all three panels have the same border, the same title style and the same green pills, so "valid" and "pass" read as one big green tick.

- T: add a modifier per panel: `panel--auth`, `panel--trust`, `panel--claims`. Give each panel title a one-line question under it:
  - Package authentication: "Is this the package Alibi signed for us?"
  - Source trust: "Who vouches for each attachment?"
  - Claim status: "Does the evidence support each answer?"
- C: make them look different in grayscale too, e.g. auth = solid 2px top rule, trust = left rule, claims = no fill with double border. Do not use the claim-badge colours for authentication `pass`; give `status_badge("pass")` a neutral outline style in the auth panel so it cannot be read as "supported".
- T: move the sentence "These three are independent…" from muted small text under the grid to the top of `#checks`, normal size.

### 3. Redacted extract badged `issuer_verified` — preview sample — L
`ctx_analyst_package` gives `att_03` (`ext_recon_01`, "Alibi redacted extract") `trust: issuer_verified`. SPEC (Consent and signing 4) and SP-14: a redacted extract is attested by the **package signer**, not the issuer. Badging it issuer-verified overstates trust in front of judges.

- L: either a fourth display value for the extract (e.g. `trust: "package_attested"` with its own badge) or a separate field `form: full_record | redacted_extract` on `source_trust` and `evidence` items so the template can print "Redacted extract, attested by Alibi" beside an issuer badge for the underlying records. Pick one and add it to UI.md; until then fix the sample so it does not claim `issuer_verified` for the extract.
- T (after L): render the label; C: badge style if a new trust value is added.

### 4. Analyst identity is a free-text field — package.html — T + L
ANALYST_FLOW §3 and the pinned Disposition row: `analyst` comes from the credential. The form has an editable `analyst_name` input, so anyone can record a disposition as anyone.

- T: show the name read-only (`readonly` on the input, keep `name="analyst_name"` so the route does not break).
- L: the route must take the analyst from the authenticated session and ignore the posted value.

### 5. Disposition reason is optional and named `note` — package.html — T + L
Pinned contract field is `reason` and ANALYST_FLOW requires it.

- T: add `required` to `#disposition-note` and relabel it "Reason (required)". Keep `name="note"`.
- L: map `note` to `reason` in the stored record, or rename the field in UI.md and the route together.

### 6. Terminal dispositions still show the form — package.html — T
Pinned contract: `clear` and `escalate` are terminal for that package. When `disposition_form.current.disposition` is `clear` or `escalate`, hide the form and the follow-up form and show "Final for this package. A new package needs a new disposition." When it is `request_more`, keep the follow-up form visible and highlighted.

## Should fix

### 7. Settlement timing is not visible — package.html — L then T
ANALYST_FLOW §2: show payout reference, release-to-balance time, bank-settlement time and the credit's booking time. This is the core of the demo's balance-versus-settlement point. It is only inside free-text `description` and `summary` strings today.

- L: add `timing` to each `evidence_by_question` item: `None | {payout_ref, released_to_balance_at, bank_settled_at, credit_booked_at, settled_before_booking: bool}`.
- T: render it as a four-row kv under the evidence table, with `settled_before_booking` as a pass or fail badge.

### 8. `request_more` and the follow-up form are not connected — package.html — L
The pinned record has `question_ids` and `requested_evidence`; neither form collects them. An analyst can choose "Request more" and never send a follow-up, or send one with no disposition.

- L: either one combined action (choosing `request_more` reveals the follow-up fields and one POST records both) or the follow-up route records a `request_more` disposition. Add `question_ids` (checkboxes from `claim_status`) to whichever form posts it.
- T: plain-JS disclosure toggle only, per FRONTEND.md.

### 9. "Clear" label can be read as clearing the account — sample data — L
SPEC: no account is cleared automatically. Option labels: "Clear (no further questions)", "Escalate (internal review)", "Request more (sends a follow-up to the holder)". The note under the button already says the disposition is a human record; move it above the button.

### 10. Follow-up scope — route — L
SP-39: the question text must not widen scope. The form is fine (period fields carry the scope). The route should validate `period_from < period_to`, both inside the parent request's subject, and store the question text as data only.

## Fine as is
- Inbox sentence that other recipients' packages cannot be listed or fetched by ID (SP-17, SP-18). The backend still has to enforce it.
- Reconciliation shows sign, sources, pence and "Recomputed by verifier"; difference row with match badge.
- Gaps use a reason code and generic wording ("Evidence unavailable, not withheld").
- Consent note on the follow-up form.
- Audit page is holder-side (`area="holder"`), so the gateway's injected-instruction refusal is not shown to banks (SP-15).

## Not reviewed
Holder screens, the export print view and phone width. Analyst screens were checked at desktop width only.
