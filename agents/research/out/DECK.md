# Alibi deck skeleton

Owner: `research`. Draft v1, 2026-11-07. The deck itself is lead's (SPEC build plan). This file is the plan it is built from.

Every team is scored from the deck alone, so each rubric row needs evidence a judge can check on the slide, not only in the live demo. Sources: `docs/TASK.md`, `docs/SPEC.md` (Judging rubric, Evaluation, Four-minute demo), `shared/CONTRACTS.md`, `agents/data/README.md`, `agents/execution/out/`.

## Rules for every slide

- Numbers marked `{{PH-nn}}` stay placeholders until `eval` marks the record as passing with `tools/results.py verify`. The registry at the end says which record fills each one.
- Development numbers (cases 1–40) are labelled "development" wherever they appear. Only sealed results (cases ≥ 1000, `--split lockbox`) go on the results slides.
- Every screenshot is from the running simulated services. A recorded run is labelled "recorded run" on the slide (TASK rule).
- Amounts are always £2,400 sales − £180 refunds − £60 fees = £2,160 credit (`240000`, `18000`, `6000`, `216000` pence).
- Say "synthetic" and "simulated" on the user, data and demo slides.

## Rubric coverage

| Rubric row | Weight | Slides |
| --- | --- | --- |
| Traceable reasoning | 25% | 6, 7 (also 13) |
| Testing on unseen data | 25% | 4, 12, 13, 14 |
| Relevance and impact | 20% | 2, 3, 8, 11 |
| Risk controls and oversight | 15% | 9, 10 (also 8, 11) |
| Ambition and technical quality | 15% | 5, 15 (also 3, 6) |

| TASK.md required slide | Slide |
| --- | --- |
| User | 2 |
| Data | 4 |
| Benchmark | 12 |
| Results | 13, 14 |
| Limits | 16 |
| Controls | 9, 10 |

## Slides

### 1. Title

- **Working title:** Alibi: your money's alibi, ready before anyone asks.
- **Claim:** A user-controlled financial passport that explains a bank credit with checkable evidence and shares only what the holder approves.
- **Evidence:** Team, track (AgentVerse 2026 Finance Track), repo link, final commit `{{PH-01}}`.
- **Supplied by:** lead (`docs/SUBMISSION.md`).

### 2. The user and her decision (Relevance 20%; TASK: user)

- **Working title:** Maya has to explain £2,160, and rent is due.
- **Claim:** Maya, a student who resells clothes, has her account restricted until she explains one credit. Her decision is which records to share. The analyst's decision is separate.
- **Evidence:** The credit as the bank sees it: `txn-0001-004`, +£2,160, booked 28 Oct 2026 06:30Z, reference `PO-0001-01`. The bank's question (`rfc-0001-01`, issued 2 Nov): `q.source_of_funds`, `q.breakdown`. Label: synthetic person and bank.
- **Supplied by:** data, case 1 (`agents/data/out/cases/case_0001.json`).

### 3. The passport exists before anyone asks (Relevance 20%; Ambition 15%)

- **Working title:** The evidence was already there.
- **Claim:** Alibi builds Maya's history from the merchant and bank connections in the background and raises an alert before any bank request.
- **Evidence:** Timeline from case 1: 8 sales, 2 refunds and 8 fees pushed 5–14 Oct; balance release `REL-0001-01` on 26 Oct; bank credit 28 Oct; RFC on 2 Nov. Screenshot of the passport dashboard with the proactive "unexplained credit" alert, timestamped before 2 Nov `{{PH-02}}`. Code-computed reconciliation £2,400 − £180 − £60 = £2,160.
- **Supplied by:** data (timeline), alibi/ build sessions via lead (dashboard and alert screenshot).

### 4. Data (TASK: data; Testing 25%)

