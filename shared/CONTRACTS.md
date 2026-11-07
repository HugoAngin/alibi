# Alibi contracts, v1

Owned by `lead`. The full contract text is the "Technical contracts", "Agent boundaries" and "Consent and signing" sections of `alibi/SPEC.md`. This page pins the decisions agents build against in parallel. To change one, post a `need` to `lead`. Actual routes, commands and JSON schemas are published by their owners once wired, and `lead` links them here.

## Pinned decisions

| Topic | Decision |
| --- | --- |
| Money | Integer minor units plus ISO 4217 code. The demo currency is `GBP`, so amounts are pence. Code never converts currencies. |
| Time | UTC, ISO 8601 with `Z`. Periods are half-open `[from, to)`. |
| Canonical fixture | Sales `240000`, refunds `18000`, fees `6000`, bank credit `216000`. One fixture in the app, tests, slides and recorded demo. The fixture is data's case 1, `agents.data.api.canonical_case()`, with case 2 as its contradicted twin: subject `maya-001`, issuer `mock-marketplace` ("Mock Marketplace Payments" on the bank row), credit `txn-0001-004` booked 2026-10-28 with reference `PO-0001-01`, balance release `rec-0001-020` (`REL-0001-01`) pushed on 2026-10-26, payout `rec-0001-021` settled 2026-10-27 and reachable only by lookup, RFCs `rfc-0001-01` (bank_a, that txn) and `rfc-0001-02` (bank_b, October 2026 period). The mock merchant and bank services, UI samples and demo script load these IDs from the case rather than inventing their own. |
| Recipients | `bank_a` and `bank_b`. One mock bank service, two credentials, two inboxes. Isolation is enforced by authenticated recipient scope, never by a UI selector. |
| Trust | Source trust is one of `issuer_verified`, `bank_observed`, `self_supplied`. Imports are tagged `self_supplied` by the backend whatever the user says. Trust describes the underlying record's issuer, so it stays at three values. Each `source_trust` and evidence item also carries `form`, one of `full_record` or `redacted_extract`. A redacted extract keeps the trust of the records it summarises and is attested by the package signature, which the UI shows as "Redacted extract, attested by Alibi" beside the issuer badge. Never badge an extract as if the issuer signed it. |
| Link state | One of `unmatched`, `ambiguous`, `linked`. Exact matching needs one unique compatible candidate. |
| Claim status | One of `supported`, `contradicted`, `unresolved`. `contradicted` means evidence conflicts with the claim; `unresolved` means the evidence is unavailable. Link state, trust and claim status are three separate fields. |
| Settlement timing | A payout released to a platform balance is not a bank settlement. A later payout never explains an earlier credit because the amounts match. |
| Identity | Merchant events: `schema_version`, `event_id`, `record_id`, `revision`, `issuer_id`, `subject_ref`, `kind`, `occurred_at`. Bank rows: stable `txn_id`, `revision`, signed amount from the holder's view. RFCs: `request_id`, `subject_ref`, `audience`, period or `txn_ids`, question IDs, expiry; follow-ups get a new `request_id` and a parent reference. |
| Idempotency | By connector and `event_id`. Same ID with different bytes is rejected. Corrections append a newer revision and keep the old one. A stale revision cannot replace the active one. |
| Signing | Ed25519 over JCS canonical JSON (`rfc8785`), separate record and package signing domains, envelope with algorithm, key ID and signature. Separate mock-merchant and Alibi demo keys. The app key comes from `ALIBI_MASTER_KEY` in `.env`. |
| Approval | Holder decision bound to SHA-256 of the canonical body, audience, `request_id`, policy version and source revisions, with a short validity window. Any change invalidates it. Each recipient and request needs a fresh approval. |
| Agents | Matcher, Detective, Challenger, Curator. Deterministic joins run first. Each role has separate inputs and a schema-validated output. The backend owns arithmetic, policy, consent, signing and delivery. |
| LLM | Gemini through `google-genai`, key name `GEMINI_API_KEY`. Model IDs as set in `alibi/llm/config.py` on the s3a build: `gemini-3.8-flash` (thinking low) for routine calls and `gemini-4-argon` (thinking medium) for the frontier role, both confirmed live on 2026-11-07. The one-pass Gemini baseline uses the same model as the Alibi role it stands in for and reports any difference. |
| Disposition | Analyst decision on a delivered package, recorded bank side: `disposition_id`, `package_id`, `package_digest` (SHA-256 of the package), `request_id`, `recipient`, `analyst`, `decision` one of `clear`, `escalate`, `request_more`, `reason`, `question_ids`, `requested_evidence`, `decided_at`, `supersedes`. Visible to the deciding recipient only, never sent to the holder automatically and never an input to an automated account decision. `clear` and `escalate` are terminal for that package. `request_more` creates a follow-up RFC with a new `request_id` and `parent_request_id`, which needs fresh holder approval. Full checklist: `agents/execution/out/ANALYST_FLOW.md`. |
| Case schema | A case is the JSON object described in `agents/data/README.md` (Case layout): `merchant_deliveries`, `merchant_lookup`, `imports`, `bank_deliveries`, `rfcs`, `holder_claim`, and for labelled cases `gold`. Development cases are one file per case in `agents/data/out/cases/`, gold embedded, loaded with `agents.data.api`. Sealed and adversarial cases use the same schema, one JSONL record per case without `gold`, with gold in a separate evaluator-only file keyed by case number and `request_id`. Sealed gold keeps `requires_retrieval`, so the report can split cases that depend on a lookup from those that do not. Neither sealed file is committed until the candidate is frozen. Systems under test receive only what `known_at(case, decision_time)` returns and reach lookup-only records through the lookup interface. |
| Questions and matching | Question IDs are `q.source_of_funds`, `q.breakdown` and `q.sales_refunds_in_period`. A settlement candidate is one settled payout or one whole `settlement_batch_id` group whose total equals the credit, with `settled_at` at most 5 days before `booked_at` and never after it, and whose reference equals the bank `payment_reference` when one is present. Gap codes are the nine listed under Gold rules in `agents/data/README.md`. These are the gold rules for the rules baseline and the scorer; the Alibi agents may use anything observable but are scored against them. |
| App adapter | The runnable entry point that takes a case without gold and returns one package per RFC is owned by the `alibi/` build sessions and is not published yet. `eval` scores against the case schema and gold above in the meantime; the adapter's import path is linked here when it lands. |

