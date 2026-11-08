# Alibi evaluation case format

The agreed case format is one JSON object per line. Cases reuse the public ledger shape documented in [agents/data/README.md](../data/README.md). Keep case inputs and gold in separate files, joined by `case_number`. The model and runtime receive only the current request and permitted evidence. The evaluator retains case metadata, answer keys and the full future event stream.

| Input | Format |
| --- | --- |
| Identity | `case_number` as an integer, `family`, `variant`, `title`, `label` and `subject_ref`. Numbers below 1000 are development. Numbers at or above 1000 are sealed. Family, variant and title are evaluator metadata. |
| Timed ledgers | `merchant_deliveries[]` of `{received_at, event}` and `bank_deliveries[]` of `{received_at, row}`. Process in arrival order. Preserve duplicates and corrections. At each decision, expose only records available by then. |
| Requests | `rfcs[]` with `request_id`, `parent_request_id`, `audience`, `subject_ref`, `txn_ids` or `period`, `question_ids`, `questions` with their wording, `issued_at` and `expires_at`. Use `bank_a` and `bank_b`, UTC ISO 8601 and half-open periods. Reuse the passport across requests and require separate approval. |
| Additional evidence | `merchant_lookup[]` of `{available_from, event}` is visible only through a permitted lookup. Supply exactly one selector: `payout_reference`, `record_id`, `covers_record_id` or `payout_date` as a UTC date. `imports[]` of `{received_at, record}` is labelled `self_supplied`. Keep original wording in the source body. Optional `privacy_policy` supplies the holder's restrictions before preview. |
| Holder claim | `holder_claim` with `txn_id`, `explanation` and `stated_minor` breakdown. Use integer minor units and an explicit currency on money records. |

Gold joins by case number and request ID. Each request records the decision time, link state and source versions, expected claim status and verifiable calculation, required facts, alternative sufficient evidence sets, genuine gaps and forbidden disclosure paths or values. Evidence references are pairs of source ID and revision. Set `requires_retrieval` for answers that depend on lookup-only evidence. Keep gold in the evaluator's private file.

Gold can name `forbidden_fields` as exact field names and `forbidden_values` as distinctive literal strings. The capture must include `package_body`, the recipient-visible package including attachments and metadata, whenever either restriction is present. Use JSON null for a refusal with no delivered package. The scorer checks nested fields and strings, including summaries. Review the PDF separately.

For each request, return a machine-readable result with the request ID, observed links and revisions, per-question claim statuses, citations, calculations and gaps, the actual approved package body or a refusal, and the application outcome. Record latency, model and tool calls, usage, lookup counts, observed inputs, unsafe proposals before policy checks and any error as evaluator metadata. Preserve denied, unresolved, timeout and invalid outcomes. Use the same evaluator input contract across the rules baseline, one-pass Gemini and full Alibi. Exact production route names and the adapter entry point are supplied by the app owner.

Security checks use Mouaad's published probe IDs and `agents.execution.api.summarise` for observed `accepted` or `refused` outcomes. Run allowed controls as well as refusal probes. Keep unavailable application routes distinct from a verified refusal.

For a multi-step test, call `agents.eval.api.score_probe_steps(plan, observations)`. Join observations to the plan by `case_number`, `request_id` and `step_id`. Capture what the app actually did as `accepted`, `refused` or `error`. If an observation supplies a `probe_id`, it must match the plan. Set `expected_outcome` on a planned step when it differs from the probe default, such as an accepted identical retry before a refused conflicting retry. Feed the full report to the capture scorer so missing and errored steps remain separate. The catalogue denominator stays fixed.

For the integrated app, use the eval runner with a git-ignored config for a disposable synthetic stack. The config has `base_urls` (service origins, `routes` and `fixtures`) and `tokens` (bearer credentials by actor). Optional `probes` entries keyed by probe ID override those settings. Give mutating probes independent stores or endpoints so one attempt cannot reuse another attempt's approval or delivery. Run a subset with `--ids` during setup. Every omitted probe remains `not_run` in the official results file.

```bash
uv run --frozen python -m agents.eval.run_probes --config data/alibi/live-probes.json \
  --results data/alibi/probes.jsonl
```

Pass that JSONL to `alibi.eval.scorer --probe-results`. The file records HTTP statuses and outcomes without storing payloads or credentials. For SP-47, set `storage_layout` to `configured` or `default` only after confirming the matching layout for the target. That result covers the isolated passport core store. Check the actual app and bank state files separately before claiming evidence is encrypted throughout the demo.