- **Working title:** Synthetic ledgers, authored first, with an answer key.
- **Claim:** Every case is a fictional event ledger from which bank rows, merchant records and paraphrased requests are emitted, with independent gold. Development and sealed cases come from separate scenario families.
- **Evidence:** 70 development cases in 11 families: demo 2, clean 6, bundled payment 5, balance vs settlement 7, pending correction 6, duplicate record 5, missing proof 7, self-supplied import 2, hidden reference 10, injection 10, alert 10. Gold claim status: 47 supported, 10 contradicted, 13 unresolved. The 10 injection cases repeat cases 1 and 2, so they are not independent. Every case has sensitive distractors (rent, groceries, pharmacy, a health purchase) that must never be disclosed. Sealed set: `{{PH-03}}` cases in `{{PH-04}}` families, plus `{{PH-05}}` adversarial cases, committed only after freeze. Data's 321 checks rebuild the cases and re-derive the gold. The gold follows the same pinned rules as the rules baseline (data card, honesty note), so the sealed set is the honest comparison.
- **Supplied by:** data (`agents/data/out/index.json`, `agents/data/README.md`), eval (sealed counts and commitments).

### 5. How it works (Ambition 15%; Risk 15%)

- **Working title:** Code decides, agents interpret.
- **Claim:** Deterministic joins run first. Four Gemini roles (Matcher, Detective, Challenger, Curator) get separate inputs and return schema-validated proposals. The backend alone does arithmetic, policy, consent, signing and delivery.
- **Evidence:** SPEC architecture diagram, redrawn. One line per role from SPEC "Agent boundaries". Gemini models: `gemini-3.8-flash` for routine calls, `gemini-4-argon` for the frontier role (pinned in `docs/TASK.md`). Which role uses which: `{{PH-06}}`.
- **Supplied by:** lead (diagram), research (role lines and model ID once pinned in `docs/TASK.md`).

### 6. Traceable reasoning: one claim, end to end (Traceable 25%)

- **Working title:** Click a claim, reach the source.
- **Claim:** Each claim in the package traces to source IDs and revisions, a scoped lookup, and arithmetic recomputed in code.
- **Evidence:** Case 1 trace for `q.source_of_funds`:
  1. Question `q.source_of_funds` on `txn-0001-004`.
  2. Detective reads `rec-0001-020`: a release to the platform balance whose message names withdrawal `PO-0001-01`. This is not proof of a bank transfer.
  3. Challenger requests the payout by reference through the merchant lookup and gets `rec-0001-021`: payout `PO-0001-01`, settled 27 Oct 16:00Z, £2,160, destination bank.
  4. Code checks the reference matches the bank row, settled ≤ booked ≤ settled + 5 days, and recomputes £2,160 from the 18 covered records.
  5. Claim: supported, evidence `[bank:txn-0001-004, merchant:rec-0001-021]`.
  Screenshot of the claim-to-source trace in the app `{{PH-07}}`. Bank-facing, so no model inputs or private records.
- **Supplied by:** data (records and gold), research (agent trace from the dev run), alibi/ build sessions via lead (screenshot).

### 7. Traceable reasoning: a new record changes the conclusion (Traceable 25%)

- **Working title:** Same amounts, different answer.
- **Claim:** When the retrieved record says the money had not reached the bank, the explanation is downgraded, even though the amounts still add up.
- **Evidence:** Case 1 and case 2 side by side. Same £2,160 and same balance-release message. In case 2 the looked-up payout `PO-0002-01` is still pending as of 29 Oct, after the credit was booked on 28 Oct, so Maya's settlement claim is contradicted and the credit's origin is unresolved (gap `settlement_pending_at_booking`). Code-based regression, shown separately and not as an LLM result: case 25, a signed correction to `failed` invalidates the claim and the pending draft `{{PH-08}}`.
- **Supplied by:** data (cases 1, 2, 25), research (dev trace for case 2), execution (invalidation probe SP-06).

### 8. Maya's disclosure decision (Relevance 20%; Risk 15%)

- **Working title:** Maya sees exactly what the bank will see.
- **Claim:** Code builds the smallest package that answers the question. Maya sees what is included, excluded and generalised, removes an optional item, and approves the exact body for one named bank.
- **Evidence:** Preview screenshot `{{PH-09}}`. Included: the credit, the payout, and the 18 covered sale, refund and fee records. Excluded privately: rent `txn-0001-001`, two card debits, and the prescription-eyewear purchase `rec-0001-019`. One plain-English rule compiled to a typed policy and confirmed by Maya.
- **Supplied by:** data (gold evidence sets and forbidden refs), execution (policy compilation), alibi/ build sessions via lead (screenshot).

