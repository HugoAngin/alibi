<!--
README draft for judges. Owner: research. Lead replaces the root README with this at T-1h
(docs/SUBMISSION.md: move the old README to docs/TEAM.md first).

Sources copied verbatim: the data card from agents/data/out/data_card.md at d711686, and the
controls table and limits from agents/execution/out/CONTROLS_SLIDE.md at 06d22b1. Research
refreshes them before T-1h. {{PH-nn}} placeholders match the registry in
agents/research/out/DECK.md and are filled only from records eval marks as passing.
Delete this comment before publishing.
-->

# Alibi

Alibi is a financial passport the account holder controls: it links bank payments to the marketplace sales, refunds, fees and payouts that explain them, so when a bank asks about a payment, the holder can answer with checkable evidence and share only what the question needs.

> Your money’s alibi, ready before anyone asks.

Built in one night for AgentVerse 2026, Finance Track. Every person, account, merchant and bank in this repo is synthetic, and both connections are simulated services.

## The problem

The Finance Track asks teams to help a specific person make a better financial decision, with evidence a professional can check and human oversight for consequential actions.

**Maya** is a student who resells clothes. Her bank has restricted her account and asks her to explain one credit of **£2,160**, and rent is due soon. Her sales were £2,400, she refunded £180 and paid £60 in fees, so the numbers add up. But the marketplace’s message only says the money was *released to her seller balance*, which is not proof it reached her bank. Matching amounts are not evidence. Maya has to decide which records to share; the bank analyst makes a separate decision about what they show.

## What Alibi does

1. **Keeps the evidence ready.** Maya connects her marketplace and her bank once. Signed merchant records and bank rows arrive in the background, are matched by deterministic rules first, and are kept with their versions. Alibi raises an alert about the unexplained credit before any bank asks.
2. **Investigates the question it was asked.** When Bank A sends a Request for Context, four Gemini roles work on the persisted passport with separate inputs and schema-checked outputs: the Matcher links ambiguous records, the Detective proposes claims and gaps, the Challenger looks for counter-evidence and chooses a scoped lookup (here, the payout record behind the balance release), and the Curator proposes the smallest disclosure.
3. **Lets code decide.** The backend recomputes every amount in pence, checks settlement timing and references, applies Maya’s privacy rules, and labels each claim supported, contradicted or unresolved. A later payout never explains an earlier credit.
4. **Puts Maya in charge of disclosure.** She sees what is included, left out and generalised, removes optional items, and approves the exact package for one named bank. The approval is bound to the package’s SHA-256 digest; any change, a different recipient or an expired window is refused at dispatch.
5. **Lets the bank verify, and a person decide.** The bank checks the Ed25519 package signature, each source’s trust and each claim’s status as three separate things. An analyst records clear, escalate or request more. Nothing happens to an account automatically.
6. **Reuses the passport.** Bank B asks a different question about the same history. Alibi answers it from the same stored records with no new import, a new package and a fresh approval, and neither bank can see the other’s.

## Results

**The sealed result is negative.** On 44 sealed synthetic cases, authored by our evaluator independently of app development and scored once on frozen commit `58bf0d4`, none of the three systems (our agents, a one-pass Gemini baseline and a rules baseline) produced a safe-complete answer. There is no demonstrated advantage over either baseline.

| System | Cases | Returned without a worker failure | Failed | Main safe-complete (both scorers) | Adversarial safe-complete |
| --- | ---: | ---: | ---: | ---: | ---: |
| Agents (Alibi) | 44 | 10 | 34 | 0 of 32 | 0 of 12 |
| One-pass Gemini | 44 | 10 | 34 | 0 of 32 | 0 of 12 |
| Rules | 44 | 10 | 34 | 0 of 32 | 0 of 12 |

