# Staged: Bank A analyst console for `web/apps/bank`

Owner: `execution`. Built to [`../out/WEB_BANK_NOTES.md`](../out/WEB_BANK_NOTES.md) while `team.json` still gives `web/` to research. When lead grants `web/apps/bank/` to execution, these files move there unchanged (`git mv agents/execution/web_bank/src/* web/apps/bank/src/`, same for `tests/`), together with the two edits below. Not built or type-checked locally (no Node toolchain on this machine); research runs `pnpm verify` after the move.

## New files

| Path (under `web/apps/bank/`) | What |
| --- | --- |
| `src/lib/types.ts` | Analyst inbox and package JSON shapes: `alibi/app/UI.md` plus pinned `attestation`, `redacted`, `txn_ids`, `timing`, verifier `reasons` |
| `src/lib/view.ts` | Pure screen decisions: auth verdict, rejected packages hide evidence, final decisions, grayscale-safe labels, "attested by Alibi", follow-up scope inheritance |
| `src/lib/screens.ts` | Backend routes (`/bank/bank_a/inbox`, `/bank/bank_a/packages/<id>`), bearer credential from `BANK_A_TOKEN`, shape checks, flash, safe return |
| `src/data/analyst_case1.json` | Exact output of `agents.eval.api.build_analyst_preview(canonical_case())`; authentication `preview` (nothing signed or verified) |
| `src/data/analyst.ts` | `packageFixture(id)`, `inboxFixture()` from that JSON |
| `src/pages/inbox.astro` | Inbox: packages bound to this bank, no claim counts for rejected packages, requests sent |
| `src/pages/packages/[id].astro` | Package view; 404 shows "not found" with no existence hint |
| `src/components/Verification.astro` | Three visibly different panels; a rejected package shows only the verifier's reasons |
| `src/components/Evidence.astro` | Evidence by question, settlement timing, reconciliation recomputed by the verifier, contradictions, generic gaps |
| `src/components/Decision.astro` | Read-only analyst, required reason, final clear/escalate, follow-up that keeps the parent's scope |
| `src/lib/actions.ts` | The only two actions (`decision`, `followup`): fixed kind + validated ID -> server-built Bank A path, allowlisted fields; `handleAct` refuses cross-site, unauthenticated and unknown requests before any upstream call |
| `src/lib/session.ts` | Signed analyst session (HMAC-SHA256 cookie via WebCrypto), constant-time compare |
| `src/pages/act.ts` | POST endpoint: same-origin + signed session required, then `forwardAction` to the server-built path; refusals verbatim |
| `src/pages/session.ts`, `src/pages/sign-in.astro` | Demo analyst sign-in with a passcode; sign-in disabled when its secrets are unset |
| `tests/view.test.ts`, `tests/analyst.test.ts` | Pure logic, and the built-in data checked against `agents/data/out/cases/case_0001.json` |
| `tests/actions.test.ts` | No upstream call for the reported exploit (`action=/merchant/admin/seed`), unauthenticated, cross-site and path-trick requests; allowed actions send only allowlisted fields to the server-built path; session tamper and expiry |

## Edits to existing files (research-owned until the grant)

1. `web/apps/bank/astro.config.mjs`, in `env.schema`, after `ALIBI_API_ORIGIN`:
   ```js
   // Bank A's credential for the backend, forwarded as a bearer token from the server only.
   BANK_A_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
   // Analyst sign-in: the demo passcode and the key that signs the session cookie.
   ANALYST_PASSCODE: envField.string({ context: 'server', access: 'secret', optional: true }),
   BANK_SESSION_SECRET: envField.string({ context: 'server', access: 'secret', optional: true }),
   ```
   Research sets the three Worker secrets. Without `BANK_SESSION_SECRET` and `ANALYST_PASSCODE`, sign-in is disabled and no action can reach the backend; with `ALIBI_API_ORIGIN` set, the inbox and package pages redirect to `/sign-in/` until the analyst signs in.
2. `web/apps/bank/src/layouts/ConsoleLayout.astro`, in the header before "Signed in as":
   ```astro
   <nav aria-label="Console">
     <a href="/">Cases</a>
     <a href="/inbox/">Alibi inbox</a>
     <a href="/sign-in/">Sign in</a>
   </nav>
   ```
   with `.console-header nav { display: flex; gap: var(--space-sm); }`.

## Not done yet

- Sending the RFC from `request.astro`: the create-RFC route is not published (`alibi/RUNBOOK.md` is pending). The button stays disabled until then.
- Live wiring is automatic: when `ALIBI_API_ORIGIN` and `BANK_A_TOKEN` are set and the routes return JSON, the screens switch from the preview to live data with no code change.