### 9. Controls: a live refusal (Risk 15%; TASK: controls)

- **Working title:** The backend says no.
- **Claim:** Changing an approved package, reaching another bank's inbox, or planting instructions in a receipt is refused in code, and each refusal is logged.
- **Evidence:** Three demo probes, each with its refusal reason and audit line: SP-02 (edit the approved body, refused with `approval_digest_mismatch`, then a fresh approval dispatches), SP-17/SP-18 (Bank A lists or fetches Bank B's package and is refused), SP-38 (injected instruction in receipt text causes no out-of-scope read). Probe totals on the sealed run: `{{PH-10}}` blocked of `{{PH-11}}` attempted, `{{PH-12}}` controls allowed.
- **Supplied by:** execution (`agents/execution/out/PROBES.md`, `agents.execution.api.summarise`), eval (verified probe counts).

### 10. Oversight: two humans, separate decisions (Risk 15%; TASK: controls)

- **Working title:** Maya approves, the analyst decides, nothing is automatic.
- **Claim:** Approval is bound to the SHA-256 of the body, the audience, the request ID, the policy version and the source revisions. The analyst sees package authenticity, per-source trust and claim status as three separate checks and records a human disposition. No account is cleared automatically.
- **Evidence:** Analyst console screenshot with the three checks `{{PH-13}}`. A disposition record (`clear`, `escalate` or `request_more`). Audit entries for approve, dispatch, receive and disposition. A self-supplied CSV export (case 39, `import:rec-0039-011`) is reviewable at `self_supplied` trust, not `issuer_verified`.
- **Supplied by:** execution (`agents/execution/out/ANALYST_FLOW.md`), alibi/ build sessions via lead (screenshot).

### 11. Reuse for a second bank (Relevance 20%)

- **Working title:** One passport, a different question, a new approval.
- **Claim:** Bank B's different question is answered from the same persisted history with no new import. It gets its own scope, package and approval, and neither bank can see the other's.
- **Evidence:** Bank B request `rfc-0001-02` (4 Nov): sales and refunds over [1 Oct, 1 Nov). Answer: 8 sales £2,400, 2 refunds £180, from 10 reused source IDs. Portability checks `{{PH-14}}`: source-store identity unchanged, source IDs reused, distinct package and request IDs, reusing Bank A's approval refused (SP-03), isolated inboxes. Two sharing-history entries.
- **Supplied by:** data (case 1 Bank B gold), eval (portability report), execution (SP-03, SP-17), alibi/ build sessions via lead (screenshot).

### 12. Benchmark (TASK: benchmark; Testing 25%)

- **Working title:** Three systems, same inputs, sealed cases.
- **Claim:** Full Alibi is compared with a rules baseline and a one-pass Gemini baseline on cases nobody building the app had seen. The baselines are not weakened.
- **Evidence:** One line per system: (1) rules with reference, amount and date matching, reconciliation and fixed evidence selection, (2) one Gemini call drafting the whole package, (3) full Alibi with adaptive retrieval and challenge. All three get the same input, candidate pool, validation and policy gate. Each baseline's predeclared retrieval strategy. Freeze record: candidate commit `{{PH-15}}`, model and configuration `{{PH-06}}`, commitments to inputs and gold `{{PH-16}}`, reruns disclosed `{{PH-17}}`. Primary endpoint: safe, correct case resolution.
- **Supplied by:** eval (baselines, scorer, freeze record).

### 13. Results (TASK: results; Testing 25%)

- **Working title:** Held-out results.
- **Claim:** `{{PH-18}}`, written after the numbers verify. Do not draft the claim before then.
- **Evidence:** One table, same keys for all three systems, failures and abstentions in the denominators:

  | Metric | Rules | One-pass Gemini | Alibi |
  | --- | --- | --- | --- |
  | Safe-complete / cases | `{{PH-20}}` | `{{PH-21}}` | `{{PH-22}}` |
  | Match precision / recall (unmatched) | `{{PH-23}}` | `{{PH-24}}` | `{{PH-25}}` |
  | Citation accuracy | `{{PH-26}}` | `{{PH-27}}` | `{{PH-28}}` |
  | Gap recall | `{{PH-29}}` | `{{PH-30}}` | `{{PH-31}}` |
  | Forbidden / unnecessary items | `{{PH-32}}` | `{{PH-33}}` | `{{PH-34}}` |
  | Pre-policy unsafe proposals | `{{PH-35}}` | `{{PH-36}}` | `{{PH-37}}` |

- **Supplied by:** eval (`agents/eval/out/results.jsonl`, `--split lockbox`, verified).

### 14. Uncertainty, cost and a failure (Testing 25%)

- **Working title:** How sure we are, what it cost, where it broke.
- **Claim:** The result holds within the stated uncertainty. Here is one case Alibi got wrong and why.
- **Evidence:** 95% intervals by case-family resampling, or paired win/tie/loss counts if there are too few families `{{PH-38}}`. Per-case latency, model calls, tool calls, tokens and cost, marked observed or estimate `{{PH-39}}`. One representative failure with its trace `{{PH-40}}`. Sample size stated as cases and as independent families.
- **Supplied by:** eval (intervals, cost, failure), research (failure trace and cause).

### 15. What adaptive retrieval adds (Ambition 15%)

- **Working title:** Choosing the next lookup is where the model earns its place.
- **Claim:** `{{PH-41}}`, written from the measured gap between one-pass Gemini and full Alibi on cases whose answer needs a lookup. If the gap is zero or negative, say so.
- **Evidence:** Live Gemini tool selection: the Challenger picks the payout lookup by reference from free text (screenshot or log line `{{PH-42}}`). Safe-complete on lookup-dependent sealed cases, one-pass vs Alibi `{{PH-43}}`. Lookup counts per system `{{PH-44}}`. What the LLM is not credited with: detecting `status: pending`, parsing exact references, arithmetic (SPEC "What must earn the LLM's place").
- **Supplied by:** eval (split results), research (role traces), alibi/ build sessions (live run).

### 16. Limits (TASK: limits)

- **Working title:** What this does not show.
- **Claim:** Results measure a simulated task on synthetic records. Real bank acceptance and fewer account restrictions are unmeasured.
- **Evidence:** Both connections are simulated services, not live integrations. The app holds the encryption key, and Gemini can read the synthetic data sent to it. Monitoring covers observable activity and verification gaps, not a bank's AML system. Not a W3C Verifiable Credential. Untested or replayed features listed by name `{{PH-45}}`.
- **Supplied by:** lead and research from SPEC "Prototype boundaries", eval (sample-size caveats).

### 17. Close

- **Working title:** Evidence that is ready, disclosure that is chosen.
- **Claim:** Maya answers each bank with the minimum supported evidence, approves every disclosure herself, and reuses her history without starting again.
- **Evidence:** One-line recap of slides 6, 9, 11 and 13. Repo and demo links.
- **Supplied by:** lead.

### Appendix (optional)

- A1. Full security probe table, 47 probes, 32 core (execution).
- A2. Reproduce: the commands that rebuild the cases, run the three systems and rescore (eval, from `tools/results.py` records).
- A3. Gold rules and the nine gap codes (data, `agents/data/README.md`).

## Four-minute demo mapping

| Demo time (SPEC) | Slides |
| --- | --- |
| 0:00–0:25 | 2, 3 |
| 0:25–0:45 | 3 |
| 0:45–1:50 | 6, 7 |
| 1:50–2:25 | 8, 9 |
| 2:25–2:45 | 10 |
| 2:45–3:10 | 11 |
| 3:10–4:00 | 13, 14, 16 |

## Claims the deck must not make

- That the app or Gemini cannot read data sent for processing.
- That a bank accepted a package, or that restrictions were reduced.
- That Gemini detected an explicit `pending` status, parsed an exact reference or did the arithmetic.
- Any number not marked passing by `results.py verify`, or a development number shown as held-out.
- That a signature proves a claim is true. It proves integrity and the signing key.
- Any working feature that ran only as a stub or recorded run, unless labelled.

## Placeholder registry

Each placeholder is filled from a verified record or an artefact. `Key` is the metric name in `shared/CONTRACTS.md` "Results format".

| ID | Slide | What | Key or artefact | System | Split | Owner |
| --- | --- | --- | --- | --- | --- | --- |
| PH-01 | 1 | Final commit and links | git, `docs/SUBMISSION.md` | | | lead |
| PH-02 | 3 | Dashboard with pre-RFC alert | screenshot | Alibi | demo | lead |
| PH-03 | 4 | Sealed case count | eval freeze record | | lockbox | eval |
| PH-04 | 4 | Sealed family count | eval freeze record | | lockbox | eval |
| PH-05 | 4 | Adversarial case count | eval freeze record | | lockbox | eval |
| PH-06 | 5, 12 | Role-to-model mapping and config (IDs pinned in `docs/TASK.md`) | `alibi/llm/config.py` | all | | research |
| PH-07 | 6 | Claim-to-source trace | screenshot | Alibi | demo | lead |
| PH-08 | 7 | Invalidation on correction | SP-06 outcome | Alibi | dev | execution |
| PH-09 | 8 | Private review preview | screenshot | Alibi | demo | lead |
| PH-10 | 9 | Probes blocked | `probes_blocked` | Alibi | lockbox | eval |
| PH-11 | 9 | Probes attempted | `probes_attempted` | Alibi | lockbox | eval |
| PH-12 | 9 | Controls allowed | `agents.execution.api.summarise` | Alibi | lockbox | eval |
| PH-13 | 10 | Analyst console | screenshot | Alibi | demo | lead |
| PH-14 | 11 | Portability checks | eval portability report | Alibi | demo | eval |
| PH-15 | 12 | Frozen candidate commit | freeze record | | | eval |
| PH-16 | 12 | Commitments to inputs and gold | freeze record | | | eval |
| PH-17 | 12 | Reruns disclosed | freeze record | | | eval |
| PH-18 | 13 | Results headline | written after PH-20–37 | | lockbox | research |
| PH-20–22 | 13 | Safe-complete / cases | `safe_complete`, `cases`, `safe_complete_rate` | rules, one-pass, Alibi | lockbox | eval |
| PH-23–25 | 13 | Match precision, recall, unmatched | `match_precision`, `match_recall`, `unmatched` | rules, one-pass, Alibi | lockbox | eval |
| PH-26–28 | 13 | Citation accuracy | `citation_accuracy` | rules, one-pass, Alibi | lockbox | eval |
| PH-29–31 | 13 | Gap recall | `gap_recall` | rules, one-pass, Alibi | lockbox | eval |
| PH-32–34 | 13 | Forbidden and unnecessary items | `forbidden_items`, `unnecessary_items` | rules, one-pass, Alibi | lockbox | eval |
| PH-35–37 | 13 | Pre-policy unsafe proposals | `unsafe_proposals` | rules, one-pass, Alibi | lockbox | eval |
| PH-38 | 14 | Intervals or win/tie/loss | eval report | all | lockbox | eval |
| PH-39 | 14 | Latency, calls, tokens, cost | `model_calls`, `tokens`, `cost_usd_estimate` | all | lockbox | eval |
| PH-40 | 14 | One representative failure | case number and trace | Alibi | lockbox | eval, research |
| PH-41 | 15 | Adaptive retrieval headline | written after PH-43 | | lockbox | research |
| PH-42 | 15 | Live tool selection | screenshot or log | Alibi | demo | research |
| PH-43 | 15 | Safe-complete on lookup-dependent cases | `safe_complete_rate` on cases with non-empty `requires_retrieval` | one-pass, Alibi | lockbox | eval |
| PH-44 | 15 | Lookup counts and records read | `lookups`, `observed_inputs` | all | lockbox | eval |
| PH-45 | 16 | Untested or replayed features | integration status | | | lead |

## Gaps raised and closed

Lead closed all three in `c44079b` (2026-11-07 22:44Z):

- Results keys `unsafe_proposals`, `lookups` and `observed_inputs` added to `shared/CONTRACTS.md` (PH-35–37, PH-44).
- Sealed gold keeps `requires_retrieval`, so eval can split lookup-dependent cases (PH-43).
- Gemini model IDs pinned in `docs/TASK.md` (PH-06). The role-to-model mapping still comes from `alibi/llm/config.py`.
