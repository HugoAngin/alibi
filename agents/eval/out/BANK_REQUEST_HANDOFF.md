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

## Fresh-start control

The Modal launcher issues Bank B at startup and leaves Bank A for the analyst to send. The public Bank A 404 is therefore consistent with a fresh demo. The signed-in action and subsequent holder readback distinguish that state from a delivery failure.

On a disposable rules-only stack at `2026-11-05T09:00:00Z`, the frozen `58bf0d4` services ran behind the same router source (`4fb85f2`) and the current Bank Worker source (`19f0a56`). Before sign-in, Bank B returned 200, Bank A returned 404 and the Bank A inbox had no sent requests. A signed-in analyst opened case `FC-0147` and submitted the request via the Worker's `/act/`. The Worker returned 303, the bank inbox then listed `rfc-0001-01`, and the holder's authenticated readback through the router returned 200. The hosted analyst session still needs its own send and readback.

## Recovering an already-recorded request

On a disposable `58bf0d4` stack, an injected first Bank A forwarding refusal returned 201 and left Maya's request page at 404. Running the existing driver on that same signed request returned `reissued: true` and `forwarded.status: accepted` via its direct relay fallback. The holder readback then returned 200 for the same request ID.

If the hosted analyst confirms this state, Research can run the existing driver inside the Modal container from `/root/repo` and verify the same holder readback. The deployment owner supplies access to that container.

```bash
python -m alibi.demo rfc bank_a --request-id rfc-0001-01 \
  --app-url http://127.0.0.1:8100 --bank-url http://127.0.0.1:8102 \
  --tokens /tmp/alibi/bank_tokens.json
```
