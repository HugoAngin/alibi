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
| `src/pages/act.ts` | Forwards decision and follow-up to the backend action path with `forwardAction`; refusals verbatim |
| `tests/view.test.ts`, `tests/analyst.test.ts` | Pure logic, and the built-in data checked against `agents/data/out/cases/case_0001.json` |

## Edits to existing files (research-owned until the grant)

1. `web/apps/bank/astro.config.mjs`, in `env.schema`, after `ALIBI_API_ORIGIN`:
   ```js
   // Bank A's credential for the backend, forwarded as a bearer token from the server only.
   BANK_A_TOKEN: envField.string({ context: 'server', access: 'secret', optional: true }),
   ```
   Research sets the Worker secret `BANK_A_TOKEN` once the backend publishes the bank credential.
2. `web/apps/bank/src/layouts/ConsoleLayout.astro`, in the header before "Signed in as":
   ```astro
   <nav aria-label="Console">
     <a href="/">Cases</a>
     <a href="/inbox/">Alibi inbox</a>
   </nav>
   ```
   with `.console-header nav { display: flex; gap: var(--space-sm); }`.

## Not done yet

- Sending the RFC from `request.astro`: the create-RFC route is not published (`alibi/RUNBOOK.md` is pending). The button stays disabled until then.
- Live wiring is automatic: when `ALIBI_API_ORIGIN` and `BANK_A_TOKEN` are set and the routes return JSON, the screens switch from the preview to live data with no code change.