- **Why it failed, as far as we know.** Retained error output shows 18 distinct cases failing at ingest, before any reasoning. Observed per system, that is 17 of the agents' 34 failures (13 core payload schema, 4 self-supplied import kind) and 18 each of the one-pass and rules baselines' 34 (13 and 5). In those cases the sealed pack used event kinds and fields our ingest schema rejected, an input-contract gap we had flagged at 01:45 and not closed. The other failures have no saved error output, so their cause is unknown. This run mostly measures that contract mismatch at ingest, not reasoning quality.
- **No mapping and no rescore.** Adding a mapping after seeing the result would have been a second look at the sealed set, so the frozen result stands. After the freeze, `a3be52a` made an unreadable holder import a refused, skipped record instead of a fatal error for the case. It is a robustness fix only: the sealed result was not rescored with it, and the result above is from the frozen candidate.
- **One later diagnostic on the same inputs, disclosed.** After the result was in, eval ran the rules baseline with `a3be52a` on the same 44 sealed inputs, to check the fix at runtime only. All 44 cases completed without a worker failure (record `09a90f6cd0`), with no model calls and no scoring against the answer keys. Because those inputs had already been opened, this is a development diagnostic, an additional look at the pack, not fresh held-out evidence, and it says nothing about correctness.
- **The capture was interrupted and resumed.** A harness fault interrupted the capture, and it was resumed under continuation rules that lead authorised after the interruption and before the resume. Every original returned result was kept byte-for-byte.
- **Variants:** 3 frozen variants were tried before this freeze.
- **Cost:** full cost is unknown. Partial logged cost is about $0.70 over 62 model calls for the agents and about $0.06 over 14 calls for the one-pass baseline; the rules baseline makes no model calls.
- **Calibration:** the sealed pack received rules-based calibration while eval authored it privately, so it was not untouched by every evaluated system. We disclose this.
- **Security probes:** of 17 exercised refusal probes, 15 held and SP-23 and SP-24 failed (see Limits); both acceptance controls passed, and 28 probes were not exercised. The protections held where we tested them: separate approval per bank, signed packages the bank verifies independently, and cross-bank reads refused.

Records: `1b235c16e9` (interrupted capture), `e16afeeac5` (continuation) and `f8dc5763b6` (aggregate export), rerun by `eval` with `python3 tools/results.py verify`. Source: `agents/eval/out/UNSEEN_RESULTS.md`; per-system error counts in `agents/eval/out/UNSEEN_ERROR_CLASSES.json`.

**Development results.** Everything else we show, the 80 public cases, the case 1 demo and the probes, is development work on public data and labelled as development. None of it is a held-out result.

## Method

The full frozen evaluation method is in [`agents/eval/out/UNSEEN_METHOD.md`](agents/eval/out/UNSEEN_METHOD.md). In short:

- **Primary endpoint:** safe, correct case resolution. A case passes when every answerable fact has correct source support, unresolved or contradicted questions are labelled correctly, and the package holds no forbidden or unnecessary item. The official scorer and a stricter second scorer are reported separately.
- **Baselines:** a rules system (reference, amount and date matching, reconciliation, fixed evidence selection) and one Gemini call that drafts the whole package. Their configured retrieval differs, and that is disclosed.
- **Seal:** one evaluator capture opened the sealed pack, on a candidate frozen before it was opened. Main and adversarial cases are kept separate, and every missing, timed-out, invalid or refused outcome counts against the system.

## Data

**What it is.** 80 synthetic development cases for Alibi, numbered 1–80. Each case is one fictional seller's history:
- marketplace events: sales, refunds, fees, balance releases, payouts, notices and statements
- bank feed rows
- one or two Requests for Context from mock banks
- the holder's explanation
- an answer key (`gold`)

The files are `agents/data/out/cases/case_NNNN.json`, indexed in `agents/data/out/index.json` and loaded through `agents/data/api.py`. Case 1 is the canonical demo fixture used in the app, the tests, the slides and the recorded demo: Maya, £2,400 sales − £180 refunds − £60 fees = £2,160 credit (`240000 − 18000 − 6000 = 216000` pence).

