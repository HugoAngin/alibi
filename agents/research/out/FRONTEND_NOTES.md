# Frontend notes (research)

Phase 1 change list for `alibi/app/templates/`, from `docs/FRONTEND.md`. No template is edited until lead announces the S4 merge (around 00:45).

## How I reviewed

- Source: `origin/s4a` at `6466584`, extracted with `git archive` into a scratch folder. This shared checkout stays on `main`.
- `uv run --frozen python -m alibi.app.preview_render` wrote 14 pages. `uv run --frozen python -m pytest alibi -q` gave 22 passed, the baseline.
- Screens were captured with headless Microsoft Edge at 1280 px and inside a 390 px iframe. Edge headless will not size a window below about 500 px, so a plain 390 px window gives misleading results. The export page was printed to PDF and I checked page 1.
- Not checked: keyboard focus order, screen readers, the light theme on screen (the captures used dark), or the alerts, rules, audit and inbox pages beyond a glance.

## Who acts on each item

| Tag | Meaning |
| --- | --- |
| T | Template edit. Research does it after the merge. |
| L | Need to lead: a new variable, a route, or sample or wiring data. |
| D | Need to data: `alibi/app/static/alibi.css`. |

## Blocking for the deck and demo (do these first)

1. **L: The preview sample data is not Maya's canonical fixture.** It uses `txn_0001` booked 3 Nov, `po_7781`, `ord_1001` and question ids `q1`–`q3`. The canonical fixture is `txn-0001-004` booked 28 Oct, `PO-0001-01`, and `q.source_of_funds`, `q.breakdown`, `q.sales_refunds_in_period`. Deck slides 2, 3 and 6–11 must show the wired app on case 1, so no screenshot can come from the preview.
2. **L: The sample "What changed" models a balance release as revision 1 of the payout record (`released_to_balance` → `settled`).** In the contracts and data's cases they are separate records: `balance_release` `rec-0001-020` and payout `rec-0001-021`, which is lookup-only. Shown as a revision, it teaches judges the opposite of the demo's point. The real "what changed" example is a signed correction (case 25: a payout corrected to `failed`), or the lookup result arriving. The wiring must not reuse the sample shape.
3. **L: Sample attachment `att_03`, "Alibi redacted extract", has trust `issuer_verified`.** SPEC says a redacted extract is attested by the package signer and labelled that way. On the analyst screen it would read as issuer-signed. Either the trust shown for extracts changes, or `source_trust[]` gains the `attestation` field that the export page already has (`issuer_signature | package_signer | none`). I would then render "attested by Alibi" in place of the trust badge.
4. **T + D: Tables clip at 390 px on holder screens.** On passport home, the transactions amount column is cut (the £2,160 is not visible), as are the requests "Received" column and the sharing-history "Sent" column. On transaction detail, the reconciliation minor-units column and the "Compared to" row are cut. Fix: at phone width, render each row as a stacked block (label, value) with the amount on its own line. T: add `data-label` attributes and a wrapper class. D: a `@media (max-width: 599px)` rule that turns `table.stack` rows into blocks.

## 1. Trace view, `requests/detail.html` `#trace` (Traceable reasoning, 25%)

5. **T: Make the chain read as one path.** Number the steps 1–6 (Question, Claim, Lookup, Sources, Calculation, Status). Put **Lookups before Sources**, because in the demo the lookup is what produced the deciding source (`PO-0001-01` → payout record). This is a reorder inside the template only.
6. **L: Mark which sources came from a lookup.** Add `trace[].sources[].via_lookup: int | None` (index into `trace[].lookups`) or `retrieved: bool`. I then render "retrieved by lookup 1" next to that source. Without it, a judge cannot see that the scoped retrieval changed the answer. Assumption until it arrives: no marker (I will not guess by string-matching `result_summary`).
7. **T: Sources take one row each with a divider, so 5 sources fill a screen.** Render them as an inline wrap list of `record r<rev>` + trust badge chips.
8. **T: Repeat the step status in the summary line of each step card head** (it is already badged), and add the `status_reason` right under it, so the conclusion is readable without scrolling to step 6.
9. **L: The calculation shows `240000 − 18000 − 6000` with no link to which records each term came from.** The analyst reconciliation has `lines[].source_ids`. Add the same `calculation.terms[] {label, sign, amount, source_ids}` to trace steps, optional, and I show it as a small table under the expression.

