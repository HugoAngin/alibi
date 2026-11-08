# Build sessions

Prompts for Arnav's Claude Code build sessions. Each session runs in its own worktree (`git worktree add ../av-<name> -b <name> main`), with `.env`, `CLAUDE.local.md` and `.claude/settings.local.json` copied in. Paste the preamble and then the session block as one message. S2, S3 and S4 run in parallel from a `main` that contains S1; S5 runs after the three have merged. Merges happen in `~/av-merge` (branch `integ`) and are pushed with `git push origin integ:main`.

## Preamble

```
ultracode

You are building part of Alibi for the AgentVerse finance hackathon, inside a git worktree of the team repo. You are lead. Read alibi/SPEC.md first: it is the product spec and the acceptance criteria. Read alibi/CONTRACTS.md for the core API. This is a build session, not the RULES.md work loop.

Hard rules for this session. They override the work loop in RULES.md:
1. Write only inside alibi/ and agents/lead/. Never touch agents/data, agents/execution, agents/eval, agents/research or web/, and never edit pyproject.toml, uv.lock, tools/, shared/, docs/ or team.json. The packages you need are already installed.
2. Run code as `uv run --frozen python -m alibi.<module>` from the repo root and tests as `uv run --frozen python -m pytest alibi -q`. Never pip install, uv add, or make a virtualenv. If an import is missing, write the exact error to alibi/BLOCKERS.md and carry on with the parts that don't need it.
3. Do not run tools/board.py, tools/setup.sh or git push. Commit locally and often: `git add alibi agents/lead && git commit -m "lead: <what changed>"`. Plain messages, no trailers, no co-author lines.
4. No retry loops. If the same step fails twice for the same reason, record it in alibi/BLOCKERS.md (command, error, what you tried) and move to the next item. Never sleep-wait or poll. Start any server you test with a timeout, e.g. `timeout 20 uv run --frozen python -m ...`, and make sure it is dead before you continue.
5. Tests never call Gemini. Live calls happen only in scripts that check GEMINI_API_KEY is set and enforce a hard call cap. Never print or log GEMINI_API_KEY or ALIBI_MASTER_KEY.
6. Money is integer pence with an ISO currency code. Times are UTC ISO-8601. Periods are half-open [from, to). The canonical fixture is sales 240000, refunds 18000, fees 6000, bank credit 216000. Generated keys, databases, caches and logs go under data/alibi/, which is git-ignored.
7. Prefer plain functions and small modules. Every module you add gets tests in alibi/tests/. Run the full test command before every commit. Commit finished items straight away; do not hold commits for a final review.
8. When your scope below is done, write alibi/REPORTS/<session>.md: what works, each with the command that proves it; what failed; what is untested. Then stop. Do not start work outside this session's scope.
9. The team's case data is the ground truth for shapes and IDs: read agents/data/README.md and shared/CONTRACTS.md (Canonical fixture, Case schema, RFC scope, Questions and matching, Results format). Load cases only through agents.data.api (load_case, canonical_case, known_at, lookup, without_gold). The canonical demo is case 1: maya-001, merchant mock-marketplace, credit txn-0001-004 booked 2026-10-28 with payment_reference PO-0001-01, balance release rec-0001-020 pushed 2026-10-26, payout rec-0001-021 settled 2026-10-27 and only reachable by lookup, rfc-0001-01 from bank_a (txn-scoped, period null), rfc-0001-02 from bank_b for October. Case 2 is the contradiction variant. Never edit anything under agents/.
10. S1 landed with these facts: import from alibi.api and read alibi/CONTRACTS.md for exact signatures (the rules baseline is run_investigation(store, request_id, mode="rules", now=...)); always pass now= from the case's RFC issued_at or the fixture's fx["now"], because fixture requests expire on 2026-11-11; verify_package only checks subject and request when the caller passes them, so the bank service must pass the subject_ref and request_id of the RFC it issued; alibi/REPORTS/S1.md lists the open issues, read it and do not reintroduce them.
```

## S2 Connectors and bank side