**Provenance.**
- The 12 receipt images in `out/receipts/` are drawn by `agents/data/receipts.py` with Pillow. Each is stamped SYNTHETIC on the image, and its true fields are in `receipts.json`. `agents/data/vision_check.py` scores a Gemini vision read against those fields: on three hard layouts (app screenshot, handwritten on lined paper, letter photographed at an angle) it got 20 of 21 fields, with one issuer name wrong (eval record `73e742c7d7`).
- Every record is synthetic and labelled `"SYNTHETIC - labelled test data, not a real person, account or institution"`.
- No real financial records, people, accounts, merchants or banks were used or consulted.
- The cases are written by one deterministic generator, `agents/data/ledgers.py` (Python standard library only), with seed `20261107`.
- Rebuild and check from a clean checkout:

  ```bash
  uv run --frozen python -m agents.data.ledgers           # rewrites out/ byte for byte
  uv run --frozen python -m pytest agents/data            # 394 checks; one asserts out/ equals a fresh build
  ```
- Names such as "Mock Marketplace Payments", "Synthetic Lettings Ltd" and `mock-marketplace` are invented.

**Marketplace view.** `agents/data/out/merchant_view/case_0001.json` is case 1 as the mock marketplace's own site shows it (8 orders with refs and titles, 2 refunds, fees, payout `PO-0001-01`, the release notification). A test checks that it agrees with case 1.

**Throughput set.** 200 more generated cases (numbers 101–300, seed 20261208) in `agents/data/out/throughput/` measure adapter speed and cost only. They are excluded from every count and score here.

**Separation from the evaluation set.**
- The development cases are numbered below `lockbox_from = 1000` (`shared/splits.json`). Every loader filters through `tools.lockbox.visible`.
- The sealed evaluation and adversarial cases (1000 and above) are authored separately by the evaluation owner, from different scenario families and wording. They are not generated by this script, and builders never see them before the candidate freeze.
- The demo pair (cases 1–2) and its variants are development data, never part of the sealed set.

**Format.** All money is integer minor units with an ISO 4217 code (GBP only, never converted). All times are UTC ISO 8601 with `Z`, and periods are half-open `[from, to)`. Each delivery records when it was received, so any case can be viewed as of a given time with `known_at(case, t)`. Gold uses only what was observable at the first request's issue time. Some records are not pushed but served only by follow-up retrieval (`api.lookup`), so a system must ask for them.

**Pinned rules the gold follows** (same as `shared/CONTRACTS.md`):
- Idempotency is by connector and event ID. A duplicate is ignored, the same ID with different bytes is rejected, and an older revision never replaces a newer one.
- A bank credit is **linked** only when exactly one compatible candidate exists. A candidate is one settled bank payout, or one whole settlement batch, with:
  - the same amount
  - settlement at or before the credit's booking time, within 5 days
  - when the credit carries a reference, a payout reference, batch ID or withdrawal ID that matches it

  No candidate makes the credit **unmatched**; several make it **ambiguous**. A release to the platform balance is never a settlement. A later payout never explains an earlier credit.
- The holder's explanation is **supported** when the credit is linked and the stated sales, refunds and fees equal the amounts computed from the cited records. It is **contradicted** when the evidence conflicts: a different breakdown, or a cited payout that was pending, failed or settled later. It is **unresolved** when the evidence is unavailable.
- Link state, source trust (`issuer_verified`, `bank_observed`, `self_supplied`) and claim status are separate fields.

**Families and what each tests.**

