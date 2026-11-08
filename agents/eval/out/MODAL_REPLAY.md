# Alibi replay deployment on Modal

Research owns the existing Modal deployment. The S5 entry point below replaces its health-only startup with the public Case 1 demo. The later sections retain the earlier replay plan. [Current hosting plan](../../../web/docs/hosting.md)

## Live deployment handoff

The new entry point keeps Modal app name `alibi-backend` and function name `backend`, which target the existing URL from the same Elio workspace and environment. It starts S5's three loopback services, checks connector and feed seeds, forwards one RFC from each bank, then binds the public router. Hosted health returns 503 if any service is down. The router refuses public connector intake, feed, package delivery, admin and query-token login paths. The holder and bank use server-side bearer credentials.

Deploy from a tracked-only stage. Run from the checkout that has the launcher commit. By default this uses the exact S5 freeze for backend source and overlays only the three reviewed launcher files from the current commit. The stage contains public synthetic cases and no local data, gold or credentials.

```bash
git fetch origin --tags
candidate=$(git rev-parse 'sealed-candidate^{commit}')
launcher=$(git rev-parse HEAD)
stage=$(mktemp -d /tmp/alibi-modal-s5.XXXXXX)
uv run --frozen python -m agents.eval.deploy.replay_stage release \
  --candidate "$candidate" --dest "$stage" > "$stage.manifest.json"
for file in router.py s5_host.py s5_modal.py; do
  git show "$launcher:agents/eval/deploy/$file" > "$stage/agents/eval/deploy/$file"
done
cd "$stage"
ALIBI_MODE=rules ALIBI_MODAL_SECRET=alibi-backend \
  uvx --from modal==1.6.3 modal deploy agents/eval/deploy/s5_modal.py
```

The Modal Secret needs `ALIBI_HOLDER_TOKEN`, `ALIBI_BANK_A_TOKEN`, `ALIBI_BANK_B_TOKEN` and a valid Fernet `ALIBI_MASTER_KEY`. Configure the same holder bearer as the holder Worker's server-only `ALIBI_HOLDER_TOKEN`, and the Bank A bearer as the bank Worker's `BANK_A_TOKEN`. The analyst also needs the bank Worker's `BANK_SESSION_SECRET` and `ANALYST_PASSCODE`. For agents mode set `ALIBI_MODE=agents` and add `GEMINI_API_KEY` to the Modal Secret. Use `ALIBI_REPLAY=1 ALIBI_MODE=agents` only for a labelled replay. The default rules mode was tested locally from the staged backend.

Verify hosted `/healthz` returns all three flags true. With no bearer, `/passport` should return 401 and connector intake should return 404. Then sign in to the holder and bank Workers and check the real package handoff and an analyst disposition.

## Earlier replay staging notes

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

The separate public replay stage can now be generated without copying the
working tree. Keep the manifest beside the stage so it is available for review.

```bash
stage=$(mktemp -d /tmp/alibi-modal-replay.XXXXXX)
uv run --frozen python -m agents.eval.deploy.replay_stage dry --dest "$stage" > "$stage.manifest.json"
```

Dry stage reports missing release files. For release, use `release --candidate
<full SHA>` with a fresh destination. It requires the tracked S5 runbook and
public replay inputs before writing a snapshot. Review the manifest and run
the published service commands from that stage before uploading it.
