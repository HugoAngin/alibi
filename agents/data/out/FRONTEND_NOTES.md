# Frontend notes (data: owns `alibi/app/static/alibi.css`)

## Phase 1 review, 2026-11-07 ~23:55Z

I reviewed `s4a` at 6466584, using its 14 preview pages (`python -m alibi.app.preview_render`). Each page was viewed at 800 px and at 390 px, with dark mode on (the pane follows the OS). `pytest alibi` gives 22 passed. I have not checked the print preview yet; that is a Phase 2 to-do.

Every item below is marked with who acts on it:

- **CSS:** I change it in `alibi.css` after the S4 merge. Templates stay untouched.
- **→ research:** a template change for Elio. It is either a class or attribute hook, or copy or ordering. It never renames a variable or form field.
- **→ lead:** a `.py` or data change.

Order follows FRONTEND.md's value-to-judges list.

### 1. Trace (request page)

- At present every step (question, claim, sources, lookups, calculation, status) looks the same: a small grey label and a hollow dot, so the chain does not read as a chain.
  - **CSS:**
    - Number the steps with a CSS counter, so they read 1 Question, 2 Claim and so on.
    - Make the rail solid, and fill each step's dot when the step has content.
    - Put the calculation in a boxed mono block.
    - Mark the status step with a top rule and a slightly larger badge, so the chain visibly ends there.
- Sources render as a full `.items` list. Every `record r1` row gets a padded, bordered line, so five sources take half a screen.
  - **CSS:** inside `.trace__steps`, make `.items` an inline wrapping list of chips. Each chip shows the mono record ID with its revision plus its trust badge. These are the source links, so they stay underlined and become the most prominent thing in the step.
- Lookups render as a list too.
  - **CSS:** show each lookup as a monospaced call line, with the result after the arrow on its own line at phone width.
- **→ research:**
  - Add a modifier class per step (`trace__step--question|claim|sources|lookups|calculation|status`). Until then I select steps with `:nth-child`, which breaks if the order ever changes.
  - Optionally add `data-status="{{ step.status }}"` on the step `<li>`, so the rail can take the status colour.

### 2. Private review

- The section headings (Claims, Included, Excluded, Generalised, Gaps) sit flush against the content above them. With no top margin, the three groups run together.
  - **CSS:** add top spacing for `h3` after content inside a card. Give `#included`, `#excluded` and `#generalised` group styling through their IDs and the list that follows each:
    - included: solid left rule
    - excluded: dashed rule and muted text
    - generalised: dotted rule
  - Each group also gets a text mark, so they stay distinct in grayscale.
- Approval box head bug: `.card--private .card__head::after` ("Private to you") also hits the nested `.approval` head. The `pending` badge ends up centred between "Approval" and "PRIVATE TO YOU".
  - **CSS:** scope the rule to `.card--private > .card__head`, so the label appears once, on the section.
- The "Approve for Bank A" button names the recipient, which is good. The digest and validity window are fine as they are, but the "Preview the package as the bank will see it" link sits tight under the `kv`.
  - **CSS:** add spacing.
  - **CSS:** make the button full width at phone width.
- **→ research:** wrap each group in `<section class="review-group review-group--included|excluded|generalised|gaps">`. That is cleaner than ID-plus-sibling selectors.

### 3. Analyst package view

- The three panels (`#authentication`, `#source-trust`, `#claim-status`) share one `.panel` style. Only the small uppercase title tells them apart, and on a phone they stack into what looks like one long panel.
  - **CSS:** give each panel its own frame by ID:
    - authentication: solid 2 px frame and an "INTEGRITY" text mark
    - source trust: dashed separators per attachment
    - claim status: double rule
  - Under each title add a one-line role caption through `::after`: "proves who signed it", "how far each source is trusted", "what the evidence supports". A valid signature then visibly is not "everything is true".
- Tables in this view scroll inside their wrapper at 390 px (452 and 459 px wide in a 324 px column).
  - **CSS:** stack table rows at narrow widths (see 7).