| Cases | Family | Tests |
| --- | --- | --- |
| 1–2 | demo | The balance release is pushed and the payout is fetched by lookup: settled (supported) or still pending at booking (contradicted, origin unresolved). Bank B's period question tests reuse. |
| 3–8 | clean | One settled payout per credit; one holder omits refunds and fees (contradicted). |
| 9–13 | bundled_payment | Two payouts settled as one credit; a later payout with the same amount; two equal candidates (ambiguous, abstain). |
| 14–20 | balance_vs_settlement | Release vs settlement: pushed, lookup-only, never settled, settled after the credit, pending at booking. |
| 21–26 | pending_correction | Signed newer revisions: a refund finalised, a sale price adjusted, a stale revision redelivered, the holder quoting a superseded amount, a payout corrected to failed. |
| 27–31 | duplicate_record | Exact redelivery, the same ID with different bytes, bank row redelivery, every sale delivered twice. |
| 32–38 | missing_proof | No merchant records, no payout, amount mismatch, a cited fee record missing, a payout arriving after the request. |
| 39–40 | self_supplied_import | The only payout proof is the holder's own export, kept at lower trust whatever it claims. |
| 41–50 | hidden_reference | The deciding payout's reference is not a regex away. It may appear only in an order confirmation, be absent (orders and weekday, or date only), use a withdrawal-ID alias mapped by a statement or an import, sit in an unfamiliar layout (email, semicolon CSV), or be one of two references told apart by amount or timing. |
| 51–60 | injection | Exact copies of cases 1 and 2, each with one adversarial string from the controls owner's catalogue planted in a message, receipt, request question, lookup result or bank description. Gold is unchanged; the expected refusal is recorded. |
| 61–70 | alert | Timelines that raise proactive alerts before any request: unexplained credit, unusual inflow or outflow, amount or settlement discrepancy, a payout settled but not received, an unverified merchant, and a quiet control with none. Gold lists the alerts. A mock merchant registry ships alongside. |
| 71–80 | second_platform | A second simulated platform with its own schema, wording and settlement vocabulary: weekly statements instead of payouts, `paid`/`scheduled` instead of settled/pending, and its own reference format. It covers paid, scheduled-at-booking, net mismatch, omitted charges, a corrected order and a redelivered statement. In two cases Maya uses both platforms in one month, and a second bank's period question must combine them. |

Every case also carries distractors that must never be disclosed: rent, grocery and pharmacy debits, and in some cases the holder's own health-category purchase.

Outcomes across the 80 cases: 54 supported, 12 contradicted, 14 unresolved; 58 linked, 21 unmatched, 1 ambiguous. The 10 injection cases repeat the outcomes of cases 1 and 2 by design, so they are not independent.

**Honesty note.** The answer key is not independent of the rules. Gold is produced by construction from the same pinned rules (matching amount, settlement timing and reference, active revisions, recomputed arithmetic) that the rules-based baseline implements, and `test_ledgers.py` re-derives it with those rules. A rules baseline with the right retrieval can therefore reach 100% on these cases by design. It did on cases 1–40, because each lookup-only reference appeared verbatim in a pushed message. We added cases 41–50 so that finding the reference takes interpretation, and a naive regex-retrieval probe scores 6 of 10 there. These cases measure whether a system observes, retrieves and applies the rules correctly. They do not show that the rules are the right policy, and they are development data that we studied while building. The honest comparison is the sealed set, authored separately. Many cases are variants of one scenario template, so case counts here overstate independent evidence.

**Changes after first release.**
- `c9472fe`: 40 cases aligned to the spec.
- `fb7cae7`: cases 41–50 added; cases 1–40 byte-identical.
- `c339e2a`: a pending payout in cases 2 and 20 is retrievable only from its `status_as_of` time. One field changed per case; gold unchanged.
- `a8105fa`: cases 51–60 (injection) added.
- `11abc05`: cases 61–70 (alert) and the mock merchant registry added. Cases 1–60 are unchanged.
- `da8289f`, `3d2ff7d`: 12 synthetic receipt images (`out/receipts/`, true fields in `receipts.json`) and cases 71–80 (second platform) added. Cases 1–70 are unchanged.
- `eb773d8`: the 200-case throughput set added (not scored).

## Controls

**Title:** Code decides what leaves. Humans decide what it means.

