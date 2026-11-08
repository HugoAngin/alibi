# Demo script: four minutes on case 1, then case 2

Owner: data (Hugo). Source: `docs/SPEC.md` "Four-minute demo" and "Acceptance criteria", `docs/FRONTEND.md`, `alibi/app/UI.md` on `s4a`, and the canonical fixture `agents.data.api.canonical_case()` (case 1, pinned in `shared/CONTRACTS.md`). Case 2 is the contradiction variant; its step-by-step expected states are in `case2_walkthrough.md`.

**Status when written (2026-11-08 ~01:00Z, main 5107aca).** No routes are wired on `main` yet. The URLs below are the ones `docs/FRONTEND.md` names. Button labels come from `UI.md`. Anything marked **[verify]** has to be checked against the S4/S5 build before rehearsal. Lead fills those in or tells data what to change.

## Roles

| Role | Who (lead confirms) | Does |
| --- | --- | --- |
| Presenter | Arnav (lead) | Speaks every line below, faces the judges, never touches the keyboard. |
| Driver | Hugo (data) | Clicks, keeps the phone-width holder window and the analyst window side by side, and triggers the live arrivals. |
| Backup | Elio (research) | Has the replay (`ALIBI_REPLAY=1`) started in a second terminal and calls the switch. Owns the results slide at 3:10. |

## Before going on stage (T−10 min)

1. Start everything: `uv run --frozen python -m alibi.demo --timeout 600`. This runs the merchant mock, the bank mock and the app on `http://127.0.0.1:8100` with Maya loaded. **[verify the S5 command]**
2. Check the starting state. It must match what 0:00 needs:
   - Maya's history is present: 8 sales (`rec-0001-001` … `rec-0001-017`, 5–13 Oct), 2 refunds (`rec-0001-005`, `rec-0001-012`), 8 fees, the eyewear purchase `rec-0001-019`, and the rent, grocery and pharmacy debits (`txn-0001-001` … `-003`).
   - The £2,160.00 credit `txn-0001-004` (booked 2026-10-28 06:30Z, reference `PO-0001-01`) is in the bank feed.
   - The balance release `rec-0001-020` is **not** delivered yet, and the payout `rec-0001-021` is lookup-only.
   - No RFC yet.
   - **[verify]** how the build seeds "history up to, but not including, `rec-0001-020`", and the command that pushes it live.
3. Gemini check: `uv run --frozen python -m alibi.llm.probe` (on `s3a`) **[verify]**. If it fails, or if latency goes over about 10 s, start the replay instead (see Fallback) and tell the Presenter before going on.
4. Open three windows:
   - Holder: `/passport`, at 390 px width (phone).
   - Bank A: `/bank/bank_a/inbox`.
   - Bank B: `/bank/bank_b/inbox`, a background tab.
5. Have the results slide ready on the deck laptop (3:10).

## The run

