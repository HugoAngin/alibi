# Working on web/ alongside research

For the agents (and humans) building parts of the front end in parallel. Read `web/README.md` first, then this.

## Who owns what

Ownership follows `team.json`. Lead grants the paths; until your path is listed there, the commit hook refuses your changes.

| Owner     | Path                                                                                                            | Builds                                                                  |
| --------- | --------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| execution | `web/apps/bank/`                                                                                                | The bank's analyst console, to `agents/execution/out/WEB_BANK_NOTES.md` |
| data      | `web/apps/merchant/`                                                                                            | The marketplace and its connect flow, with Maya's case 1 records        |
| research  | `web/apps/alibi/`, `web/packages/ui/`, `web/docs/`, the `web/` root (`package.json`, `pnpm-lock.yaml`, configs) | The Alibi site and app, shared UI, integration, every Cloudflare deploy |

Commit only your own folder: `git add web/apps/<yours>/`.

## Rules that keep us out of each other's way

- **No new dependencies yourself.** `web/pnpm-lock.yaml` is research's. Post a `need` to research with the package name and why; research adds it.
- **Shared code is read-only for you.** Import from `@alibi/ui/*` (tokens, logomark, the two Alibi buttons, the backend client). For a change there, post a `need` to research.
- **Run `pnpm verify` from `web/` before every commit.** It formats, lints, type-checks all three apps, runs the tests and builds everything.
- **Research deploys.** Push, then post `done` to research saying what changed. Research builds and deploys the Workers from Elio's Cloudflare account and replies with what is live.

## Run it

```bash
cd web && pnpm install
pnpm dev:bank        # http://localhost:4323
pnpm dev:merchant    # http://localhost:4322
pnpm dev:alibi       # http://localhost:4321 (the Connect button lands here)
```

To work against the backend, set `ALIBI_API_ORIGIN`, for example `ALIBI_API_ORIGIN=http://127.0.0.1:8100 pnpm dev:bank`. Unset, your screens must still work on built-in data.

## Talking to the backend

The contract is `web/docs/backend-integration.md`. In short:

- Call the backend from the server only (an Astro page's frontmatter, or an endpoint like `src/pages/.../x.ts`), never from browser JavaScript. Both apps already render per request on the Worker.
- Use the shared client: `loadContext()` and `forwardAction()` from `@alibi/ui/api`. They fall back cleanly when the backend is not answering, and never invent a refusal. See how `web/apps/alibi/src/pages/app/requests/[id].astro` and `src/pages/app/act.ts` use them.
- Through the public Modal router, the bank's routes are under `/bank/...` and the merchant's under `/merchant/...` (`agents/research/deploy/router.py`). Exact paths and JSON shapes: the analyst contexts in `alibi/app/UI.md`, and the route table in `alibi/RUNBOOK.md` once S5 lands.
- Refusals are the backend's `{code, reason}`, shown word for word (`shared/CONTRACTS.md`, Refusals). Never write your own reason.
- Credentials (the bank's token) are Worker secrets, read server-side through `astro:env/server` with `access: 'secret'`. Never in the page, never in a `PUBLIC_` variable. Research sets them on the Worker; post a `need` with the variable name.
- Built-in data must look like the backend's JSON shape and match the canonical fixture, with a test against `agents/data/out/`. The pattern is `web/apps/alibi/src/data/fixtures.ts` and `tests/fixtures.test.ts`. When a screen shows built-in data, say so on screen.

## Look and feel

- Tokens: `web/packages/ui/src/tokens.css`. The bank and the merchant should look like someone else's product, so override tokens in your own layout (see each app's layout file). Do not restyle the Alibi buttons.
- Keep the "simulated, synthetic data" banner on every page.
- Phone width works (16 px gutters, no horizontal scroll), and status never depends on colour alone.
