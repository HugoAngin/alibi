# Moved: Bank A analyst console now lives in `web/apps/bank`

Landed on main by execution after lead moved `web/` to execution (e1be433), together with eval's UI handoff patch (`agents/eval/out/elio_ui_handoff.patch`, 584a8aa). The staged copy that was here is removed so there is one source; history keeps it.

Verified in `web/` before the commit (Node 22.23.4, pnpm 10.33.0 via `npm exec`): `pnpm verify` passed, `astro check` 0 errors / 0 warnings in all three apps, vitest 76 tests in 12 files, all three builds.

The original staging notes follow.

# Staged: Bank A analyst console for `web/apps/bank`

Owner: `execution`. Built to [`../out/WEB_BANK_NOTES.md`](../out/WEB_BANK_NOTES.md) while `team.json` still gives `web/` to research. When lead grants `web/apps/bank/` to execution, these files move there unchanged (`git mv agents/execution/web_bank/src/* web/apps/bank/src/`, same for `tests/`), together with the two edits below. **Verified 2026-11-08 06:26Z:** overlaid on a copy of `web/` at main with the two edits below, `pnpm verify` (Node 22.23.4, pnpm 10.33.0) passed: prettier, eslint, `astro check` 0 errors / 0 warnings in all three apps, vitest 74 tests in 11 files, all three builds. Files here are the prettier-formatted versions.

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
| `src/lib/bankApi.ts` | Live bank service (`alibi/bank/service.py`) JSON checks and conversion to the console's shapes; bank routes `/inbox`, `/packages/{id}`, `/packages/{id}/disposition` |
| `src/lib/load.ts` | Loads inbox and package from the bank; a real 404 stays "not found", anything else falls back to the preview with the reason |
| `src/data/live_samples/*.json` | Real bank responses for case 1 (verified package, rejected tampered copy, after `request_more`, `disposition_final` refusal, inbox) |
| `tests/bankApi.test.ts` | Conversion checked against those real responses |
| `tests/actions.test.ts` | No upstream call for the reported exploit (`action=/merchant/admin/seed`), unauthenticated, cross-site and path-trick requests; allowed actions send only allowlisted fields to the server-built path; session tamper and expiry |

## Edits to existing files (research-owned until the grant)

1. `web/apps/bank/astro.config.mjs`, in `env.schema`, after `ALIBI_API_ORIGIN`:
   ```js
   // Bank A's credential for the backend, forwarded as a bearer token from the server only.
   BANK_A_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
   // The bank service directly (e.g. http://127.0.0.1:8102); unset means ALIBI_API_ORIGIN + /bank.
   BANK_API_ORIGIN: envField.string({ context: 'server', access: 'secret', optional: true }),
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

## Live wiring (lead, 06:07Z)

- Reads `GET /inbox` and `GET /packages/{id}`; writes `POST /packages/{id}/disposition` with `{decision, reason}` (reason at most 300 characters). A follow-up is `request_more` with `requested_evidence: [question]`; the bank issues the signed follow-up request with the parent's scope.
- Target: `BANK_API_ORIGIN` if set (no prefix), else `ALIBI_API_ORIGIN` with the router's `/bank` prefix. Token: `BANK_A_TOKEN` as a bearer (the bank's `ALIBI_BANK_A_TOKEN`).
- Checked on 2026-11-08: the converter and planner, run with Node on the real samples, and the planned bodies posted to a real `create_bank_app` for case 1. The follow-up created `rfc-0001-01-f1` scoped to `txn-0001-004`; `clear` was recorded; a second decision got `409 disposition_final`, shown verbatim.
- Samples were generated with `create_bank_app` + case 1 through `alibi.api` (the same steps as `alibi/tests/test_bank_service.py`'s `built` fixture), then `/deliver`, `/inbox`, `/packages/{id}` and `/disposition`.

## Not done yet

- Sending the RFC from `request.astro`: the create-RFC route is not published (`alibi/RUNBOOK.md` is pending). The button stays disabled until then.
- Live wiring is automatic: when `ALIBI_API_ORIGIN` and `BANK_A_TOKEN` are set and the routes return JSON, the screens switch from the preview to live data with no code change.