| Time | Window, URL | Driver does | Must be on screen | Presenter says |
| --- | --- | --- | --- | --- |
| 0:00–0:25 | Holder `/passport` | Nothing. Scroll once to Alerts. | Holder Maya, `maya-001`. Connections: mock marketplace (issuer verified), bank feed (bank observed), imports (self-supplied). The history above. An **alert that exists before any request**: unexplained credit, £2,160.00 on 28 Oct, with no settlement record linked. Requests: none. | "This is Maya. She resells clothes, and her bank has restricted her account over a £2,160 credit. Rent is due. Alibi has been keeping her evidence in the background, before anyone asked. It has already noticed that this credit has no settlement record behind it." |
| 0:25–0:45 | Holder `/txn/txn-0001-004` | Trigger the merchant webhook for `rec-0001-020` **[verify command]**, then open the transaction. | A new linked source: `rec-0001-020` r1, kind `balance_release`, issuer verified. Its message: "£2,160.00 from your recent sales is now available in your seller balance. Withdrawal PO-0001-01 … has been requested." Reconciliation computed by code: `240000 − 18000 − 6000 = 216000`, £2,160.00. Link state still **unmatched**, or flagged settlement discrepancy. | "A signed message just arrived from the marketplace. The arithmetic works: £2,400 of sales, less £180 of refunds and £60 of fees, is exactly £2,160. But read the message. The money went to her seller balance. That is not the same as reaching her bank." |
| 0:45–1:25 | Holder `/requests/rfc-0001-01` | Have the bank mock send Bank A's RFC **[verify trigger]**, open it, and click **Run investigation**. | Request `rfc-0001-01` from Bank A, audience `bank_a`, `txn_ids: [txn-0001-004]`, expires 2026-11-09 09:00Z. Questions: `q.source_of_funds` ("What is the origin of the incoming payment of £2,160.00 booked on 2026-10-28?") and `q.breakdown` ("Please show how this amount was calculated."). Roles table: Matcher, Detective, Challenger, Curator. The Detective's candidate may start as **Unresolved**, which is acceptable (SPEC). The Challenger notes that release to balance is not settlement and calls `merchant lookup payout_reference=PO-0001-01`. | "Bank A asks where the money came from. The Detective proposes an explanation. The Challenger refuses to accept a matching amount as proof. It sees the release names withdrawal PO-0001-01, and asks the marketplace for that payout's settlement record." |
| 1:25–1:50 | Same page, `#trace` | Scroll to the trace and open the `q.source_of_funds` step. | Lookup result: `rec-0001-021` r1, payout `PO-0001-01`, **settled 2026-10-27 16:00Z**, before the credit was booked at 2026-10-28 06:30Z. The reference matches the credit. Link state is **linked**. `q.source_of_funds` is **Supported**, and so is `q.breakdown`. The trace chain reads: question → claim → sources (`txn-0001-004` r1 bank observed, `rec-0001-021` r1 issuer verified, plus the covered sales, refunds and fees) → lookup → calculation `240000 − 18000 − 6000 = 216000` → status. | "Here is the trail a bank analyst can check. The settlement record says the payout reached the bank the afternoon before the credit was booked, under the same reference. The arithmetic is recomputed by code from the cited records, not by the model. Only now is the claim supported." |
| 1:50–2:25 | Same page, `#review`, then the dispatch page | 1. Show the three groups. 2. Click **Approve for Bank A**. 3. Click **Remove** on one optional included item (an extract or the self-supplied item, if offered). 4. Send **[verify where the send button lives]**. The refusal page appears. 5. Click **Review and approve the revised body**, then **Approve for Bank A**, then send again. | Included: the credit `txn-0001-004` and payout `rec-0001-021`, plus whatever the breakdown needs. Excluded, with reasons: rent `txn-0001-001`, grocery `-002`, pharmacy `-003`, the eyewear purchase `rec-0001-019`. The approval box names **Bank A**, with the digest and validity window shown. After the edit: **Dispatch refused**, code `approval digest mismatch`, and the backend reason verbatim. After re-approval: **Delivered to Bank A**. | "Maya sees exactly what Bank A will receive, and what is left out and why. Her rent, her groceries and a pharmacy purchase stay private. She approves. Now watch: change one item after approval, and the backend refuses to send, because the approved digest no longer matches the body. She reviews the new version and approves again." |
| 2:25–2:45 | Bank A `/bank/bank_a/inbox`, then the package | Open the package. Scroll through the three panels. Under Disposition, pick **clear**, type a note and submit. | Three separate panels: **Package authentication** valid (signature, digest, recipient binding, expiry, replay); **Source trust** for each attachment (issuer verified, bank observed); **Claim status** Supported for both questions. The disposition is recorded by a named analyst. | "Bank A verifies the package. A valid signature only proves who sent it and that it was not altered. Trust in each source, and whether each claim is supported, are shown separately. A human analyst records the decision. Nothing clears the account automatically." |
| 2:45–3:10 | Holder `/requests/rfc-0001-02`, then Bank B `/bank/bank_b/inbox` | Have the bank mock send Bank B's RFC **[verify]**. Click **Run investigation**, then **Approve for Bank B**, then send. Switch to the Bank B inbox. Then, in the Bank A window, paste Bank B's package URL. | `rfc-0001-02`, audience `bank_b`, period `[2026-10-01, 2026-11-01)`, `q.sales_refunds_in_period`. Supported: sales £2,400.00 (8), refunds £180.00 (2). The sources are the **same IDs** (`rec-0001-001` …, `rec-0001-005`, `rec-0001-012`), with no new import. A new selection: no payout, no fees, no Bank A package. A fresh approval for **Bank B**. Sharing history shows two separate entries. Bank B's inbox shows it; Bank A's does not. Bank A fetching Bank B's package is **refused** by the backend. | "A second bank asks a different question: her marketplace sales and refunds in October. Same passport, same record IDs, nothing re-imported. A new, narrower package, and a fresh approval for Bank B. And Bank A cannot open Bank B's package, even with the link." |
| 3:10–4:00 | Deck: results slide | Switch to the deck. | Eval's frozen numbers: Alibi against the rules baseline and one-pass Gemini on the sealed cases. Denominators, intervals, cost and one failure. | "On cases we never saw while building, …" (read the eval numbers as they are). Close: "Reusable evidence, controlled disclosure. Whether real banks would accept this is unmeasured." |

