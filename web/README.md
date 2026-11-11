# Alibi web

Alibi's web front end: one Astro site on one Cloudflare Worker, https://alibi.eliohaddad.workers.dev. It runs the demo as a **recorded simulation**: every backend answer comes from one recorded run of the real Python stack (`packages/ui/src/sim/`), so the site needs no backend, no API keys and no accounts, and each visitor plays Maya's case through on their own (docs/hosting.md).

| Part                 | Path     | Code                                                | What it is                                                                           |
| -------------------- | -------- | --------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Alibi                | `/`      | `apps/alibi/src` (`/simulate/` is Maya's dashboard) | The product: the landing page and Maya's dashboard.                                  |
| Merchant (simulated) | `/shop/` | `apps/alibi/src/shop`, `apps/alibi/src/pages/shop`  | A resale marketplace with "Connect to my Alibi" in account settings and at checkout. |
| Bank (simulated)     | `/bank/` | `apps/alibi/src/bank`, `apps/alibi/src/pages/bank`  | A bank's fraud-operations console with "Request customer's Alibi" on each case.      |
| Shared UI            |          | `packages/ui`                                       | Design tokens, the logomark, the two embeddable buttons, and the simulation.         |

Everything is synthetic. Every page carries a banner saying so.

## Run it

Node 22.12 or newer (`.nvmrc`) and pnpm 10.

```bash
pnpm install
pnpm dev            # http://localhost:4321 (the bank at /bank/, the marketplace at /shop/)
pnpm verify         # format, lint, type check, tests, build: run before every commit
```

`pnpm dev` runs the simulation, like the deployed site. Open http://localhost:4321/simulate/ and follow the guide at the top of each page: withdraw on the marketplace, request Maya's alibi at the bank, answer it as Maya, then read it as the analyst. **Reset demo** starts over.

```bash
pnpm record:simulation   # re-record the simulation from the Python stack (about 2 minutes)
pnpm demo                # the local live demo instead: the site against the real backend (alibi/RUNBOOK.md)
```

## How the pieces fit

- **The simulation.** `apps/alibi/src/middleware.ts` runs each request inside `runSimulated` (`packages/ui/src/sim/context.ts`) when `ALIBI_SIMULATION` is `"1"` (the default). The backend clients then answer from `recording.json`, moving the visitor along the recorded graph, and the state cookie carries their place across Alibi, `/bank/` and `/shop/`. `SimGuide.astro` shows the next step on every page.
- **"Connect to my Alibi"** (`packages/ui/src/ConnectAlibiButton.astro`) is a link to Alibi's `/connect` page with `merchant`, `context` and `return_to`. Alibi shows what the merchant will share and what it will see (first name only), and sends them back with `?alibi=connected&alibi_name=…` or `?alibi=declined`. With `display="popup"` the page opens in a small window and reports the same outcome by `postMessage` to `return_to`'s origin (`ConnectMessage` in `links.ts`). `return_to` must be absolute http(s); anything else is dropped. An unknown merchant is shown as unverified.
- **"Request customer's Alibi"** (`packages/ui/src/RequestAlibiButton.astro`) opens the bank's own request screen, where the analyst picks the question IDs from `shared/CONTRACTS.md`. Sending is not wired yet; it will go through the mock bank service.
- **URL contracts** live in `packages/ui/src/links.ts` and are unit tested. Maya's demo data in `apps/alibi/src/data/demo.ts` is tested against the team's canonical fixture, `agents/data/out/cases/case_0001.json`.

More detail, and what is deliberately left out, in [docs/architecture.md](docs/architecture.md). How the front end and the Python backend split the work, and what the backend must expose, is in [docs/backend-integration.md](docs/backend-integration.md). Working on part of `web/` in parallel: [docs/contributing.md](docs/contributing.md).

## Deploy to Cloudflare

`apps/alibi/wrangler.jsonc` names the Worker `alibi`, served at https://alibi.eliohaddad.workers.dev (the account's workers.dev subdomain is `eliohaddad`). With a Cloudflare account logged in (`pnpm exec wrangler login` from `apps/alibi`):

```bash
pnpm --filter @alibi/site run deploy
```

No secrets are needed.
