# Handoff: landing page redesign (execution → Tamim / t7acct)

2026-11-08 07:05Z. From execution (sirExile44), at the request of Mouaad (human).

## Ownership

`web/` is execution's in `team.json` (lead, e1be433). For you to commit there, ask lead to move
`web/apps/alibi/src/components/site/`, `web/apps/alibi/src/components/landing/`,
`web/apps/alibi/src/lib/stream.ts` and `web/apps/alibi/tests/stream.test.ts` to eval, or send
execution a patch and I land it. Do not commit into `web/` before one of those happens; the
pre-commit hook will reject it anyway.

## The brief (Mouaad, verbatim points)

The last landing commit (eval's UI patch, applied in 63fa61b) does not meet Elio's spec
(`agents/research/out/HERO_STREAM.md`). What is wanted:

- The landing **sells the product**. The centrepiece is the whole financial passport **data
  stream and workflow**, large, running **vertically and horizontally**, reacting **heavily to
  scroll**. Not a small diagram in half of the hero.
- Headline: **"Your financial passport"**. Very little text. "Really smooth, really visual, no
  heavy text. Stay within scope" (landing only).
- **Merchants**: Amazon, UNIQLO, Deliveroo, and one high-end merchant people make large payments to
  or receive refunds from (plan: Harrods). Colour from their brands.
- **Banks**: Santander and NatWest, two of the customer's accounts. A **mule-pattern scenario**: a
  large sum moves between the two accounts; the engine traces it.
- Formatted transaction information leaves merchants and banks and flows into the **engine** (four
  agents: Matcher, Detective, Challenger, Curator; code decides).
- Show the two buttons: **"Connect to my Alibi"** inside the merchant, **"Request customer's
  Alibi"** pressed by the bank's fraud analyst.
- The **Core sections fold into the stream** (engine, integrations, control) instead of sitting
  as separate text blocks below it.
- The end of the scroll **releases the curated context package** from the engine to the bank's
  fraud department.

Logos: I told Mouaad we render the real names as brand-colour tiles in our own type, not copied
logo artwork, with a small "Illustrative · not affiliated · synthetic data" label, because real
trademarks beside a fraud/mule story imply endorsement, and RULES/SPEC require simulated services
to be labelled. Licensed logo files can be dropped in later. Keep that unless a human says
otherwise.

## Current state

- Live landing: `web/apps/alibi/src/components/landing/Landing.astro` renders `IslandHeader`,
  `Hero` (copy + `HeroScene`, data's SVG stream from eval's patch), `Core` (3 features), a
  Simulate card, a Join section (Clerk `Show`), `SiteFooter`. Elio's original 3D three.js hero is
  in commit `1e3b155` (`HeroScene.astro`) if you want parts of it.
- **Committed with this handoff**: `web/apps/alibi/src/lib/stream.ts` (pure scroll timeline:
  `STAGES`, `stageAt`, `stageIndex`, `progressOf`, `packetState`, `easeInOut`, `agentsLit`) and
  `web/apps/alibi/tests/stream.test.ts` (5 tests, passing). Nothing uses it yet.
- Not started: the component itself and the Landing change.

## The plan I was about to build

New `web/apps/alibi/src/components/site/PassportStream.astro`, replacing `<Hero /><Core />` in
`Landing.astro`. Keep Simulate and Join. Give the section `id="core"` (header and footer link to
`/#core`).

**Structure.** A tall section (~600vh) with a sticky 100svh stage: head (h1 "Your financial
passport", one caption line per stage, a thin progress bar, Join/Simulate only at the intro),
then a board. Nodes are HTML (so the real shared buttons work); wires are one SVG overlay whose
paths the script computes from node rectangles (`getBoundingClientRect` relative to the board),
recomputed by a `ResizeObserver` on the board and every node. No CSS transforms on nodes, or the
anchors drift.

**Desktop board** (CSS grid, 4 columns, board max ~88rem): merchants column (4 tiles) | engine
(row 1) with Santander and NatWest side by side under it (row 2) | consent card | NatWest fraud
desk. **Narrow (≤760px)**: one column in DOM order: merchants 2×2, engine, banks, consent, desk;
board font-size tied to height (`clamp(8px, 1.3svh, 11px)`) so it fits 375×812.

**Tiles.** Amazon `#232F3E` with an orange `#FF9900` bar; UNIQLO `#FF0000`; Deliveroo `#00CCBC`;
Harrods dark green `#00573F` with gold `#B8975A`; Santander `#EC0000`; NatWest `#5A287D`. Each
merchant tile carries the real `ConnectAlibiButton` (`@alibi/ui`, scaled with
`.tile :global(.alibi-connect)`, which out-specifies Astro's scoped `:where`), briefly pressed,
then "✓ Connected". The desk carries the real `RequestAlibiButton href="/simulate/"`.

**Flows** (from → to, draw window, packet window, packet text):

| Flow | Route (desktop) | Draw | Packet | Packet |
| --- | --- | --- | --- | --- |
| amazon | tile right → engine left | .08–.12 | .11–.17 | Payout PO-0001-01 · £2,160.00 |
| uniqlo | tile right → engine left | .10–.14 | .14–.20 | Order · £45.80 |
| deliveroo | tile right → engine left | .12–.16 | .17–.23 | Order · £18.40 |
| harrods | tile right → engine left | .14–.18 | .20–.27 | Refund · £3,450.00 |
| santander | bank top → engine bottom | .30–.34 | .33–.40 | +£3,450.00 · Harrods refund |
| natwest | bank top → engine bottom | .31–.35 | .34–.41 | +£2,160.00 · txn-0001-004 |
| mule | Santander bottom → NatWest bottom (arc down) | .36–.39 | .38–.45 | £3,450.00 → NatWest |
| request | desk top → engine top (arc over consent) | .63–.66 | .65–.71 | rfc-0001-01 · Explain £2,160.00 |
| draft | engine right → consent left | .72–.75 | .73–.78 | Draft · 3 records |
| package | consent right → desk left | .86–.89 | .88–.96 | Context package · signed |

Mule badge pinned on the mule arc: "Mule pattern?" from .40, then "Cleared: own accounts, traced
to Harrods refund ✓" from .56. Agents light at .47 / .505 / .54 / .575, then "✓ refs ✓ dates ✓
pennies" at .60. Consent: ✓ Amazon payout PO-0001-01, ✓ NatWest txn-0001-004, ✓ the match; ✕
UNIQLO, Deliveroo, Harrods stay private; "Approve for NatWest" pressed .825–.84, then "Approved ✓".
Desk at .96: "Package received · signature valid ✓ · approved by Maya for NatWest only".

The merchant and mule figures above are illustrative; only the case 1 IDs (£2,160 credit
`txn-0001-004`, payout `PO-0001-01`, `rfc-0001-01`) come from the fixture.

**Captions** (one per stage, from `STAGES`, reword freely): Scroll to see it work · Merchants send
signed receipts · Your banks stream in · Four agents investigate. Code decides. · Your bank asks a
question · You choose what leaves · A signed answer, delivered.

**Script.** Progress `p = progressOf(section.top, section.offsetHeight, innerHeight)`; update in
rAF on native `scroll` (Lenis in `src/scripts/motion.ts` scrolls the window, so native scroll
fires), only while an `IntersectionObserver` says the section is visible. Each frame: set
`data-stage`, toggle `.is-on` on every `[data-on]`/`[data-off]` element, set each wire's
`stroke-dashoffset` from its draw window, and place each packet with
`path.getPointAtLength(packetState(p, w).t * len)`, fading over the first and last 8%. Wire
curves: cubic Bézier, control points pushed out along each side's normal by
`min(reach, max(24, 0.45·distance))` (reach ~160 desktop, ~90 for the request arc, ~60 for the
mule arc, ~24–36 narrow).

**Fallbacks.** Server-render the final frame (all `.is-on` set except captions/pressed states), so
no-JS shows a complete, readable picture. `prefers-reduced-motion`: section height auto, stage not
sticky, `p = 1`. Packets `aria-hidden`; add a visually hidden summary of the flow. Spec bans
glows, orbs, neon, purple gradients.

## Check before done

```bash
cd web
npm exec --yes --package=node@22 --package=pnpm@10.33.0 -- pnpm format
npm exec --yes --package=node@22 --package=pnpm@10.33.0 -- pnpm verify
npm exec --yes --package=node@22 --package=pnpm@10.33.0 -- pnpm dev:alibi
```

(Use plain `pnpm` if it is installed.) Look at http://localhost:4321/ at 1440×900 and 375×812,
scrolling the whole stream both ways. `pnpm verify` was green on main at 662dbee (76 tests, astro
check 0 errors, 3 builds).

## Deploy

Cloudflare Workers on Elio's account. From `web/`, signed in to wrangler:
`pnpm --filter @alibi/site run deploy` (the package is `@alibi/site`, not `@alibi/alibi`).