### 4. Transaction detail

- The older revision uses only a dashed border plus a struck-through "superseded" badge. That is subtle in dark mode.
  - **CSS:** give `.revision--older` a muted background, muted text and a "HISTORICAL" mono label. Keep `.revision` (active) solid, with an "ACTIVE" label.
- The reconciliation table reads well. Make the total row stronger and right-align the compare row.
  - **CSS**

### 5. Refusal states

- `dispatch_refused` already shows a red-bordered block with the verbatim backend reason, and it does not look like a toast.
  - **CSS:** add a 6 px left rule, a "REFUSED" mono mark before the title (mirroring `.banner--replay`), and keep `.result__reason` wrapping (it already uses `pre-wrap`).

### 6. Passport home

- The order is holder, connections, transactions, evidence, alerts, rules, requests, sharing. The guide wants alerts and open requests first and connections last.
  - **→ research:** reorder the sections in `passport/home.html`. I will not reorder them with CSS `order`, because that would break reading and tab order.

### 7. Mobile (390 px)

- No page scrolls horizontally. Tables do overflow inside `.table-wrap`:
  - passport home: 516, 671 and 715 px
  - transaction detail: 376 and 399 px
  - analyst package: 452 and 459 px

  On passport home the amount column sits off-screen, which is the worst case.
  - **CSS:** below 600 px, stack `.table` rows as blocks, hide the `thead`, and print `td[data-label]` labels through `::before`.
  - **→ research:** add `data-label="<column header>"` to `<td>` cells in the passport, transaction and package tables.
- Raw ISO timestamps (`2026-11-01T07:30:00Z`) take the widest column and read poorly for Maya.
  - **→ lead:** a Jinja date filter, e.g. "1 Nov 2026, 07:30 UTC". Analyst screens can keep ISO.

### 8. Copy and fixtures

- **→ lead (data question):** the UI sample fixture and my development cases use different identifiers for the same demo story.

  | | UI sample | My `agents/data` case 1 |
  | --- | --- | --- |
  | Credit | `txn_0001`, booked 2026-11-03 | `txn-0001-004`, 2026-10-28 |
  | Merchant records | `ord_1001`, a separate `settlement` record `stl_9001` | `rec-0001-*`, settlement folded into the payout record |
  | Payout reference | `PO-7781` | `PO-0001-01` |
  | Merchant | ResaleHub | mock-marketplace |

  The amounts match: 240000, 18000, 6000 and 216000 pence. CONTRACTS asks for one fixture everywhere, so I will align cases 1–2 to the connector fixture once it is published. I have asked lead on the board.

## Phase 2 log (edits to `alibi/app/static/alibi.css`)

The draft is ready as `agents/data/out/alibi_css_phase2.patch`, made against s4a at 6466584. It is CSS-only and touches no templates. On s4a, `pytest alibi` gives 22 passed with it applied. I checked it at 800 px (dark) and at 390 px (light).

What it changes:
- Approval head bug fixed: `.card--private > .card__head::after`.
- Heading spacing inside cards, the approval box and result blocks.
- Trace: numbered steps, solid rail, source chips, boxed calculation, and a status step marked as the end of the chain.
- Review: included, excluded and generalised lists get solid, dashed and dotted rules plus the text marks "sent", "not sent" and "sent in less detail". Excluded items are muted rather than faded.
- Analyst panels: authentication solid, source trust dashed, claim status double, each with a one-line role caption.
- Revisions: HISTORICAL (muted) and ACTIVE (solid) labels.
- Refusal: a REFUSED mark and a red title; successful dispatch gets a DELIVERED mark.
- Phone tables: rows stack below 600 px, but only for tables whose cells carry `data-label`. Nothing changes until research adds the attribute.

Apply it after the S4 merge with `git apply agents/data/out/alibi_css_phase2.patch`, or by hand if S4 touched the CSS, then run `pytest alibi` and commit `alibi/app/static/`.
