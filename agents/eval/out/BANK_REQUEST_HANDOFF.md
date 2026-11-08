# Bank request delivery feedback

At 11:50 UTC, the public holder dashboard loaded live Case 1 and listed Bank B. Bank A request `rfc-0001-01` returned 404. The public Modal health checks showed all three services healthy and the app in rules-only mode. The signed-in hosted submission response still needs capture by the deployment owner.

A disposable test used the frozen bank endpoint with a synthetic holder refusal. The first POST returned 201 with `forwarded.status: refused`, while the request appeared in the bank inbox. The second POST returned 200 with `forwarded: null`, and the bank made no second forwarding call. The current Bank Worker treats both responses as success and tells the analyst the request reached Maya.

`bank-request-delivery.patch` changes only the Bank Worker. It reports success after `forwarded.status: accepted`, preserves a typed holder refusal and surfaces unconfirmed delivery. This corrects the analyst message. If hosted forwarding failed, the deployment owner still needs to check the relay and verify that the holder receives the request. The frozen app and sealed benchmark remain separate.

```bash
git apply --check --whitespace=error agents/eval/out/bank-request-delivery.patch
git apply agents/eval/out/bank-request-delivery.patch
(cd web && pnpm exec vitest run apps/bank/tests/actions.test.ts && pnpm --filter @alibi/bank check)
```

Verified with 14 Bank Worker action tests and the Bank Astro check in a disposable copy.
