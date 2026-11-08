# Hosting plan

Decided by Elio on 2026-11-08. The demo runs live: the merchant, Alibi and the bank talk to each other in real time, on the internet.

## Where each part runs

| Part                                                                    | Host                                   | Notes                                                                                                                                 |
| ----------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Alibi site and app (`web/apps/alibi`)                                   | Cloudflare Worker                      | Astro with `@astrojs/cloudflare`. Clerk accounts (`alibi-app`).                                                                       |
| Mock merchant site (`web/apps/merchant`)                                | Cloudflare Worker                      | Static today; becomes on-demand where it calls the backend.                                                                           |
| Bank console (`web/apps/bank`)                                          | Cloudflare Worker                      | Becomes on-demand: inbox, package view, "Send request".                                                                               |
| Python backend (`alibi/`): app 8100, mock merchant 8101, mock bank 8102 | **Modal**, Elio's account ($51 credit) | One CPU container, as in eval's `agents/eval/out/MODAL_REPLAY.md`, but live: Gemini on.                                               |
| Web-side state                                                          | Cloudflare D1 (SQLite)                 | Only what the web needs: Clerk-linked accounts, merchant connection status, the bank's case queue. The passport stays in the backend. |

## The backend on Modal

This follows Tamim's topology (`agents/eval/out/MODAL_REPLAY.md`), with three changes for the live demo:

1. **Live, not replay.** `GEMINI_API_KEY` and `ALIBI_MASTER_KEY` come from a Modal Secret that Elio creates in the Modal dashboard. Nobody pastes them into chat or code.
2. **All three services reachable.** The bank and merchant Workers need the bank service (`POST /rfc`, inboxes, analyst actions) and the merchant service, not only the app. A small router in the same container publishes one HTTPS endpoint (`@modal.web_server`) and forwards `/merchant/*` to 8101, `/bank/*` to 8102 and everything else to 8100. Loopback-only routes (seed, reset) stay unreachable from outside.
3. **Kept warm for the judging window.** One container, `min_containers=1` during review, so the SQLite store persists between requests. A restart reseeds from case 1, as in Tamim's plan.

Service start commands, environment variables and readiness checks come from S5's `alibi/RUNBOOK.md` (due about 03:30). The deploy file lives in `agents/research/deploy/modal_app.py` and is deployed from Elio's Modal account with `modal deploy`.

## How the Workers reach it

Each Worker calls the Modal URL from the server only, with `ALIBI_API_ORIGIN` (and the bank and merchant equivalents) set as Worker variables. Bank tokens and any backend credential are Worker secrets (`wrangler secret put`), never in the browser bundle, as lead asked.

## Fallbacks

- If Modal is down: run the backend on the presenter's laptop and expose it with a Cloudflare Tunnel; the Workers only change `ALIBI_API_ORIGIN`.
- If the network is down: everything local (Astro dev server plus the three services), as in lead's original plan.
- The Jinja screens stay the verified fallback for the product UI.

## Cost

One small CPU container kept warm for a few hours costs a few dollars at most, well inside the $51 credit. Gemini calls are billed to the Gemini key, not Modal. Stop the app with `modal app stop` after judging.