## Results format

`tools/results.py add` records a command that prints one JSON object on its last line. For this task the headline metrics are case rates, not returns:

```json
{"cases": 32, "safe_complete": 27, "safe_complete_rate": 0.844,
 "match_precision": 0.97, "match_recall": 0.95, "unmatched": 3,
 "citation_accuracy": 0.96, "gap_recall": 0.9,
 "forbidden_items": 0, "unnecessary_items": 1,
 "probes_blocked": 11, "probes_attempted": 12,
 "unsafe_proposals": 4, "lookups": 36, "observed_inputs": 410,
 "model_calls": 140, "tokens": 182000, "cost_usd_estimate": 0.41}
```

Report every system (rules baseline, one-pass Gemini, full Alibi) with the same keys so the README table lines up. `unsafe_proposals` counts packages the policy gate rejected before delivery, so pre-policy behaviour is compared and not hidden by the common gate. `lookups` and `observed_inputs` count lookup calls and records read across all cases, which the SPEC asks the baselines to report against their predeclared fixed retrieval strategy. Failures and abstentions count in the denominators. Keep the exact-answer scorer separate from any human review of explanations.

## Split and lockbox

`shared/splits.json` sets `lockbox_from` to a case number. Development and demo cases are numbered below it. The sealed evaluation and adversarial cases are numbered from it upward, are authored by `eval` from separate scenario families, and are not committed until the candidate is frozen. `eval` scores them once with `results.py add --split lockbox`. The demo fixture and its variants are never in the sealed set.