For the app and bank check, use public Case 1 with a disposable master key and
signing keys outside the data directories. Run, approve and dispatch Bank A's
request, then verify the receipt against the package in the bank view. Inspect
the scratch S4 `case_1.state.json` entry
`["bank:bank_a:packages"][package_id]["raw"]` and the S2 `state.json` entry for
that package under `recipients.bank_a.inbox`. A plaintext signed envelope whose
canonical digest matches its accepted receipt proves exposure even when the
passport database is encrypted. Record the finding without retaining the package.

For native bank checks SP-25 to SP-32, issue a real Bank A request and approve and dispatch a public case package that includes a full issuer-signed record. Put its ID in `fixtures.native_verifier_package_id` to select the native handlers. Supply the matching throwaway package and record signing key paths as `fixtures.native_verifier_package_pem` and `fixtures.native_verifier_record_pem`, outside Git. Configure `bank_a` and `bank_b` tokens and a disposable loopback `bank` origin. A second bank service can be selected with `fixtures.native_verifier_service` if it handles both recipient identities. The default routes are `/deliver`, `/inbox` and `/packages/{package_id}`; override them as `native_verifier_deliver`, `native_verifier_inbox` and `native_verifier_package` if the runbook uses a prefix. These checks write signed synthetic variants and rejected receipts. Run them on an isolated probe inbox, away from demo and sealed case state.

For consent probes against the native bank, set `fixtures.consent_bank_service` to the service key containing that bank origin. The default inbox, package view and raw envelope routes are `/inbox`, `/packages/{package_id}` and `/packages/{package_id}/raw`. A successful dispatch control checks the approval against the signed body's digest and the dispatch receipt against the signed envelope's separate digest. Leave this fixture unset when using the legacy app-hosted bank view.

Before releasing the routed demo, start a fresh public Case 1 stack with
`ALIBI_HOLDER_TOKEN` set. Use a private profile with `base_urls.app` pointing to
the loopback router and `base_urls.bank` pointing to the same router's `/bank`
prefix. Supply three distinct synthetic tokens under `holder`, `bank_a` and
`bank_b`. Run the gate once per fresh store.

```bash
uv run --frozen python -m agents.eval.verify_demo_handoff --config "$ALIBI_ROUTED_PROBES"
```

The gate exercises valid approval and dispatch with no credential, each bank
credential, and then the holder credential. It checks both routed inboxes,
matches each accepted receipt to the signed body and exact RFC, and checks
that the other recipient gets 404. A successful run leaves two public receipts
available for profile generation against the direct loopback services.

After dispatching one public Case 1 package to each bank, generate the native
profile from the signed receipts and the holder's source view. Supply a private
base config with the disposable loopback origins and synthetic bearer tokens.
The generator writes an owner-only file and preserves explicit overrides.

```bash
uv run --frozen python -m agents.eval.probe_profile --config "$ALIBI_BASE_PROBES" \
  --key-dir "$ALIBI_DEMO_KEYS" --out "$ALIBI_LIVE_PROBES"
```

This prepares SP-17, SP-18 and SP-20 through SP-32. Run the three RFC intake
checks on a disposable bank clock inside both Case 1 RFC windows. SP-22
expects `subject_mismatch` and SP-23 expects `txn_out_of_feed`. SP-24 sends a
valid Bank B control and changes the declared issuer to Bank A on the attack. Supply the
remaining route and fixture settings in the base config for the full catalogue.

Keep the receipt stack as the shared origin for readback. Give each mutating
consent probe its own fresh store by setting `probes.<id>.base_urls.app`,
`probes.<id>.base_urls.bank` and `probes.<id>.tokens` to that probe's local stack.
Set the shared fixtures `consent_bank_service: "bank"`, `rfc_bank_a` and
`rfc_bank_b` to the Case 1 RFC IDs, and set both recipient IDs and the policy
version from holder review. For SP-04 also supply `rfc_followup` for the
published follow-up. SP-05 needs an approved request held beyond the approval
window while its RFC remains open, supplied as `expired_request`. SP-09 derives
the conflicting retry from the stored signed envelope and sends it to the
disposable bank's `/deliver` route unless overridden by an explicit fixture.

For a fresh S4 rehearsal stack, the optional `ingest_probe_profile` module
derives the SP-39 follow-up and checks the active-source readback control for
SP-33 through SP-37. It checks the app is serving the selected public case.

```bash
uv run --frozen python -m agents.eval.ingest_probe_profile --config "$ALIBI_BASE_PROBES" \
  --out "$ALIBI_INGEST_PROBES"
```

SP-39 dispatches a real package through the S4 app's in-process bank and
checks the retained transaction scope and bank-visible response. SP-33
through SP-37 also need the app owner's webhook and import submission routes
and the corresponding attack bodies. Readback alone does not give them an outcome.

