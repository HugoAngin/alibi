# Drop-in: landing hero (data)

Copy `src/components/site/HeroScene.astro` over `web/apps/alibi/src/components/site/HeroScene.astro`, then run `cd web && pnpm format && pnpm verify`. Data has no Node toolchain here, so it has **not** been formatted, type-checked or built. The same markup, CSS and logic ran as a standalone page in a browser (`agents/data/out/hero_prototype/index.html`) at 1440×900 and 375×812, with no console errors and no horizontal scroll.

- It replaces the three.js passport scene with an SVG stream, built to `agents/research/out/HERO_STREAM.md`. three.js is no longer imported by this component.
- `Hero.astro` needs no change. Its doc comment still describes "Maya's passport with her evidence docking onto it"; research may want to reword it.
- It uses the page tokens only (`--s-paper`, `--s-card`, `--s-ink`, `--s-ink-soft`, `--s-line`, `--s-accent`, `--s-accent-soft`, `--s-font-mono`, `--s-ease`): no glows or gradients.
- Motion follows the page's `alibi:scroll` event, and rendering is paused while the hero is off screen (IntersectionObserver). `prefers-reduced-motion` gets one still frame.
- Merchant labels are generic and synthetic until Elio confirms Amazon, Deliveroo and Uniqlo; a confirmed name is a one-line change per source. Only case 1 values are stamped as real fixture data.
