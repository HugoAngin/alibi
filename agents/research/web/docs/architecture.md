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

`AppShell.astro` is a phone layout: a header, one screen at a time, and a bottom tab bar. Screens switch with anchors and CSS `:target`, with no client-side JavaScript; the passport screen shows when nothing is targeted. On a desktop the shell stays phone-width and centred.

`/phone/` is the desktop preview behind the landing page's **View on a phone** button. `PhoneFrame.astro` loads `/app/` in an iframe at the phone viewport size set in the tokens (`--device-w`, `--device-h`), so the app's layout behaves as it does on a device. The frame loads `/app/` rather than `/?view=app`, so previewing never sets the view cookie. Links in the app that should leave the preview, such as Settings' link back to the website, use `target="_top"`.

The screens show Maya's canonical case: the alert raised before any request, the £2,160 credit, and its reconciliation computed from integer pence. Requests, sharing history and settings are placeholders.

## Not built yet

- No backend calls. The app, the connect consent and the bank's request form read static synthetic data. The live workflow (investigation, review, approval, signing, verification) is the Python app in `alibi/`. The Astro app will call it, or replace its screens, once its routes are published.
- No sign-in for the holder, the merchant or the analyst.
- The connect consent stores nothing. The bank's "Send request" button is disabled.
- No visual design: tokens and the logomark are placeholders.
- No PWA manifest or offline support for the app view.