| Threat | Control (enforced in code, not prompts) | Tested by | Evidence |
| --- | --- | --- | --- |
| **Fabricated source, arithmetic or conclusion** | Every cited record ID and revision is checked against what was retrieved. Money is recomputed in pence from cited amounts by code, and again by the bank's verifier. A later payout cannot explain an earlier credit. Contradicting evidence is kept. | SP-41, SP-42, SP-43, INJ-02, INJ-07, INJ-21 | ⏳ trace view on the request page (S4); ⏳ verifier recompute (S2 `alibi/bank/`); ✅ timing and matching rules in `shared/CONTRACTS.md` |
| **Malicious text in a receipt, request or tool result** | All text is data. Agents can only propose schema-validated output. Tools are allowlisted and scoped to Maya's subject. Out-of-scope requests are refused and logged. | SP-38, SP-39, SP-40, SP-45, all 25 in `injections.jsonl` | ✅ [`injections.jsonl`](agents/execution/out/injections.jsonl); ⏳ gateway refusal in the audit log (`tool.refused`); ⏳ eval injection run |
| **Over-disclosure through fields, summaries or traces** | Plain-English rules compile to typed deny, generalise, age and recipient rules, and Maya confirms them. Ambiguous rules are held and deny wins. The package is built from allowlisted fields and the policy applies to every rendering: body, summaries, extracts, metadata and PDF. Gaps never name what was withheld. The exclusion list stays on Maya's side. | SP-10 to SP-16, PR-01 to PR-40 | ✅ [`policy_rules.jsonl`](agents/execution/out/policy_rules.jsonl); ⏳ private review screen (S4); ⏳ eval `forbidden_items` / `unnecessary_items` |
| **Forged sender, tampering, replay or stale approval** | Approval is bound to the SHA-256 body digest, recipient, request, policy version and cited source revisions, and expires after 15 minutes. Dispatch rechecks all of them and refuses on any change. Ed25519 signatures with separate merchant and Alibi keys. Event and package IDs are deduplicated. | SP-01 to SP-09, SP-25 to SP-28, SP-31, SP-33 to SP-35, C1 to C16 | ✅ [`consent_matrix.md`](agents/execution/out/consent_matrix.md); ⏳ live refusal on stage (demo 1:50); ⏳ `dispatch.refused` audit entry |
| **Unauthorised vault access or data leakage** | Holder and analyst roles are separate. Recipient scope comes only from the credential, on every inbox, package, attachment and receipt endpoint. Bank A cannot list or fetch Bank B's package, and a wrong ID gets a 404 without revealing whether it exists. Evidence in the passport store is encrypted at rest with an app key stored apart from the data; the mock bank's delivered demo state is not (SP-47). Gemini receives only the fields its task needs. | SP-17 to SP-24, SP-46, SP-47 | ⏳ isolation test (S2 / eval); ⏳ demo 2:45 (Bank B inbox) |
| **Tool failure, missing evidence or misuse of alerts** | Retries are bounded. Unavailable evidence becomes a typed `unresolved` reason, never a made-up success. Alerts describe observed facts and verification gaps, never suspicion or profiling. Agents cannot sign, approve, send or act on an account. | SP-44, SP-45 | ⏳ unresolved gaps in eval results; ✅ alert and disposition wording in [`ANALYST_FLOW.md`](agents/execution/out/ANALYST_FLOW.md) |

**Human oversight, two decisions:**
- **Maya** approves the exact package for one named bank. Any change after that needs a new approval.
- **The analyst** records clear, escalate or request more, with a reason. Nothing changes on any account automatically.

**Probe totals (development, public probes):** of 17 exercised refusal probes, 15 held (SP-23 and SP-24 failed); both acceptance controls passed; 28 of 47 probes were not exercised.

## Limits

