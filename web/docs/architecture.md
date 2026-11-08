# Architecture

## Layers

Each app follows the same layering as Elio's portfolio site:

```
src/data/*.ts        content and fixtures   what the site says, synthetic records
src/lib/*.ts         logic                  pure decisions (tested in tests/)
src/components/*     presentation           scoped styles, no data fetching
src/pages/*.astro    routes                 composition only
```

Shared pieces live in `packages/ui` and are imported as `@alibi/ui/<file>`. Every colour, size and space comes from `packages/ui/src/tokens.css`. The merchant and the bank override a few tokens in their layouts so they look like someone else's product, while the Alibi buttons keep Alibi's colours, as embedded third-party buttons would.

## Rendering

- **Alibi** (`apps/alibi`) uses `@astrojs/cloudflare`. Pages are prerendered by default. Two opt out with `export const prerender = false`, because they read the request:
  - `/` chooses the landing page or the app (`src/lib/view.ts`). It sets `Vary: Sec-CH-UA-Mobile, User-Agent, Cookie`, `Accept-CH: Sec-CH-UA-Mobile`, `Cache-Control: private, no-cache` and a debugging header, `X-Alibi-View`.
  - `/connect` reads the connect request.
- **Merchant and bank** are fully static. Each builds to `dist/` and deploys as a Worker with static assets only.

The adapter's defaults would provision a Cloudflare Images binding and a KV namespace for sessions on deploy. Neither is used, so `astro.config.mjs` sets `imageService: 'passthrough'` and an in-memory session driver. Change both when images or sessions are needed.

## The app view

The phone app has two demo screens, both rendered per request from the backend's JSON (`web/docs/backend-integration.md`):

- `/app/` (and `/` on a phone): Maya's passport home. Alerts and open requests come first, then payments, sharing history, rules and connections.
- `/app/requests/<id>/`: a bank's request, the trace for each question as a numbered chain (question, claim, lookup, sources, calculation, status), and Maya's private review: sent, not sent, sent in less detail, and the approval for one named bank.

`src/lib/api/client.ts` asks the backend for each screen's context with `Accept: application/json`, using the shapes in `src/lib/api/types.ts` (copied from `alibi/app/UI.md`). If `ALIBI_API_ORIGIN` is unset, the backend does not answer, or it answers with HTML or the wrong shape, the screen falls back to the built-in case 1 data in `src/data/fixtures.ts` and says so in a notice. The fixture follows data's case 1 and its answer key, and `tests/fixtures.test.ts` checks it against `agents/data/out/cases/case_0001.json`, including that no forbidden record is ever included.

Actions (run investigation, remove an item, approve) post to `/app/act`. It forwards them to the action path the screen's context gave, accepting only same-backend paths, and returns to the screen with the outcome. A refusal is the backend's `{code, reason}`, shown word for word (`shared/CONTRACTS.md`, Refusals). Being unable to reach the backend is reported as "nothing was sent", never as a refusal. Without a backend, action buttons are disabled.

Run against a backend:

```bash
ALIBI_API_ORIGIN=http://127.0.0.1:8100 pnpm dev:alibi
```

## Not built yet

- Only the passport home and the request screen call the backend. The connect consent and the bank's request form still read static synthetic data.
- No sign-in for the holder, the merchant or the analyst. `backendHeaders()` in `src/lib/screens.ts` is where the holder credential goes once its scheme is published.
- The connect consent stores nothing. The bank's "Send request" button is disabled.
- No visual design: tokens and the logomark are placeholders.
- No PWA manifest or offline support for the app view.
