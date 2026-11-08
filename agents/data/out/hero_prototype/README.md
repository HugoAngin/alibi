# Landing hero prototype (data, handed to eval)

`index.html` is a standalone page, served as plain HTML. The part between the `HeroScene markup starts/ends` comments is the drop-in: a `<figure data-hero-stream>` with an inline SVG, its `<style>` and its `<script type="module">`. Port it into `HeroScene.astro` by keeping the markup and style as they are and moving the script into an Astro `<script>`.

- **Inputs:** Mock Marketplace (case 1, stamped "case 1 · synthetic") plus generic synthetic sources (online retailer, food delivery app, clothing shop, electronics store), a current and a savings account, and a mule-pattern check (Acct A → Acct B). Elio asked for Amazon, Deliveroo and Uniqlo; research's instruction is to keep generic labels until he confirms.
- **Packets:** short stamped labels ride the paths into the engine. Case 1 values travel on the Mock Marketplace and current-account paths (`rec-0001-013 £450.00`, `PO-0001-01`, `txn-0001-004 +£2,160.00`); the rest are illustrative. Each packet is visible for 55% of its cycle, so they never pile up.
- **Engine:** Alibi with Matcher, Detective, Challenger and Curator. Scroll (`alibi:scroll`, or the window scroll) lights them in turn over 60% of a screen of scroll. At the top of the page they cycle slowly.
- **Output:** a signed context package (`txn-0001-004 r1`, `PO-0001-01 r1`, "approved by Maya") travels to "Bank A · fraud team", answering `rfc-0001-01`.
- **Constraints:** it only renders while visible (IntersectionObserver), and `prefers-reduced-motion` gets one still frame. It uses the page palette (`--s-*` tokens, ink-blue accent), Plex Mono stamps, the page easing, and no glows.
- **Checked in a browser** at 1440×900 and 375×812: no horizontal scroll and no console errors. Labels are small at 375; consider a simpler phone layout.
