# Elio UI handoff

The patch assembles Hugo's landing hero and merchant seller pages over Research's Astro workspace at `2842b58`. It fixes the holder flow against the merged local Python app. Research owns applying it to `web/`. For Bank A, use [Execution's bank-service integration](https://github.com/eliohd/winthrutheory-eceb0cdd-submission-repo/commit/e48e669).

```bash
git apply --check agents/eval/out/elio_ui_handoff.patch
git apply agents/eval/out/elio_ui_handoff.patch
cd web
pnpm install --frozen-lockfile
pnpm verify
```

- The holder approves the current digest, audience and policy version on the review page. Sending is a separate action. The holder proxy forwards the supported request actions and rejects cross-origin and out-of-scope POSTs.
- The Astro holder client sends the approval fields as form data and requests a JSON response.
- The merchant connect control uses the fixture while the authenticated connection route is pending.

## Verification

- `pnpm verify` passed with 42 tests and all three apps built on the exact handoff patch. The earlier combined staging build with the in-process Bank A console passed 66 tests.
- Chrome drove the Astro holder UI against the running local Python app. Approval left the bank inbox empty. Changing the review and sending from a stale tab showed the backend's `approval_digest_mismatch` reason verbatim and still left the inbox empty. Fresh approvals yielded distinct Bank A and Bank B receipts. Bank A received 404 when fetching Bank B's package. The holder proxy returned 403 for a cross-origin POST and 400 for a forged bank action.
- Chrome signed into a staging copy of Bank A's console wired to the app's in-process bank routes and recorded an escalation. The backend stored the decision and the UI rendered it as final.
- The holder smoke used a local Clerk middleware stub because the development handshake returned 500. The patch preserves the original middleware. Both runs used the merged rules-only backend and the same Case 1 store. The checks prove UI controls and receipt delivery. The rules run still returned unresolved claims in the revised Bank A draft and the Bank B period request.
- F3 is addressed separately by the eval router at `agents/eval/deploy/router.py`. Its 67 tests include rejection of admin routes with zero upstream calls. Arnav's service-level admin controls remain part of the hosting gate.