```
SESSION S2: connectors and bank side. You own alibi/connectors/, alibi/bank/, alibi/demo_services.py and their tests. Do not edit alibi/core, alibi/fixtures, alibi/gateway.py or alibi/api.py; write needs in alibi/NEEDS.md and work around them locally. Report in alibi/REPORTS/S2.md.

1. connectors/case_source.py: the bridge from the team's case schema to the core types. map_merchant_event(case_event) -> core EvidenceRecord, map_bank_row(row) -> core BankTxn, map_rfc(rfc) -> core RequestForContext, map_import(record) -> EvidenceRecord tagged self_supplied. Keep every case field the core type cannot hold in an `extra` dict (payout_reference, withdrawal_id, settlement_batch_id, covers_record_ids, status, settled_at, status_as_of, release_reference, destination, related_record_id, counterparty_name, correction_reason). Document the mapping in connectors/MAPPING.md. A simulated clock: `as_of` defaults to the case's first RFC issued_at; deliveries with received_at > as_of and lookups with available_from > as_of are not visible. eval's builders in agents.eval.api (build_mock_merchant, build_mock_bank) exist for the canonical case; use them where they fit and the case schema elsewhere.

2. connectors/merchant.py: a FastAPI app for the mock resale platform (issuer_id mock-marketplace). POST /admin/seed?case=N&as_of= loads a case and pushes its merchant_deliveries in arrival order as signed webhooks to the passport ingest URL or in-process callable, duplicates, conflicts and stale revisions included, signing each with merchant_demo in the record domain; GET /records/{record_id}; GET /settlements?payout_reference= and GET /payouts?covers=rec-...&from=&to= (the four pinned lookup keys: payout_reference, record_id, covers_record_id, payout_date) serve merchant_lookup records visible at as_of; POST /admin/correct issues a signed correction (revision+1, correction_reason); POST /admin/retry resends an event with identical bytes; POST /admin/advance?as_of= moves the clock.

3. connectors/bank_feed.py: the mock read-only feed serving the case's bank_deliveries visible at as_of with cursor paging; the client persists the cursor only after a successful store write; provenance carries connection id, retrieval time and source version. connectors/self_supplied.py: the import path for the case's imports, tagged self_supplied by the backend whatever the record claims.

4. bank/service.py: one FastAPI app, two recipient identities bank_a and bank_b with separate bearer tokens (env or data/alibi/bank_tokens.json) and inboxes. POST /rfc issues the case's RFC for the authenticated identity, signed with the bank key, audience bound to that identity, exactly one of period or txn_ids; POST /deliver accepts a signed Context Package only when audience equals the authenticated recipient and passes the RFC's subject_ref and request_id into verify_package, returns a receipt bound to package id and digest, identical bytes return the same receipt, same id different bytes rejected; GET /inbox, GET /packages/{id}, GET /packages/{id}/attachments scoped to the recipient; POST /packages/{id}/disposition with the pinned Disposition record (decision clear | escalate | request_more, analyst, reason, question_ids, requested_evidence; clear and escalate terminal; request_more creates a follow-up RFC with new request_id and parent_request_id that inherits the parent's scope kind and values); verification reports package authentication, per-source trust and claim status separately, and a rejected package exposes no contents, only the rejection.

5. connectors/plug.py: wires merchant lookups and bank refresh into the Gateway's injectable callables so run_investigation performs real scoped retrievals through the simulated services; each lookup is a TraceEvent with the query and what came back.

6. demo_services.py: starts merchant (8101) and bank (8102) with uvicorn, --case N (default 1), --as-of, --timeout, exits cleanly.

Tests (TestClient): seeding case 1 leaves the expected active records; case 28's same-id-different-bytes is rejected; case 23's stale revision is ignored; a correction lands as a new revision; lookup of PO-0001-01 returns rec-0001-021 at the RFC time and nothing before its available_from; case 2's pending payout is visible only from its status_as_of; bank_a cannot list, fetch or download bank_b's package by any id; wrong-audience delivery rejected; retry returns the same receipt; expired RFC rejected; request_more produces a child RFC with the parent's txn_ids; a rejected package returns no attachment contents; cursor advances only after a successful write.

Done when tests pass, `timeout 20 uv run --frozen python -m alibi.demo_services --timeout 10` runs and exits, and REPORTS/S2.md is written.
```

## S3 Gemini agents

