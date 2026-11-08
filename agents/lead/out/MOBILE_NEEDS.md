# Mobile app redesign: needs outside the holder app folders

From lead's mobile redesign (2026-11-08), which changed only `web/apps/alibi/src/components/app/` and `web/apps/alibi/src/pages/app/`. Each item below is a file Mouaad owns, or a backend field. None of them blocks the redesign; it works without them.

## For Mouaad (web/)

1. **`web/apps/alibi/src/lib/screens.ts`: add two routes to `ROUTES`.** Home calls `/txn/<id>` (one call per transaction, for the Financial Context timeline), and the package download calls `/requests/<id>/export.json`. Both paths are written inline today, in `components/app/PassportHome.astro` and `pages/app/requests/[id]/export.json.ts`. Suggested:
   ```ts
   txn: (id: string) => `/txn/${encodeURIComponent(id)}`,
   exportJson: (id: string) => `/requests/${encodeURIComponent(id)}/export.json`,
   ```
   Then tell lead and the two call sites switch to `ROUTES`.
2. **`web/apps/alibi/tests/home.test.ts`: unit tests for `components/app/home.ts`.** The vitest include pattern only covers `apps/*/tests/`, so the pure functions (`requestState`, `activity`, `headline`, `banks`, `contextTimeline`, `describe`, `initials`) are covered by `astro check` and the build only. Lead can write the test file if you prefer; it needs a path in your folder.
3. **`pnpm format:check` fails on Windows checkouts.** With `core.autocrlf=true`, git writes CRLF and Prettier's default `endOfLine: lf` flags all 124 files, including ones nobody touched. `npx prettier --check --end-of-line auto .` passes for the whole workspace. Fix with `"endOfLine": "auto"` in `web/.prettierrc.json`, or `* text=auto eol=lf` in a `web/.gitattributes`. Lint, check, test and build all pass on this machine.
4. **`web/apps/alibi/src/lib/api/types.ts` and `guards.ts`: declare `audience`.** The backend sends `audience` (`bank_a`, `bank_b`) on `requests` and `sharing_history`, but `PassportHome` does not declare it, so `components/app/home.ts` reads it through a cast and falls back to the display name. Please add `audience: string` to both.
5. **`web/apps/alibi/src/middleware.ts`: protect `/app/*` under Clerk.** Demo holder mode already requires a session on holder routes. In the Clerk path, `clerkMiddleware()` runs without a route matcher, so `/app/` pages render for anyone. The two new download endpoints (`/app/account/export.json`, `/app/requests/<id>/export.json`) check sign-in themselves; the pages do not.
6. **`web/apps/alibi/src/pages/index.astro`: optional.** It passes `active="passport"` to `AppFrame`. That still works (`passport` and `requests` map to Home, `settings` to Account), but `active="home"` reads better.

## For the backend (lead / build sessions)

7. **Bank connections have no `audience`.** Home pairs a bank feed with its recipient by the id prefix `conn-<audience>-` (`conn-bank-a-maya-001` for `bank_a`). An `audience` field on bank connections would replace that.
8. **Alert titles carry raw minor units:** `Unexplained credit of 216000 GBP`. Home rewrites a trailing `<minor> <CCY>` as money (`£2,160.00`) so it cannot read as £216,000. `amount: {minor, currency}` on the alert would remove the guess.
9. **No "declined by the holder" request status.** The app shows `Declined by you` if `status` is ever `declined`; today the backend has no decline action, so no request can reach that state.
10. **The second platform is not a connection in case 1.** Home lists merchants exactly as `GET /passport` returns them (`kind != bank`), so case 1 shows Mock Marketplace alone and "See all" only appears with more than three.