## Case 2 (contradiction variant)

Use case 2 when the judges ask "what if the settlement doesn't check out?", or as a second run in rehearsal. The path is identical to the one above up to 1:25. Then:

- The Challenger's lookup of `PO-0002-01` returns `rec-0002-021` with **status pending**, no `settled_at`, and **still pending as of 2026-10-29 06:30Z**, a day after the credit was booked.
- The settlement claim is **Contradicted**. The credit's origin (`q.source_of_funds`) and `q.breakdown` stay **Unresolved**, with gap `settlement_pending_at_booking`. The amounts still reconcile on screen.
- Bank A's package carries the gap, not a supported answer. The analyst picks **request more**.
- Bank B's period answer is unaffected.

Every expected state is listed in `case2_walkthrough.md`.

> Presenter line at the lookup: "This time the marketplace says the transfer was still pending a day after the money arrived. The amounts still add up, but they cannot be the explanation. Alibi marks the settlement claim contradicted and leaves the source unresolved. It refuses to tell the bank something the evidence does not support."

## Fallback: Gemini down or slow

- **Decide by T−5 min** (Backup calls it). If the probe fails, or one live investigation takes over about 10 s in rehearsal, use the replay.
- Start: `ALIBI_REPLAY=1 uv run --frozen python -m alibi.demo --timeout 600` **[verify]**. Every page then shows the **REPLAY** banner (`banner--replay`). Do not hide it.
- The script, clicks and expected screens stay the same. Only the agent output is recorded.
- Presenter adds one sentence at 0:45: "Gemini is not reachable from this room, so this is a clearly labelled recorded run of the same flow. The backend checks, refusals and signatures are running live." Only say the last part if it is true in replay mode **[verify which parts stay live in replay]**.
- If the app itself fails, play the recorded demo video (owner: Elio, from the deck capture).
- Never present replay output as live.

## Things that must not happen on stage

- Do not call the claim "verified" or "true". Say **supported**, **contradicted** or **unresolved**, as the badge says.
- Do not say the bank accepted anything or that the account was cleared. The disposition is a human record.
- Do not say Gemini or the app cannot read the data. SPEC forbids that claim.
- Do not open the browser dev tools or any terminal on the judges' screen.

## Open items for lead ([verify])

1. The start command, and how to seed "history before `rec-0001-020`".
2. How the Driver triggers, during the demo, the `rec-0001-020` webhook, Bank A's RFC and Bank B's RFC (CLI, a hidden admin button, or a timed script).
3. The exact package URL in the analyst console, and where the send/dispatch button lives (export page or detail page).
4. The Gemini probe command, and what stays live under `ALIBI_REPLAY=1`.
5. Whether the starting alert is "unexplained credit" (the credit is already in the feed at 0:00, as written here). SPEC 0:25 also mentions a bank feed update arriving live; if lead prefers that, push `txn-0001-004` at 0:25 together with `rec-0001-020`. The alert then appears at 0:25 instead, so it is no longer "before any request" until the RFC at 0:45.