```
SESSION S3: Gemini agents. You own alibi/agents/ (prompts/ exist from S3a; refine, don't rewrite), alibi/baselines/ and their tests. alibi/llm/ exists from S3a: use its client, FakeClient, cache and budget. Do not edit alibi/core, alibi/fixtures, alibi/gateway.py or alibi/api.py; write needs in alibi/NEEDS.md. For development data use alibi/fixtures (S1's Maya JSON) because the case-to-store bridge is being built in parallel; the integration session switches to the team's cases. Report in alibi/REPORTS/S3.md.

1. Four roles, each with an input builder that passes only the fields that role needs, the prompt from agents/prompts/, and a pydantic output model matching the sibling JSON schema:
- Matcher (FLASH): only for txns the deterministic join leaves ambiguous. Applies the pinned candidate rule (settled payout or whole settlement_batch_id group, total equal to the credit, settled_at at most 5 days before booked_at and never after, reference equal to payment_reference or withdrawal_id when present; a balance_release is never a candidate). Abstains when several remain.
- Detective (FLASH): observable alerts and per-question candidate claims for q.source_of_funds, q.breakdown, q.sales_refunds_in_period, with source refs, gaps from the pinned gap codes, and proposed checks. Must not claim bank settlement from a balance-release message.
- Challenger (FRONTIER): revised status with confirming or conflicting refs, missing evidence, and next_action {type: lookup_settlement | lookup_payouts_covering | lookup_by_date | refresh_txn | none, args}. The runner executes next_action through the Gateway (budget 3 per run), feeds the result back, loops at most 3 times. It must distinguish release-to-balance from settlement, check payout reference and timing, and may downgrade a claim before curation.
- Curator (FLASH): per-question disclosures with one reason each, private exclusion reasons, unresolved gaps; the backend recomputes the filter.
Every reference is validated against retrieved material (record id and revision); unknown refs are dropped and logged as validation failures. Evidence items carry attestation (issuer_signature | package_signer | none) and redacted per CONTRACTS.md.

2. agents/runner.py: run_investigation(store, request_id, mode="agents", now=...) in the same shape as the rules version so alibi.api dispatches on mode. TraceEvents: question -> claim -> source id and revision -> lookup (sources[].via_lookup indexes the lookup that retrieved a source) -> calculation (terms [{label, sign, amount, source_ids}]) -> final state, plus the reason for every status change. Count lookups and observed inputs for the results keys. Private records stay out of bank-facing output.

3. agents/policy_parse.py: LLM version of parse_rule_text (FLASH) returning the typed PolicyRule schema with a confidence flag; ambiguous rules come back unresolved. Run execution's 40 fixtures (agents.execution.api.load_policy_rules()) through it once with the key (cap 45 calls) and report compiled, held and wrong-parse-leak counts.

4. agents/replay.py: record every live run to fixtures/replays/<case>_<variant>.json; with ALIBI_REPLAY=1 serve recordings with a visible replay label.

5. baselines/one_pass.py: one FLASH call proposing the whole draft from the same candidate pool, with the fixed retrieval strategy "look up any payout reference named in a balance_release message", then the same validation and policy checks. Emit the pre-policy proposal too, so unsafe_proposals can be counted.

6. Gemini breadth, cheap wins for the Best Use of Gemini prize: if client.models.list() shows a Gemma model on this key, use it as the independent second parser whose output must agree with Flash before a Matcher or Detective result is trusted (record the model id); add agents/receipt_vision.py, a self-supplied import that sends a receipt photo (PNG or JPEG) to FLASH and returns a structured EvidenceRecord draft tagged self_supplied with the extracted fields and a confidence, which the holder must confirm. Both are optional: if either fails twice, note it and move on.

7. Dev traces with the key set (cap 40 calls): agents mode on the fixture's contradiction variant and supported control; write human-readable traces to REPORTS/traces/; state whether the Challenger distinguished balance release from settlement, which lookup it chose, and the final statuses. Injection probe: run three strings from agents.execution.api.load_injections() planted in a message record; none may produce a reference outside the retrieved set or change a status.

Tests never call Gemini: input builders, output validation, reference validation, the runner loop with FakeClient scripts (including a lookup that returns nothing and budget exhaustion), the 5-day candidate rule, replay mode, unsafe_proposals counting.

Done when tests pass, dev traces are in REPORTS/S3.md, and recordings exist for both variants.
```

## S4 App wiring

