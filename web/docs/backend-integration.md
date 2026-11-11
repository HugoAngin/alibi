# Front end and backend: the agreed split

Decided by Elio (research's human) on 2026-11-08. This is the source of truth for how the front end and the Python backend fit together. Questions go to research on the board.

## The decision

- **The product front end is the Astro workspace in `web/`, deployed on Cloudflare.** It renders every screen people see: the Alibi landing page, Maya's app (the same URL on a phone, `/phone/` on a desktop), the bank's analyst console (`/bank/`, `apps/alibi/src/bank`) and the merchant (`/shop/`, `apps/alibi/src/shop`).
- **The Python app in `alibi/` is the backend.** It owns connectors, the passport store, the four agents, arithmetic, policy, consent, signing, verification, delivery and recipient isolation. Every decision stays there; the front end only displays results and sends user actions.
- **The Jinja templates in `alibi/app/templates/` stay working as a fallback and debug view.** Research keeps them from breaking but builds no new product screens there.

## The stack

| Part        | Choice                                                                                                                               |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Framework   | Astro 6, TypeScript strict, no client framework. Plain JS only for small interactions.                                               |
| Workspace   | pnpm 10, Node 22.12 or newer. `apps/alibi` (with the bank and merchant inside), `packages/ui`.                                       |
| Hosting     | Cloudflare Workers. Alibi uses `@astrojs/cloudflare` (on-demand pages where a request is read); merchant and bank are static assets. |
| Shared UI   | `packages/ui`: tokens, logomark, "Connect to my Alibi", "Request customer's Alibi", URL contracts (tested).                          |
| Checks      | `pnpm verify`: prettier, eslint, `astro check`, vitest, build.                                                                       |
| Local ports | Alibi 4321, merchant 4322, bank 4323. Backend 8100.                                                                                  |

## What the front end needs from the backend

1. **JSON for every screen.** Each route returns its full context as JSON on `Accept: application/json`, with the same names and shapes as `alibi/app/UI.md`. Lead has already planned this in S4. Keep names stable and give the JSON a version field.
2. **JSON for every action.** Every form action also accepts JSON and answers with JSON: success data, or a refusal `{code, reason}` with the backend's reason verbatim and a 4xx status. The validation must be identical to the HTML path, so a refusal shown in Astro is the backend's, not ours. The actions are: run investigation, remove item, approve, dispatch, export; analyst disposition and follow-up; and the two below.
3. **Two entry points the other sites call.**
   - The bank's "Send request" (`/bank/`, case request screen) creates an RFC: audience, `txn_ids` or period, `question_ids`, expiry. It goes to the mock bank service, which delivers it to the holder.
   - Alibi's `/connect` "Connect" button creates the merchant connection for the holder after consent: `merchant_id` and `context`.
4. **Identity.** The holder session and the two recipient credentials (`bank_a`, `bank_b`) are documented as bearer tokens or cookies. Astro forwards them on the server. Recipient scope is enforced by the backend only; the front end never decides who may see what.
5. **One fixture.** Data's case 1 everywhere (`txn-0001-004`, `PO-0001-01`, `rfc-0001-01`, 240000 − 18000 − 6000 = 216000).
6. **A published route table.** Method, path, request and response JSON for each route, in `alibi/app/UI.md` or `shared/CONTRACTS.md`. Research mirrors the shapes as TypeScript types in `web/`.

## How the calls flow

Astro calls the backend from the server (the dev server or the Cloudflare Worker), never from the browser, so the backend needs no CORS. Pages that call it become on-demand (`prerender = false`). The base URL is an environment variable, `ALIBI_API_ORIGIN`, defaulting to `http://127.0.0.1:8100`.

A deployed Cloudflare site cannot reach `127.0.0.1`. For the live judged demo, either everything runs locally, or the backend gets a public HTTPS URL (for example a Cloudflare Tunnel). Lead decides which; research needs to know by 08:00.

## Until the JSON lands

The Astro screens render static synthetic data from case 1, as they do now. Each screen switches to the backend as soon as its route returns JSON, with no change to the backend.
