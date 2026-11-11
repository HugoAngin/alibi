# Hosting

Since 2026-11-11 the public demo is a **recorded simulation** on one Cloudflare Worker, with no backend, no API keys, no secrets and no accounts. The live setup used for judging (Python backend on Modal, Gemini, Clerk, three Workers on `alibi-app.workers.dev`) is retired; its history is in git.

## Where it runs

| Part                             | Address                                    |
| -------------------------------- | ------------------------------------------ |
| Alibi (landing page, Maya's app) | https://alibi.eliohaddad.workers.dev/      |
| Marketplace (simulated)          | https://alibi.eliohaddad.workers.dev/shop/ |
| Bank console (simulated)         | https://alibi.eliohaddad.workers.dev/bank/ |

One Worker, `alibi` (`apps/alibi/wrangler.jsonc`), on the account's workers.dev subdomain, `eliohaddad`. Its only setting is `ALIBI_SIMULATION="1"`. A custom domain can be added later as a route in `wrangler.jsonc`; the site needs no change for it.

## How the simulation works

`pnpm record:simulation` (from `web/`) starts the real Python stack once on this machine, with recorded Gemini answers (`ALIBI_REPLAY=1`, no key), walks every path the site can take through Maya's case, and saves what each backend route answered at each step to `packages/ui/src/sim/recording.json`: a graph of backend states joined by the site's actions. Run it again only if the backend or the case changes.

At run time the middleware serves each request against that recording (`packages/ui/src/sim/`): the same backend clients (`@alibi/ui/api`, `@alibi/ui/demo`) answer from the recording instead of the network, so the screens, converters and refusals run unchanged. A visitor's place in the demo lives in one cookie, `alibi_sim`: withdrawing at `/shop/` shows at `/bank/`, requesting at `/bank/` shows in Maya's dashboard. Each visitor runs their own copy, and Reset demo starts it over. A step the recording does not have is refused with a plain message, and nothing changes.

What a visitor can do: withdraw, request, run the investigation, remove one item and restore it, approve and send (or see the send refused after a change), and, as the analyst, clear, escalate or ask a follow-up question, which Maya can answer. The analyst's reasons and question are the visitor's own words, kept in the cookie.

## Deploy

With a Cloudflare account logged in (`pnpm exec wrangler login` from `apps/alibi`):

```bash
pnpm --filter @alibi/site run deploy
```

The recording adds about 0.5 MB (gzipped) to the Worker, well inside the free plan's limit.

## The local live demo

The real backend still runs locally, for re-recording or for a live demo on one laptop: start `uv run --frozen python -m alibi.demo --case 1 --reset` and then `pnpm demo`, which turns the simulation off (`ALIBI_SIMULATION=0`) and points the site at the local services (`alibi/RUNBOOK.md`).
