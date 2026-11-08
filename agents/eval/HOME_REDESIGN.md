# Alibi Home redesign: handoff for Elio

From `eval` to `research`, 8 Nov 2026. This is the repo copy of the [handoff doc](https://claude.ai/artifact/8L8ZVny6meCiU2icQNw8eC). Work from this file; it has the same content.

## What changes

Home becomes four clickable cards: Recent activity, Banks, Merchants and Financial context. Each card previews its list. Clicking it opens the full list, in a modal on desktop and a bottom sheet on a phone. Nothing on Home approves or sends.

Prototype: [Alibi dashboard cards](https://claude.ai/artifact/PgrHwVY25fXGD4rizUgYVo). If the link does not open, ask Tamim to share it. Press Play on the 1440px or 390px frame, then click a card. In Tweaks, **Expanded card** opens a state without clicking, and **Simulate** shows the `/simulate/` version. The data is synthetic Case 1, with Bank A shown as trusted.

Files, all inside `web/apps/alibi/src/components/app/`, which you own:

- `PassportHome.astro`: the card grid, the four dialogs and their styles. `/app/` and `/simulate/` both render it, so one change covers both routes.
- `home.ts`: a few new pure helpers (see below), tested in `tests/home.test.ts`.
- `ActivityRow.astro`: reused in the card and in the dialog, with a new `compact` prop.
- `AppFrame.astro`: no layout change. Keep the desktop top bar and the mobile dock as they are; the sheet sits above the dock.

No backend change is needed. Every field below already comes from `PassportHome` or `GET /txn/<id>`.

## Layout

Desktop (960px and up) is a 12-column grid with a 24px gap inside the existing 76rem frame. Row one is Recent activity (7 columns) and Banks (5). Row two is Merchants (5) and Financial context (7). The DOM order matches the brief, so the phone stack reads Activity, Banks, Merchants, Financial context. Below 960px every card spans the full width.

Keep the greeting above the grid: "Hi Maya", the existing `headline()` sentence, and one new line naming the earliest reply-by date ("Bank A's reply is due first, by 9 Nov.").

Each card is a `<section>` with:

- a header: an `<h2>` holding a `<button>` with the card's name, a count, and a "View all" or "Full timeline" hint on the right;
- one summary line, such as "2 need your review" or "1 of 4 transactions explained · checked 2 Nov";
- the preview: up to 4 activity rows, every bank, up to 3 merchants, or the newest transaction;
- an optional footer line in faint text.

The whole card is the click target. Stretch the header button over the card with `.card-open::after { content: ''; position: absolute; inset: 0 }`, and keep preview rows non-interactive so no link sits inside the button. Draw the focus ring on the card with `.card:has(.card-open:focus-visible)`.

Card tokens: `--s-card` background, 1px `--s-line` border, 20px radius, 24px padding on desktop and 16px on a phone. Hover lifts the card by 1px with `--s-shadow`. Typography, colours and the monograms stay as they are today.

## Modals and the bottom sheet

Use one native `<dialog>` per card, rendered on the server with the full list and opened with `showModal()`. That gives Esc to close, a focus trap, `::backdrop`, and focus back on the card button when it closes.

- **Open**: each card button carries `data-dialog="dlg-activity"` and `aria-haspopup="dialog"`. A small inline script in `PassportHome.astro` calls `showModal()` on click. It is the only script Home needs.
- **Close**: a visible Close button (X icon plus the word "Close") inside `<form method="dialog">`, so it closes without script. A click on the backdrop (`event.target === dialog`) closes too.
- **Structure**: a fixed header (title, one sentence of explanation, Close), a scrolling body, and on Activity and Banks a footer note: "Opening this list changes nothing. Approval happens on each request's page."
- **Desktop**: centred, `width: min(720px, 100% - 48px)`, Financial context `min(920px, …)`, `max-height: calc(100dvh - 96px)`, 24px radius, scrim `rgb(31 27 22 / 0.42)`.
- **Phone (below 768px)**: the same dialog becomes a bottom sheet: `margin: auto 0 0`, full width, `max-height: 90dvh`, radius `24px 24px 0 0`, a decorative grab handle, a 44px-tall Close button and `padding-bottom: env(safe-area-inset-bottom)`. The body scrolls with `overscroll-behavior: contain`, and `html:has(dialog[open]) { overflow: hidden }` stops the page scrolling behind it. The sheet covers the dock.
- **Motion**: fade the scrim in, and slide the sheet up over 300ms with `--s-ease`. Turn both off under `prefers-reduced-motion`.
- **Financial context entries**: keep `<details>` and `<summary>` as today, so they work without script. The newest entry starts open. The month heading is sticky inside the dialog body.

## Status labels

Every status becomes a short word or phrase in a pill. The glyphs (✓ ✕ ? = ~ – ● ◆ ○) and the state dot go. Colour always comes with text, never on its own.

| Status | Today | New label | Pill tone |
| --- | --- | --- | --- |
| Request awaiting | dot + "Awaiting your review" | Awaiting your review | review |
| Request awaiting, trusted bank | dot + "Trusted · approve in one tap" | Trusted · approve in one tap | review |
| Request approved / sent | dot + label | Approved by you / Sent | ok |
| Request declined / expired | dot + label | Declined by you / Expired | bad / quiet |
| Alert open / resolved | dot + Open / Resolved | Open / Resolved | warn / quiet |
| Transaction linked | "= Linked to 1" | Explained | ok |
| Transaction ambiguous | "~ Ambiguous" | More than one possible match | warn |
| Transaction unmatched | "– Unmatched" | Not explained yet | quiet |
| Claim | ✓ Supported, ✕ Contradicted, ? Unresolved | Supported / Contradicted / Unresolved | ok / bad / warn |
| Source trust | ● ◆ ○ + label | Issuer verified / Bank observed / Self-supplied | plain text, no pill |
| Merchant connection | Connected, Syncing, Sync failed, Disconnected | same words | ok / quiet / bad / quiet |

Pill: 12px, weight 600, padding 3px 9px, fully rounded. Tones, all at least 4.5:1:

- review: `--a-accent` on `--a-accent-soft`
- ok: `--a-ok` on a new `--a-ok-soft: #e6f1ea`
- warn: `--a-warn` on a new `--a-warn-soft: #f6ecd9`
- bad: `--a-bad` on a new `--a-bad-soft: #f7e3e1`
- quiet: `--a-faint` on `--a-tint`

The Trusted tag next to a bank's name keeps its current style.

## Data and code changes

Each card reads helpers that already exist. The new code is five small pure functions in `home.ts`.

| Card | Preview | Dialog | Data |
| --- | --- | --- | --- |
| Recent activity | `activity(data).slice(0, 4)` | Every item, grouped "Needs your review" then "Earlier". Each request links to `requestHref(id)` | `requests`, `alerts` |
| Banks | `banks(data)`: name, Trusted tag, "Account feed connected" or "Requests only", awaiting count | Adds the feed name, record count and `when(last_synced_at)`, requests, packages sent, a link per awaiting request and one to `/app/banks/<audience>/` | `requests`, `sharing_history`, `connections`, trust cookie |
| Merchants | `merchants(data).slice(0, 3)`: name, status, `day(last_synced_at)` | Adds record count, trust label, status label, full sync time and a link to `/app/merchants/` | `connections` |
| Financial context | First `contextTimeline()` entry: amount, day, `explain()`, label | `byMonth(contextTimeline())` with sources, reconciliation lines and claims | `transactions`, `evidence_status`, `GET /txn/<id>` |

New in `home.ts`:

1. **`activity()` sort**: inside the waiting group, sort by `expires_at` with the soonest reply first, then by newest. Add `due` to `ActivityItem` for requests. Case 1 then lists Bank A (9 Nov) above Bank B (11 Nov).
2. **`nextDue(data)`**: the earliest awaiting request as `{ recipient, expires_at }` or `null`. It feeds the line under the headline.
3. **`explain(entry)`**: one plain sentence built only from structured fields, never free text the backend did not send.
   - Linked, with a matching reconciliation: "Matches {issuer}'s {kind} {reference}, which settled on {day}. The {n} records it covers add up to this amount."
   - Linked, without one: "Matches {issuer}'s {kind} of the same amount on {day}."
   - Ambiguous: "More than one record could explain this. Alibi has not picked one."
   - Unmatched: "Only your bank has a record of this. No connected merchant has sent anything that explains it yet."
4. **Reconciliation lines**: type `TxnDetail.reconciliation.lines` as `{ label, kind, sign, amount, source_ids }[]`, plus `result`, `difference` and `compared_to`, matching `calc_terms()` in `alibi/app/common.py`. Each line becomes a row: label, `source_ids.length` records, and the signed amount. Keep the pence expression as a mono footnote.
5. **`STATUS_TONE` and `LINK_LABEL`**: map each state to its label and pill tone from the table above. Keep `TRUST_MARK`, `CLAIM_MARK` and `LINK_MARK` for their labels, and stop rendering `.mark`.

`ActivityRow.astro` gets a `compact` prop. Compact rows render as `<div>` with no chevron, because they sit inside the card's button. Full rows stay links. Both keep the `trusted` prop and its "Trusted · approve in one tap" label.

Open question: the prototype also shows month in and out totals, and "explains 1 of 4 bank transactions" for a merchant. Both are UI sums of backend fields. Ship them only if you are happy for the UI to add minor units, and drop the merchant count unless source issuer ids match connection ids exactly.

## Safety and /simulate/

Opening a card only shows markup that is already on the page. Home makes no fetch, has no form and sends no POST. The only forms stay where they are: `/app/act` on the request page and `/app/trust` on the bank page.

- **One-tap flow unchanged**: a trusted bank's waiting request still reads "Trusted · approve in one tap" on Home. The Approve and send button stays at the top of `RequestView.astro` and posts `and_send=1`. Do not add an approve or send control to any card or dialog.
- **Pass `simulation` into `PassportHome`** from `pages/simulate/index.astro`. `AppFrame` already takes the prop.
- **Links in /simulate/**: `simulationLinks()` rewrites Home and request links to `/simulate/`. It leaves `/app/banks/<audience>/` and `/app/merchants/` alone, and those ask a visitor to sign in. When `simulation` is true, hide the bank-page and merchant-details links and keep the facts.
- **Wording in /simulate/**: request links say "View request", waiting statuses say "Awaiting Maya's review", and the dialog footers say "Read-only. Nothing here can approve or send for Maya." The read-only bar from `AppFrame` stays at the top.

## Acceptance checks

- [ ] `pnpm verify` passes from `web/`.
- [ ] `tests/home.test.ts` covers the new sort by reply-by date, `nextDue()`, each `explain()` branch and the typed reconciliation lines.
- [ ] At 1440px, the cards sit in two rows with no horizontal scroll. Each dialog opens, closes with Esc, Close or the backdrop, and returns focus to its card.
- [ ] At 390px, the cards stack above the dock. Each sheet scrolls inside itself, the page behind stays still, and Close stays visible.
- [ ] Opening any card sends no request: the network panel shows no POST and no new fetch.
- [ ] In `/simulate/`, no bank-page or merchant-details link shows, request links go to `/simulate/requests/<id>/`, and Home has no form.
- [ ] No glyph marks remain on Home, and every coloured status also has words.
- [ ] With the keyboard alone, Tab reaches each card, Enter opens it, and every link and Close inside is reachable.
