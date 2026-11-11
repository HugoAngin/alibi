# Working on web/ together

For the agents (and humans) building parts of the front end in parallel. Read `web/README.md` first, then this.

## Who can push

Anyone on the roster may commit to `web/` and push it to `main` without asking anyone (`open_paths` in `team.json`). The one exception is a folder a member holds inside `web/`, listed under their `paths` in `team.json`; the commit hook refuses those for everyone else.

Stage only what you changed: `git add web/<path>`.

## Rules that keep us out of each other's way

- **Run `pnpm verify` from `web/` before every commit.** It formats, lints, type-checks the app, runs the tests and builds everything.
- **Check shared files when you change them.** The `web/` root (`package.json`, `pnpm-lock.yaml`, configs) and `web/packages/ui/` affect the whole site, so `pnpm verify` must pass for all of them. Post an `fyi` when you change them.
- **If a rebase conflicts, keep your teammate's change** and redo yours on top. Never force-push.
- **Pushing is not deploying.** The Workers deploy from Elio's Cloudflare account, so a push does not change what is live. See `web/README.md`.

## Run it

```bash
cd web && pnpm install
pnpm dev             # http://localhost:4321: Alibi, the bank at /bank/, the marketplace at /shop/
```

The site runs the recorded simulation by default. To work against the backend instead, use `pnpm demo` (alibi/RUNBOOK.md), which sets `ALIBI_SIMULATION=0`.

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
