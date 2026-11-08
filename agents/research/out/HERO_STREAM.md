# Landing hero: the Alibi stream (Elio's spec, 2026-11-08 04:25Z)

This replaces the hero visual in `web/apps/alibi/src/components/site/HeroScene.astro`, which currently shows the passport card with three paper slips. `Hero.astro` places it in the right half of the hero grid.

## What it shows

The Alibi engine sits at the centre, with its Gemini agents around it: Matcher, Detective, Challenger and Curator. Animated streams of financial data flow into the engine.

**Inputs: merchants**
- Amazon
- Deliveroo
- Uniqlo
- A merchant known for large purchases.

**Inputs: banks**
- More than one bank account stream, each showing example transactions as they arrive.
- A money-mule detection stream that shows money moving between two bank accounts.

**Output**
- A curated context package that leaves the engine and answers a query from a bank's fraud department.

## How it should feel

- It must not feel static.
- Elio prefers it to react to scroll: inputs flow in, the engine works, then the package leaves for the bank. A single animated diagram is an acceptable fallback.
- The rest of the page should stay as it is.

## Constraints that already hold on the page

The page has just been through an unslop pass (commit with this file):
- Warm paper palette: `--s-paper #f7f4ee`, `--s-card #fdfbf7`, `--s-ink #1f1b16`.
- One accent, ink-blue `--s-accent #2b45c4`.
- The signature is a stamped mono reference such as `txn-0001-004 r1` (class `.s-ref`, IBM Plex Mono).
- No glows, orbs, neon or purple gradients.
- Easing `cubic-bezier(0.22, 1, 0.36, 1)`.
- Respect `prefers-reduced-motion` by showing one still, readable frame.
- Render only while the hero is visible. Scroll progress arrives on the `alibi:scroll` event (Lenis, `src/scripts/motion.ts`).
- three.js is already a dependency, but SVG or canvas is fine.

## Data honesty

- Amounts and IDs should come from case 1 (`src/data/case1.ts`):
  - £2,160.00 credit `txn-0001-004`
  - payout `PO-0001-01`
  - Mock Marketplace
  - Bank A request `rfc-0001-01`
- The named merchants and the mule stream are illustrative. Mark them as synthetic, for example "synthetic" in mono, and don't present them as real customer data.

## Check before done

```bash
cd web && pnpm format && pnpm verify
```

Then look at http://localhost:4321/ at 1440×900 and 375×812.