For the sealed comparison, start the disposable stack from the clean frozen candidate and verify the running services from the runbook before capture. Use the whole catalogue and a new output path.

```bash
uv run --frozen python -m agents.eval.run_probes --config data/alibi/live-probes.json \
  --candidate-evidence --results "$ALIBI_FROZEN_PROBES"
```

Keep the adjacent `<results filename>.receipt.json` with that capture. It records the probe checkout commit and a digest of the outcomes. Candidate capture accepts only loopback service origins, including per-probe overrides. Candidate capture and the lockbox runner each require a definite outcome for a demo refusal probe and for the demo control. A failed check counts as an observation and stays visible in the scores. If coverage is inconclusive, the command still prints the probe table and skips writing candidate evidence. Review full coverage and any failed controls before opening the lockbox. The evaluator confirms the service revision separately from the receipt.

`score_capture` takes normalized records extracted from actual application output. Include `case_number`, `request_id`, `audience`, `answers` keyed by question and `disclosed_refs` for every reference in the delivered body. Each answer has `status`, `facts`, `gap` and `evidence_refs`. The scorer counts a case as complete only when every request passes, so a missing second-bank response stays in the denominator. The probe file is a JSON object keyed by probe ID, or the `observed` report printed by `uv run --frozen python -m pytest -q -s agents/eval/test_sharing_flow.py` for the local reference. Use separate probe captures for each system. The final output line contains numeric metrics, with unexercised probes listed separately above it.

For the supported source question, capture `payout_references` and `settled_at` as parallel arrays ordered by payout record ID. For a computed breakdown, capture `breakdown_minor` with `sales`, `refunds`, `fees` and `net`. A contradiction supported by a cited payout status can instead carry `cited_payout_status`. For the period answer, capture `sales_minor`, `refunds_minor`, `sale_count` and `refund_count`. Use an empty facts object when no facts are asserted. These fields describe the actual answer and its evidence. The adapter must never populate them from gold.

```bash
uv run --frozen python -m agents.eval.score_capture --split validation \
  --inputs data/dev-inputs.jsonl --gold data/dev-gold.jsonl \
  --capture data/dev-capture.jsonl --probes data/dev-probes.json \
  --telemetry data/dev-telemetry.json
```

Use `--split lockbox` only through the authorized evaluator runner after candidate freeze. The lockbox run requires measured `unsafe_proposals`, `lookups` and `observed_inputs` from that system. A development capture can omit the telemetry file while wiring the adapter. Supply normalized `links` in gold and capture to compute match precision and recall. Citation accuracy counts observed supported claims backed by a sufficient evidence set, and gap recall counts expected gaps resolved to the correct status and gap code. Zero denominators are reported with their counts. The capture reports lookup-dependent cases separately and can include measured model calls, tokens and cost. Review text and PDF evidence separately.

Use `agents.eval.api.compare_captures(cases, gold, full=..., rules=..., one_pass=...)` for paired results. Each capture uses the same inputs and independent gold. The report gives win, tie and loss counts per case and per scenario family. A family wins when full Alibi passes more variants than the baseline within that family. Report the number of independent families with the counts and state the uncertainty when there are too few families for an interval.

The local reference retrieval helper is `agents.eval.api.fixed_candidate_pool(view, lookup)`. For each named credit it looks up the payment reference, the holder's cited reference, canonical references in visible documents and ordinary labelled references parsed from compatible balance releases. It then looks up missing component IDs from uniquely matched payouts. The two baselines map both delivered and retrieved second-platform events with `agents.data.api.to_contract` after the time and subject filters. Period requests use the records already visible in their time window. `match_credit`, `reconcile_credit` and `period_totals` expose deterministic draft inputs. A well-formed self-supplied record retains its import provenance. The official comparison uses the app owner's S5 runner and its shared policy and delivery checks.

`agents.eval.api.predict_baseline(case_without_gold, mode, gate=...)` emits one prediction per RFC in the published [prediction schema](../../alibi/eval/predictions.md). Choose `rules` or `one_pass`. Both modes prepare the same time-scoped candidate pool for each RFC. `one_pass` makes one uncached Flash request per RFC, records failed attempts, and stops on provider failure. The pre-policy draft preserves citation revisions and rationale for S5's shared gate. The `gate(evidence, draft)` callback returns the final `claims` keyed by question and the recipient-bound `package`, with optional `exclusions` and `failure`. Without the gate, predictions retain the draft and carry an `unavailable` failure. Run the shared gate before scoring disclosures, counting unsafe proposals or reporting benchmark results.

```bash
uv run --frozen python -m agents.eval.baselines --mode rules --case 1 --case 2
```

