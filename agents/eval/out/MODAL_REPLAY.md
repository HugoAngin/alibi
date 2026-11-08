# Alibi replay deployment on Modal

This is the earlier replay proposal. The current team plan targets the live demo on Research's existing Modal app, with the local stack as fallback. Research owns that deployment. Reuse it for any later hosted replay if the team requests one. The checks below apply to a separately labelled replay of public synthetic data. [Current hosting plan](../../../web/docs/hosting.md)

## Live deployment handoff

Eval prepares and tests the existing `alibi-backend` app after the path assignment and S5 runbook land. Research deploys it from Elio’s workspace and shares the logs and health result. The router currently answers `/healthz` with all three service flags false. Its top-level `ok` indicates only that the router responded.

Prepare the deploy from a tracked-only snapshot of the approved commit. The deployment module imported successfully from this layout with Modal 1.6.3. The existing ignore patterns alone allow arbitrary untracked JSON files under a mounted directory. This snapshot excludes untracked exports, credentials and scratch data from the deployment input.

```bash
candidate=$(git rev-parse HEAD)
stage=$(mktemp -d /tmp/alibi-modal-stage.XXXXXX)
git archive "$candidate" | tar -xf - -C "$stage"
```

Once the runbook lands, verify the three process commands and their readiness routes from that snapshot. Research attaches the `alibi-backend` Secret in Elio’s workspace. Modal can require `GEMINI_API_KEY` and `ALIBI_MASTER_KEY` by name without exposing their values. [Modal Secret reference](https://modal.com/docs/sdk/py/latest/Secret#from_name)

Before starting the hosted upstreams, confirm that merchant seed, reset and debug routes stay internal even through a service prefix. Start with a disposable local stack for mutating probes. Then verify all three hosted health flags, both recipients' allowed reads, the live consent refusal and the refusal's JSON body. Record the commit and observed results for Research's web smoke test.

## Topology

| Service | Bind address | Purpose |
| --- | --- | --- |
| App | `0.0.0.0:8100` | The only externally routed port. Serve the published console and judge replay actions. |
| Mock merchant | `127.0.0.1:8101` | Synthetic events, settlement lookup, and the internal seed action. |
| Mock bank | `127.0.0.1:8102` | RFC intake, package verification, recipient inboxes, and analyst actions. |

Run the three services in one CPU-only Modal container. Publish port 8100 with `@modal.web_server(8100, startup_timeout=60)`. Use the S5 runbook's process commands and readiness checks. The wrapper starts the loopback services before the app, exits if either fails, and checks the app before announcing readiness. Pin one container and leave request concurrency at its default for the first replay. Warm one container only for the review window. Each startup seeds a fresh local SQLite store from the same public replay inputs. The single store is reused between requests during that container's life. [Web server decorator](https://modal.com/docs/sdk/py/latest/web_server) · [Container limits](https://modal.com/docs/sdk/py/latest/App#function) · [Input concurrency](https://modal.com/docs/guide/concurrent-inputs)

Use container-local storage for the writable passport and bank inbox. A replacement container starts from the signed replay seed and the labelled initial state. Shared Volume storage is unnecessary for this replay. Modal Volumes require explicit consistency handling and do not provide distributed file locking for concurrent writers. [Modal Volume consistency](https://modal.com/docs/guide/volumes)

## Build and access

1. Use the repository's Python version and `Image.uv_sync(frozen=True)` with the pinned lockfile. Add the specific source trees, templates, static assets, and public synthetic replay fixtures required by the published commands. Stage them from tracked paths in the clean checkout. Inspect the staged file list before upload. Sealed inputs, gold, local databases, scratch directories, environment files, private keys, and model logs stay outside the build context. [Modal Images](https://modal.com/docs/sdk/py/latest/Image#uv_sync)
2. Store the replay master key and synthetic service credentials in a Modal Secret. Place signing keys and the master key outside the passport data directory. Start the app in the S5 replay mode, with Gemini credentials absent. Replay startup fails if a required capture is missing. [Modal Secrets](https://modal.com/docs/guide/secrets)
3. Start with proxy authentication for team verification. The judge-facing version serves a public read-only replay view, or uses the app's server-side judge session for the scripted actions. All holder, analyst, connector, and reset credentials stay server-side. The seed and reset routes remain loopback-only. Check the actual public route list before enabling anonymous access. [Modal proxy authentication](https://modal.com/docs/guide/webhook-proxy-auth)

## Release checks

- Follow the pinned S5 start and seed commands in a clean checkout. Verify cold startup, a second request through the same warm container, and a fresh reset after restart.
- Rehearse the public Cases 1 and 2. Case 1 must show the £2,160 credit with its signed proof, refuse a modified draft under stale consent, then accept a new approval. Case 2 must show the unresolved pending settlement. Confirm Bank B reuses the source store with fresh consent and receives only its own package.
- Fetch both recipient exports and verify their original JSON using the independent verifier with separately pinned public keys. Confirm credential scope, replay labelling, and absence of live model calls. Review the public asset list and response captures for credentials and private file paths.
- Record the tested Git commit, replay artifact identifier, cold and warm timings, and deployed URL on the Message Board. Label the URL as replay in the deck. Deploy with `modal deploy` after the local checks pass. Stop the optional app with `modal app stop` after review. [Deploying Web Functions](https://modal.com/docs/guide/webhooks) · [App CLI](https://modal.com/docs/cli/latest/app)

The service entry points, replay flag, and exact readiness checks come from the S5 runbook.
