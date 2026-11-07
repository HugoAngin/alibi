# Alibi contracts, v1

Owned by `lead`. The full contract text is the "Technical contracts", "Agent boundaries" and "Consent and signing" sections of `alibi/SPEC.md`. This page pins the decisions agents build against in parallel. To change one, post a `need` to `lead`. Actual routes, commands and JSON schemas are published by their owners once wired, and `lead` links them here.

## Pinned decisions

| Topic | Decision |
| --- | --- |
| Money | Integer minor units plus ISO 4217 code. The demo currency is `GBP`, so amounts are pence. Code never converts currencies. |
| Time | UTC, ISO 8601 with `Z`. Periods are half-open `[from, to)`. |
| Canonical fixture | Sales `240000`, refunds `18000`, fees `6000`, bank credit `216000`. One fixture in the app, tests, slides and recorded demo. |
| Recipients | `bank_a` and `bank_b`. One mock bank service, two credentials, two inboxes. Isolation is enforced by authenticated recipient scope, never by a UI selector. |
| Trust | Source trust is one of `issuer_verified`, `bank_observed`, `self_supplied`. Imports are tagged `self_supplied` by the backend whatever the user says. |
| Link state | One of `unmatched`, `ambiguous`, `linked`. Exact matching needs one unique compatible candidate. |
| Claim status | One of `supported`, `contradicted`, `unresolved`. `contradicted` means evidence conflicts with the claim; `unresolved` means the evidence is unavailable. Link state, trust and claim status are three separate fields. |
| Settlement timing | A payout released to a platform balance is not a bank settlement. A later payout never explains an earlier credit because the amounts match. |
| Identity | Merchant events: `schema_version`, `event_id`, `record_id`, `revision`, `issuer_id`, `subject_ref`, `kind`, `occurred_at`. Bank rows: stable `txn_id`, `revision`, signed amount from the holder's view. RFCs: `request_id`, `subject_ref`, `audience`, period or `txn_ids`, question IDs, expiry; follow-ups get a new `request_id` and a parent reference. |
| Idempotency | By connector and `event_id`. Same ID with different bytes is rejected. Corrections append a newer revision and keep the old one. A stale revision cannot replace the active one. |
| Signing | Ed25519 over JCS canonical JSON (`rfc8785`), separate record and package signing domains, envelope with algorithm, key ID and signature. Separate mock-merchant and Alibi demo keys. The app key comes from `ALIBI_MASTER_KEY` in `.env`. |
| Approval | Holder decision bound to SHA-256 of the canonical body, audience, `request_id`, policy version and source revisions, with a short validity window. Any change invalidates it. Each recipient and request needs a fresh approval. |
| Agents | Matcher, Detective, Challenger, Curator. Deterministic joins run first. Each role has separate inputs and a schema-validated output. The backend owns arithmetic, policy, consent, signing and delivery. |
| LLM | Gemini through `google-genai`, key name `GEMINI_API_KEY`. Pin the model ID in code and in `docs/TASK.md` once chosen. |

## Results format

`tools/results.py add` records a command that prints one JSON object on its last line. For this task the headline metrics are case rates, not returns:

```json
{"cases": 32, "safe_complete": 27, "safe_complete_rate": 0.844,
 "match_precision": 0.97, "match_recall": 0.95, "unmatched": 3,
 "citation_accuracy": 0.96, "gap_recall": 0.9,
 "forbidden_items": 0, "unnecessary_items": 1,
 "probes_blocked": 11, "probes_attempted": 12,
 "model_calls": 140, "tokens": 182000, "cost_usd_estimate": 0.41}
```

Report every system (rules baseline, one-pass Gemini, full Alibi) with the same keys so the README table lines up. Failures and abstentions count in the denominators. Keep the exact-answer scorer separate from any human review of explanations.

## Split and lockbox

`shared/splits.json` sets `lockbox_from` to a case number. Development and demo cases are numbered below it. The sealed evaluation and adversarial cases are numbered from it upward, are authored by `eval` from separate scenario families, and are not committed until the candidate is frozen. `eval` scores them once with `results.py add --split lockbox`. The demo fixture and its variants are never in the sealed set.
