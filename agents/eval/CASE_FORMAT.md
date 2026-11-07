# Alibi evaluation case format

The agreed case format is one JSON object per line. Cases reuse the public ledger shape documented in [agents/data/README.md](../data/README.md). Keep case inputs and gold in separate files, joined by `case_number`. The model and runtime receive only the current request and permitted evidence. The evaluator retains case metadata, answer keys and the full future event stream.

| Input | Format |
| --- | --- |
| Identity | `case_number` as an integer, `family`, `variant`, `title`, `label` and `subject_ref`. Numbers below 1000 are development. Numbers at or above 1000 are sealed. Family, variant and title are evaluator metadata. |
| Timed ledgers | `merchant_deliveries[]` of `{received_at, event}` and `bank_deliveries[]` of `{received_at, row}`. Process in arrival order. Preserve duplicates and corrections. At each decision, expose only records available by then. |
| Requests | `rfcs[]` with `request_id`, `parent_request_id`, `audience`, `subject_ref`, `txn_ids` or `period`, `question_ids`, `questions` with their wording, `issued_at` and `expires_at`. Use `bank_a` and `bank_b`, UTC ISO 8601 and half-open periods. Reuse the passport across requests and require separate approval. |
| Additional evidence | `merchant_lookup[]` of `{available_from, event}` is visible only through a permitted lookup. `imports[]` of `{received_at, record}` is labelled `self_supplied`. Keep original wording in the source body. Optional `privacy_policy` supplies the holder's restrictions before preview. |
| Holder claim | `holder_claim` with `txn_id`, `explanation` and `stated_minor` breakdown. Use integer minor units and an explicit currency on money records. |

Gold joins by case number and request ID. Each request records the decision time, link state and source versions, expected claim status and verifiable calculation, required facts, alternative sufficient evidence sets, genuine gaps and forbidden disclosure paths or values. Evidence references are pairs of source ID and revision. Set `requires_retrieval` for answers that depend on lookup-only evidence. Keep gold in the evaluator's private file.

Gold can name `forbidden_fields` as exact field names and `forbidden_values` as distinctive literal strings. The capture must include `package_body`, the recipient-visible package including attachments and metadata, whenever either restriction is present. Use JSON null for a refusal with no delivered package. The scorer checks nested fields and strings, including summaries. Review the PDF separately.

For each request, return a machine-readable result with the request ID, observed links and revisions, per-question claim statuses, citations, calculations and gaps, the actual approved package body or a refusal, and the application outcome. Record latency, model and tool calls, usage, lookup counts, observed inputs, unsafe proposals before policy checks and any error as evaluator metadata. Preserve denied, unresolved, timeout and invalid outcomes. Use the same evaluator input contract across the rules baseline, one-pass Gemini and full Alibi. Exact production route names and the adapter entry point are supplied by the app owner.

Security checks use Mouaad's published probe IDs and `agents.execution.api.summarise` for observed `accepted` or `refused` outcomes. Run allowed controls as well as refusal probes. Keep unavailable application routes distinct from a verified refusal.

For a multi-step test, call `agents.eval.api.score_probe_steps(plan, observations)`. Join observations to the plan by `case_number`, `request_id` and `step_id`. Capture what the app actually did as `accepted`, `refused` or `error`. Keep missing and errored steps in the report. A repeated probe passes only when all its planned steps have the expected outcome. Feed the returned `observed` map to the capture scorer and retain the step counts separately.

`score_capture` takes normalized records extracted from actual application output. Include `case_number`, `request_id`, `audience`, `answers` keyed by question and `disclosed_refs` for every reference in the delivered body. Each answer has `status`, `facts`, `gap` and `evidence_refs`. The scorer counts a case as complete only when every request passes, so a missing second-bank response stays in the denominator. The probe file is a JSON object keyed by probe ID, or the `observed` report printed by `uv run --frozen python -m pytest -q -s agents/eval/test_sharing_flow.py` for the local reference. Use separate probe captures for each system. The final output line contains numeric metrics, with unexercised probes listed separately above it.

```bash
uv run --frozen python -m agents.eval.score_capture --split validation \
  --inputs data/dev-inputs.jsonl --gold data/dev-gold.jsonl \
  --capture data/dev-capture.jsonl --probes data/dev-probes.json \
  --telemetry data/dev-telemetry.json
```

Use `--split lockbox` only through the authorized evaluator runner after candidate freeze. The lockbox run requires measured `unsafe_proposals`, `lookups` and `observed_inputs` from that system. A development capture can omit the telemetry file while wiring the adapter. Supply normalized `links` in gold and capture to compute match precision and recall. Citation accuracy counts observed supported claims backed by a sufficient evidence set, and gap recall counts expected gaps resolved to the correct status and gap code. Zero denominators are reported with their counts. The capture reports lookup-dependent cases separately and can include measured model calls, tokens and cost. Review text and PDF evidence separately.

Use `agents.eval.api.compare_captures(cases, gold, full=..., rules=..., one_pass=...)` for paired results. Each capture uses the same inputs and independent gold. The report gives win, tie and loss counts per case and per scenario family. A family wins when full Alibi passes more variants than the baseline within that family. Report the number of independent families with the counts and state the uncertainty when there are too few families for an interval.

The shared baseline retrieval implementation is `agents.eval.api.fixed_candidate_pool(view, lookup)`. For each named credit it looks up the payment reference, the holder's cited reference and ordinary labelled references parsed from compatible visible balance releases. It then looks up missing component IDs from uniquely matched payouts. Period requests use the records already visible in their time window. Both baselines use this same fixed strategy and its measured lookup and input counts. `match_credit`, `reconcile_credit` and `period_totals` expose the rules baseline's deterministic draft inputs. A well-formed self-supplied record retains its import provenance. All drafts still need the app's common validation, policy and delivery checks.