```
SESSION S4: wire the app. You own alibi/app/*.py and alibi/app/UI.md; the templates and stylesheet exist from S4a. You may edit templates only to add the hooks in item 5 and to bind real variables; visual redesign belongs to teammates after you merge. Call everything through alibi.api; if run_investigation(mode="agents") is not importable yet, use mode="rules" with a visible "rules-only" label. Never fake an agent result. Report in alibi/REPORTS/S4.md.

1. Sample and fixture data: delete S4a's hand-written sample contexts and build them from agents.data.api.canonical_case() (case 1) and case 2, via agents.eval.api.build_analyst_preview where it fits. The "what changed" view shows a correction as a newer revision of the same record; balance release and payout are different records, never two revisions of one.

2. Holder routes: /passport (alerts and open requests first, history, connections last); /txn/{id} with linked sources, trust badges, the code-computed reconciliation and what-changed; /alerts with POST /tick re-running checks over the store and a mock merchant lookup form labelling unknown merchants unverified; /rules using alibi.core.policy.parse_rule_text, or alibi.agents.policy_parse when importable, with confirm/reject and held unresolved rules; /requests/{request_id} with run-investigation, the trace view (sources[].via_lookup, calculation.terms) and the private review (included, excluded with reasons, generalised, gaps, remove item, approve naming the recipient, digest and validity window); /requests/{request_id}/export (signed JSON, print view); /requests/{request_id}/dispatch posting to the bank service URL from config and showing a refusal verbatim when the body changed since approval. Every holder and analyst route also returns its template context as JSON when the request has Accept: application/json, so the Astro front end in web/ can render it later.

3. Analyst routes: /bank/{recipient}/inbox and /bank/{recipient}/packages/{id}; the recipient comes from the bearer token or session, a URL mismatch is 403; three separate panels (package authentication, per-source trust, claim status) with the auth panel's pass state rendered as a neutral outline, never as "true"; evidence by question with timing shown; package.period may be null for transaction-scoped RFCs, show txn_ids then; a rejected package renders only the rejection, no contents; disposition form fields per the pinned record (decision, analyst, reason, question_ids, requested_evidence) and request_more wired to the follow-up RFC; the follow-up form offers period or txn_ids like intake and defaults to the parent's scope. Evidence items show attestation and redacted: a redacted extract reads "Redacted extract, attested by Alibi" beside the issuer badge.

4. Replay banner when ALIBI_REPLAY=1; /audit; /health.

5. Template hooks the stylesheet owner has already styled, add them exactly: trace step li classes trace__step--question|claim|lookups|sources|calculation|status; review groups wrapped in section.review-group.review-group--included|excluded|generalised|gaps; analyst panels panel--auth|trust|claims with a one-line role question under each title; td data-label="<header>" on passport, transaction and package tables with table.stack. Add a when() macro in partials for holder-screen dates. Update UI.md for every variable you add (via_lookup, terms, attestation, redacted, timing, txn_ids).

Tests with TestClient against case 1 and case 2: every route renders in HTML and JSON; approving then editing the body makes dispatch refuse; recipient mismatch is 403; a rejected package shows no attachment contents; a txn-scoped package shows txn_ids; export JSON verifies with alibi.core.package.verify_package; the print view contains no excluded item; the render test from S4a still passes.

Done when tests pass, `timeout 20 uv run --frozen python -m alibi.app --timeout 10` serves /health, and REPORTS/S4.md lists each route with a one-line status and the hooks added.
```

## S5 Integration

```
SESSION S5: integration. You work on main in the repo root and may edit anything under alibi/ and agents/lead/. Nothing is stubbed: a path that does not work is reported as not working. Report in alibi/REPORTS/S5.md.

1. alibi/adapter.py: the app adapter eval asked for. run_case(case_without_gold, mode, as_of=None) -> predictions in the shape of alibi/eval/predictions.md: seeds the store from the case through the real connector code paths (case_source, signed merchant webhooks, bank feed, imports), issues each RFC, runs the investigation in the given mode (rules | one_pass | agents), builds the policy-filtered package per RFC, and emits links, claims, package, private exclusions, the pre-policy proposal, lookups, observed_inputs, cost fields and a typed failure. A CLI runs it over a case range and writes one predictions JSONL per mode. Link its import path in alibi/CONTRACTS.md so lead can pin it.

2. alibi/demo.py: `uv run --frozen python -m alibi.demo --timeout 600 [--case 1] [--reset]` starts merchant (8101), bank (8102) and app (8100), seeds the case through the real webhooks and feed, prints URLs and token names (never values). Without --reset, existing history is kept so a restart demonstrates persistence.

3. Live end to end with GEMINI_API_KEY set and ALIBI_LLM_CAP=60, on case 1 then case 2: proactive tick raises an alert before any RFC; bank_a issues rfc-0001-01; agents-mode investigation with a real lookup of PO-0001-01; review; approve; dispatch; bank_a verifies and records a disposition. Restart the app process and confirm history persists. bank_b issues rfc-0001-02: same source ids reused with no new import, different package id, fresh approval, reusing the first approval fails. Record each step and outcome in REPORTS/S5.md; save recordings as replays. Case 2 must end contradicted or unresolved for q.source_of_funds, never supported. Follow agents/data/out/demo_script.md and resolve its five [verify] items, writing the answers back into REPORTS/S5.md for the presenter.

4. Probes: run alibi/eval/probes.py against the running services, importing attempt functions from agents.eval.api and agents.execution.api where published, otherwise writing the attempts for the core set yourself; also run execution's consent_matrix flows F1 to F3; report blocked over attempted with every not_run listed.

5. Scorer: run alibi/adapter.py for rules, one_pass and agents over all dev cases including the hidden_reference, injection and alert families if present (ALIBI_LLM_CAP=600, cache on), then alibi/eval/scorer.py, printing the comparison and per-family tables. Record with `python3 tools/results.py add --split validation --variants 1 --command "<the scorer command>"` and commit agents/lead/out/results.jsonl. If the systems tie on a family, say so plainly.

6. alibi/RUNBOOK.md: exact commands for teammates to install, set .env names, start the demo, open each URL with the two tokens' names, run tests, run the adapter and scorer, switch to replay. Then go through SPEC.md's acceptance criteria one by one in REPORTS/S5.md with pass, fail or untested and the evidence.

Stop after the report. No UI polish.
```