The baseline CLI reads only public development cases. The evaluator-only runner supplies sealed inputs after freeze. Use `--mode one_pass --log data/alibi/dev-one-pass.jsonl` for a live public run with a separate provider log.

Check a delivered native package using the original export and a separately pinned public registry.

```bash
uv run --frozen python -m agents.eval.verify_package data/alibi/export.json \
  --registry data/alibi/keys/trust_registry.json --recipient bank_a --request-id rfc_a_001
```

Use `--at` with the fixture clock for a replay. The output separates package authentication from issuer signatures, redacted extracts and claim status. Use `--subject` when the bank must verify the subject from the export. An extract that omits that binding returns `subject_unverifiable`.

Prepare the evaluator-only release inputs before freeze. Keep the source files and destination outside all Git worktrees.

```bash
uv run --frozen python -m agents.eval.freeze_pack \
  --sealed "$SEALED_INPUTS" --adversarial "$ADVERSARIAL_INPUTS" \
  --gold "$AUTHORED_GOLD" --steps "$ADVERSARIAL_STEPS" --out "$PRIVATE_RELEASE_PACK"
```

The output contains `cases/` for the official seal and a separate `gold.jsonl` grouped by case. The latter includes the official string-ref answers, the independent per-request revision and privacy checks, and the adversarial step plan. A ref excluded by only one recipient stays in that request’s checks. The case-level field contains refs forbidden to every recipient. The seal commits the grouped file in full. Write the release manifest with the frozen candidate, using `alibi.eval.seal`.

Rehearse the one-command runner on public data before freezing. `--case` selects a subset and can be repeated. The rehearsal uses independent rules drafts and a stubbed one-pass transport. It produces the same files as a frozen run, with typed `unavailable` outcomes for the missing live gate and app.

```bash
uv run --frozen python -m agents.eval.run_sealed --dry-run --out "$REHEARSAL_OUTPUT"
```

When `alibi.adapter.run_case` lands, check it on public cases before freeze. Run from the clean candidate with the lockbox closed and `GEMINI_API_KEY` in the environment or the local `.env`. In a detached checkout, set `ALIBI_EVAL_ENV_FILE` to the evaluator’s existing credential file. The evaluator reads the run key and denies the worker access to the source file. Before creating its output or lock, a new live capture checks the credential and imports the adapter in the worker sandbox without supplying a case. Choose a new output directory. Start with Cases 1 and 2 to cover the settled and pending examples and both bank recipients. Inspect `report.md` and the three prediction files for request coverage, refusals, actual packages and measured usage.

```bash
uv run --frozen python -m agents.eval.run_sealed --public-live --case 1 --case 2 \
  --private-root "$ALIBI_FROZEN_PRIVATE_ROOT" --out "$PUBLIC_LIVE_OUTPUT"
```

For the recorded run, export `ALIBI_FROZEN_CASES`, `ALIBI_FROZEN_GOLD`, `ALIBI_FROZEN_MANIFEST`, `ALIBI_FROZEN_MODEL_CONFIG`, `ALIBI_FROZEN_PRIVATE_ROOT`, `ALIBI_FROZEN_PROBES`, and `ALIBI_FROZEN_OUT` in the evaluator’s environment. Use the clean frozen candidate checkout. The private root covers authoring and release data. Place the run directory outside that root and every Git worktree. The macOS `sandbox-exec` preflight denies the worker access to the private root, repository data and repository `.env`. The evaluator loads the Gemini run credential and keeps lockbox access in the parent. Each worker receives one allowlisted case over stdin and starts with separate scratch state for that case and system.

```bash
python3 tools/results.py add --split lockbox --variants 1 --timeout 4500 \
  --command 'uv run --frozen python -m agents.eval.run_sealed'
```

The runner verifies the seal before and after capture. It checks the probe receipt against the sealed commit and captured outcomes before starting the one allowed run, then retains the receipt with the predictions and probe JSONL. Both live modes deny workers access to that directory so later cases and systems cannot read earlier predictions. Repeating the recorded command checks their digests and scores those same files. An incomplete run stays recorded in the private directory for the evaluator to investigate. The deck table and final metrics distinguish the official scorer from the independent check. When an attachment omits its revision, the independent check uses the active revision at the RFC time, as specified by the official prediction format.

Render the aggregate deck handoff from the saved scores without launching workers:

```bash
uv run --frozen python -m agents.eval.headline "$ALIBI_FROZEN_OUT/scores.json"
```

This writes `HEADLINE.md` alongside the scores and prints one JSON object. If the frozen set has no `hidden_reference` family, pass `--public-scores` pointing to a public run's `scores.json` to add that separately labelled row. The exact sign test uses family-level net wins and losses. Zero-failure upper bounds use observed trials, with coverage shown separately.