- Both connections are simulated services with synthetic data. No real bank, merchant, account or money is involved.
- The app holds the encryption key, and Gemini processes the synthetic fields sent to it. We do not claim that the app or the inference provider cannot read data sent for processing.
- Probes run against our own mock services. They show our controls work as built, not that a real bank integration would be secure.
- The policy compiler supports five rule kinds. Absolute dates, aggregation ("refunds as a total"), rules about people and standing auto-share permissions become clarifications rather than rules (PR-20, PR-26, PR-28, PR-31).
- Monitoring covers observable discrepancies and verification gaps. It is not AML screening or scam detection.
- Signing proves integrity and which key signed. It does not prove a claim is true; the analyst decides that.
- Real bank acceptance, and any reduction in account restrictions, are unmeasured.
- Production would need separate privacy, security and regulatory review, real key management and a holder-controlled vault.
- The sealed evaluation mostly measured an input-contract mismatch at ingest: 18 distinct sealed cases failed before any reasoning (observed 17, 18 and 18 per system), and the remaining failures' cause is unknown. It cannot tell us how well the agents reason.
- Refusal probes SP-23 and SP-24 failed in the frozen run, and both still fail the bank-intake refusal contract. SP-23: the bank accepted and sent the request (201) although the holder side refused it, and the holder readback was 404. SP-24: the bank accepted a request (201) whose declared issuer was rewritten to Bank B; the holder accepted it (200) and it never appeared in Bank A's inbox. The fix is to check the request before signing; it is not in the frozen candidate. Evidence: `agents/eval/out/BANK_REQUEST_HANDOFF.md`. 28 of 47 probes were not exercised.
- Encryption at rest covers the passport store only. The mock bank's delivered demo state was observed in plaintext (SP-47).
- On the frozen local app, a `?token=` login URL is accepted and can be logged. The hosted router refuses credential-bearing URLs; the local app does not.

## Run it

The whole two-bank demo, with no API key, from the repo root:

```bash
agents/execution/run_demo.sh --replay
```

It starts a clean case 1 stack (backend, mock merchant, mock bank, both recipients and Maya's fixture), opens the holder and Bank A windows, and walks the demo one Enter at a time: the £2,160 payout lands and Alibi raises its alert, Bank A sends its Request for Context, then Bank B sends its own. `--replay` uses the recorded agents and labels them so; everything else runs live. Without `--replay` it calls Gemini and needs `GEMINI_API_KEY` in `.env`. Bank tokens are read from `data/alibi/bank_tokens.json` and never printed.

The web front end (`web/`) needs Node 22.12 or newer and pnpm 10:

```bash
cd web
pnpm install
pnpm dev        # Alibi http://localhost:4321, merchant :4322, bank :4323
pnpm verify     # format, lint, type check, tests, build
```

Development data and checks:

```bash
uv sync --frozen
uv run --frozen python -m agents.data.ledgers
uv run --frozen python -m pytest agents/data
```

## How it is built

| Part | What | Where |
| --- | --- | --- |
| Front end | Astro on Cloudflare: landing page, Maya’s app (phone view), the bank’s analyst console and the merchant, with the shared “Connect to my Alibi” and “Request customer’s Alibi” buttons | `web/` |
| Backend | Connectors, persisted passport, the four Gemini roles, arithmetic, policy, consent, signing, verification and delivery | `alibi/` |
| Data | 80 synthetic development cases with answer keys | `agents/data/` |
| Controls | Security probe catalogue, consent matrix, policy rules, analyst flow | `agents/execution/` |
| Evaluation | Sealed cases, baselines, scorer, package verifier | `agents/eval/` |

Gemini models: `gemini-3.8-flash` for routine calls and `gemini-4-argon` for the frontier role ({{PH-06}}).

Each of the five of us ran one Claude Code agent that owned one folder. The agents coordinated on a pinned GitHub issue, a commit hook and a CI check stopped any agent writing outside its own folder, and every reported number was rerun by the evaluation agent from a clean checkout.

## Team

| Name | GitHub | Role |
| --- | --- | --- |
| Arnav Mahajan | Knull | lead: product integration, backend, submission |
| Hugo | HugoAngin | data: synthetic cases and connections |
| Elio Haddad | eliohd | research: deck, web front end |
| Mouaad | sirExile44 | execution: controls, policy, bank side |
| Tamim | t7acct | evaluation: sealed set, baselines, scoring |

Repo and demo: {{PH-01}}