## 2. Private review, `#review` (Relevance and Risk)

10. **T: Make Included, Excluded and Generalised three visibly separate groups.** Right now they are plain `h3` headings in one long list. Give each its own bordered sub-card with the count in the heading ("Included · 4"), and drop the separate `.stats` row, which duplicates the counts. Excluded uses a muted style, and Generalised shows the before → after diff it already has.
11. **T: Excluded reason duplicates the rule.** "Rule rule_01 denies item descriptions. Rule `rule_01`." Print the rule id only when the reason text does not already contain it, or always print it as a separate small "rule_01" chip.
12. **T: Approval box hierarchy.** Keep the sentence "Approving sends exactly this preview to Bank A" and the button as the primary content. Move the digest, policy version and validity window into a `<details>` "Approval details" (no JS needed). The status badge sits in the middle of the head; move it next to the title.
13. **T: "Preview the package as the bank will see it" sits flush against the key-value list.** Make it a secondary button above the approve button.
14. **T: The review repeats each claim's rationale already shown in the trace.** Keep it (Maya reads the review, judges read the trace), but drop the long rationale on the trace side to one line once item 8 lands.

## 3. Analyst package, `analyst/package.html` (Risk, 15%)

15. **T + D: The three panels look identical** (same card, same heading style). Give each a distinct header line that answers one question: "Is this package genuine?" (authentication), "Who vouches for each source?" (trust), "What does the evidence establish?" (claim status). Put a large verdict at the top of each: one authentication badge; trust counts by level; claim counts by status. D: a different left-border style per panel (solid, dashed, double), matching the grayscale badge scheme.
16. **T: The authentication panel is cramped at 3 columns.** Dates wrap mid-token, and the digest takes 4 lines. Move the checks table below the 3-panel grid at full width, leaving the verdict, key and short digest (first 12 hex characters + "…", full value in `title`) in the panel.
17. **T: Keep the existing sentence "These three are independent…"** but move it above the panels as the lede, where a judge reads it first.

## 4. Transaction detail, `passport/transaction.html`

18. **T: Mark the older revision as historical.** Add a "superseded" status badge on the r1 column header and dim the whole before column, not only strike-through text. The `<details>` "View older revision" is right.
19. **T: The reconciliation "Compared to" row puts "matches" in the Sources column.** Move the match badge into its own last cell, or below the table with the difference.

## 5. Refusal, `requests/dispatch.html`

20. **T: The refusal already reads as a block.** Two changes: put the one-line plain reason ("You changed the package after approving it. Nothing was sent.") at the top in large text, above the IDs, and keep the verbatim backend reason in its `<pre>`. Shorten the expected and actual digests to a prefix with the full value in `title`, so the mismatch is visible at a glance (the backend reason already prints prefixes).
21. **T: "Retry dispatch" next to "Review and approve the revised body" invites the wrong click in the live demo.** Make retry a quiet link under the explanation.

## 6. Passport home, `passport/home.html`

22. **T: Reorder sections as the guide says.** Alerts and open requests first, then transactions and evidence status, then sharing history, rules and connections. Holder details move to the bottom or into the header.
23. **T: Timestamps on holder screens are raw ISO and wrap.** Add a `when(iso)` macro in `partials/` that prints `28 Oct 2026, 06:30 UTC` by slicing the string, with the raw value in `<time datetime>`. Analyst screens keep raw ISO.

## 7. Print

24. **D: "Claims by question" lands alone at the foot of page 1** of the printed export. Add `h2, h3 { break-after: avoid }` and `.card, article { break-inside: avoid }` to the print block. The footer line and `no-print` banner render correctly.

## 8. Copy

25. **T: Holder-facing jargon to soften** (plain English, guide item 8): "Subject reference" → "Account reference"; "Link state is separate from source trust and claim status" → keep for analysts, and on the holder side say "Linked means we found the matching records. It does not mean the bank accepted them."; "Default-minimum disclosure" → "Not needed to answer the question."

## Deck screenshots (guide item 9)

After the screens are final and wired to case 1, capture every demo step at 1280×800 desktop and at 390 px phone width, dark and light, and file them under `agents/research/out/shots/` with the deck slide number in the name. They fill DECK.md placeholders PH-02, PH-07, PH-09 and PH-13.
