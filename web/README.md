# Alibi web

The foundation for Alibi's web front end: three Astro sites in one pnpm workspace, each deployable to Cloudflare. The structure is in place and every page renders from synthetic data. Visual design is a later pass, and no screen talks to a backend yet.

| Site                 | Folder          | Local URL             | What it is                                                                           |
| -------------------- | --------------- | --------------------- | ------------------------------------------------------------------------------------ |
| Alibi                | `apps/alibi`    | http://localhost:4321 | The product. `/` is the landing page on a desktop and the app on a phone.            |
| Merchant (simulated) | `apps/merchant` | http://localhost:4322 | A resale marketplace with "Connect to my Alibi" in account settings and at checkout. |
| Bank (simulated)     | `apps/bank`     | http://localhost:4323 | A bank's fraud-operations console with "Request customer's Alibi" on each case.      |
| Shared UI            | `packages/ui`   |                       | Design tokens, the Alibi logomark and the two embeddable buttons.                    |

Everything is synthetic. Every page carries a banner saying so.

## Run it

Node 22.12 or newer (`.nvmrc`) and pnpm 10.

```bash
pnpm install
pnpm dev            # all three sites
pnpm dev:alibi      # or one at a time: dev:merchant, dev:bank
pnpm verify         # format, lint, type check, tests, build: run before every commit
```

To see the app on a desktop, press **View on a phone** on the landing page. It opens http://localhost:4321/phone/, which runs `/app/` inside a 390 × 844 phone frame. `/app/` on its own always shows the app, and `/?view=app` makes `/` show it too (remembered in a cookie; `?view=site` switches back).

Try the connect flow: open http://localhost:4322/checkout/ (or `/account/settings/`) and press "Connect to my Alibi". Alibi's consent page opens in a small window: sign in to Alibi (Clerk), then Connect or Cancel. The window closes and the shop shows "Connected to Alibi as <first name>" without leaving checkout; with popups blocked it is a normal redirect instead. The connection is saved on the Alibi account and listed, with Disconnect, at http://localhost:4321/app/settings/. The Alibi site needs `CLERK_SECRET_KEY` in its environment to run locally (web/docs/hosting.md).

## How the pieces fit

- **One URL, two experiences.** `apps/alibi/src/pages/index.astro` is rendered per request by the Cloudflare Worker. `src/lib/view.ts` picks `site` or `app` from `?view=`, the `alibi_view` cookie, the `Sec-CH-UA-Mobile` client hint, then the user agent (phones only; tablets get the responsive landing page). The response carries `Vary` so caches keep the two apart. Every other page is prerendered to static assets.
- **"Connect to my Alibi"** (`packages/ui/src/ConnectAlibiButton.astro`) is a link to Alibi's `/connect` page with `merchant`, `context` and `return_to`. Alibi signs the holder in, shows what the merchant will share and what it will see (first name only), records the connection on their Clerk user, and sends them back with `?alibi=connected&alibi_name=…` or `?alibi=declined`. With `display="popup"` the page opens in a small window and reports the same outcome by `postMessage` to `return_to`'s origin (`ConnectMessage` in `links.ts`). `return_to` must be absolute http(s); anything else is dropped. An unknown merchant is shown as unverified.
- **"Request customer's Alibi"** (`packages/ui/src/RequestAlibiButton.astro`) opens the bank's own request screen, where the analyst picks the question IDs from `shared/CONTRACTS.md`. Sending is not wired yet; it will go through the mock bank service.
- **URL contracts** live in `packages/ui/src/links.ts` and are unit tested. Maya's demo data in `apps/alibi/src/data/demo.ts` is tested against the team's canonical fixture, `agents/data/out/cases/case_0001.json`.

More detail, and what is deliberately left out, in [docs/architecture.md](docs/architecture.md). How the front end and the Python backend split the work, and what the backend must expose, is in [docs/backend-integration.md](docs/backend-integration.md). Working on part of `web/` in parallel: [docs/contributing.md](docs/contributing.md).

## Deploy to Cloudflare

Each app has a `wrangler.jsonc`. With a Cloudflare account logged in (`pnpm exec wrangler login` from an app folder):

```bash
pnpm --filter @alibi/site deploy
pnpm --filter @alibi/merchant deploy
pnpm --filter @alibi/bank deploy
```

Set the origins at build time so the sites point at each other: `ALIBI_ORIGIN` for the Alibi site, `MERCHANT_ORIGIN` and `PUBLIC_ALIBI_ORIGIN` for the merchant, and `BANK_ORIGIN` for the bank. They default to the localhost ports above. Nothing has been deployed yet.
