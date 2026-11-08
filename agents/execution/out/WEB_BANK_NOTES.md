# Bank analyst console in `web/apps/bank`: build list

Owner: `execution`, for research (Elio). Updates [`FRONTEND_NOTES.md`](FRONTEND_NOTES.md) (written against the Jinja templates at 23:35) for the Astro console, using what has been pinned since: `shared/CONTRACTS.md` (Trust attestation, Disposition, RFC scope, Refusals) and `agents.lead.api.verify_package` output. Reviewed `web/apps/bank` at `0920c2a`: case overview and request screen exist; the package view is still to build. Probe IDs refer to [`PROBES.md`](PROBES.md).

## Must

1. **A rejected package shows no evidence** (SP-25 to SP-29).
   - If `authentication.valid` is false, or the status is not `valid`, show the summary, the authentication panel and one refusal block listing `authentication.reasons` verbatim.
   - Then stop: no trust panel, claims, evidence, reconciliation, disposition or follow-up.
   - In the inbox, a rejected package shows "—" instead of claim counts.
   - The tamper demo produces this exact case: reasons "body_sha256 does not match the package body" and "Alibi package signature: signature does not verify" (`out/TAMPER_DEMO.md`).
2. **Three panels that look different** (judges check that a valid signature is not shown as "all true").
   - Use one title and one question per panel:
     - **Package authentication:** "Is this the package Alibi signed for us?"
     - **Source trust:** "Who vouches for each attachment?"
     - **Claim status:** "Does the evidence support each answer?"
   - Use a different border treatment per panel that survives grayscale, and a neutral style for authentication "pass", never the green "supported" style.
   - Put the sentence "These are independent: a valid package can carry low-trust sources and unresolved claims" above the panels, at normal size.
3. **Redacted extracts are labelled as such.**
   - For each `sources[]` item with `form: redacted_extract` or `attestation: package_signer`, show "Redacted extract, attested by Alibi" beside the underlying trust badge.
   - Show `lower_trust: true` items visibly lower.
   - Never badge an extract as issuer-signed (CONTRACTS, Trust row).
4. **Analyst identity comes from the login.**
   - Show the name read-only.
   - Do not send a name the server would trust: the backend takes `analyst` from the credential (Disposition row).
5. **A reason is required for every disposition.**
   - Label the field "Reason (required)"; it maps to the pinned field `reason`.
   - Label the options "Clear (no further questions)", "Escalate (internal review)" and "Request more (sends a follow-up to the customer)".
   - Put "This records a human decision. It does not change the customer's account." above the button.
6. **Clear and escalate are final.**
   - When the current disposition is `clear` or `escalate`, hide both forms and show "Final for this package."
   - If the backend refuses anyway, show its `disposition_final` reason verbatim.
7. **A follow-up keeps the parent's scope** (RFC scope row, `consent_matrix.md` F2).
   - Default to the parent's `txn_ids` (case 1: `txn-0001-004`), offering the same two scope choices as intake.
   - Show that widening needs a new request, and display the backend's `scope_wider_than_parent` refusal verbatim.
8. **The request screen must not decide the audience** (SP-20, SP-24).
   - `request.astro` posts a hidden `audience=bank_a`. Either drop it, or the backend must ignore it and set the audience from the bank credential.
   - Keep questions as the fixed ID list with no free text that could widen scope (SP-39). This is already right.
9. **Refusals are blocks, not toasts.**
   - Every 4xx shows `{code, reason}` with the reason verbatim (CONTRACTS, Refusals row).
   - Never write our own wording over the backend's.

## Should

10. **Settlement timing.**
    - The analyst context now carries `timing: {payout_ref, released_to_balance_at, bank_settled_at, credit_booked_at, settled_before_booking}` (`agents.eval.api.build_analyst_preview`).
    - Render the four times as rows, with `settled_before_booking` as a pass or fail badge, and label "released to balance" as distinct from "settled to bank".
    - This is the demo's core point.
11. **Reconciliation shows the verifier recomputed it.** Use `claims[].calculation_valid` from `verify_package`, not the package's own total, and say "Recomputed by the bank's verifier".
12. **Isolation stays server-side.**
    - The console only lists what the backend returns for this bank's credential.
    - A package ID from elsewhere gets the backend's 404, shown as "Not found" with no hint the package exists (SP-18).
    - Never add a client-side recipient switch that changes what is requested (SP-20).
13. **Gaps stay generic.** Show the backend's gap text as is; it never names a withheld merchant or category (SP-13).

## Already resolved

- Changing a package after approval is now refused in the core (`41ff999`, consent matrix C2, C6), so the live refusal moment works end to end.
- The disposition record and follow-up scope are pinned in `shared/CONTRACTS.md`.

## Optional for the demo

- A "Verify a package file" box on the bank console, calling the backend's `verify_package`, would let the presenter do the tamper demo in the bank UI instead of the terminal (`out/TAMPER_DEMO.md`).
