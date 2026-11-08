# Notes

Role: `execution` (sirExile44). Writes only in `agents/execution/`. Spec: `docs/SPEC.md` (snapshot of `alibi/SPEC.md`). SPEC's execution role: reconciliation, privacy compilation/enforcement, exact approval, signing, package/source verification, minimal analyst console. Nobody edits `alibi/` (build sessions S2 bank/verifier, S4 app screens, S5 integration).

## Done
2026-11-07
- 21:40Z assignment: `out/probes.json` → `out/PROBES.md` (47 probes, 32 core), `out/ANALYST_FLOW.md`. Lead pinned the disposition record (f67902c). PROBES.md is the S5 acceptance list; keep it current.
- 23:33Z frontend Phase 1: `out/FRONTEND_NOTES.md`, analyst-screen review of s4a (6 must-fix, 4 should-fix). Elio owns templates, Hugo static, lead the `.py`.

2026-11-08, lead work order 00:45Z (5107aca), all posted done:
- (1) `out/policy_rules.jsonl` + `out/policy_material.json`, 40 rules vs s3a `policy_parse.json` schema. Generator `policy_fixture.py`. 7e51e7f.
- (2) `out/injections.jsonl`, 25 strings on dev cases 1/2. Generator `injection_fixture.py`. a27fcad.
- (3) `out/consent_matrix.md`, triggers C1–C16 + HTTP flows F1–F3. Routes from S4 form actions; RFC intake and merchant events are placeholders. b5cf448. F2 updated to inherited txn_ids per lead 7a7ba1e (follow-up keeps parent scope; wider = new RFC).
- (4) `out/CONTROLS_SLIDE.md`, six threat rows → control, IDs, evidence; README limits draft. 06d22b1.

Eval need 00:45Z: SP-25..SP-32 verifier probes. Plan `out/verifier_probes.json` (12 steps, SP-31 identical retry uses `expected_outcome: accepted`). Eval exported helpers (d8f2aa3); local reference run against eval's BankInbox: `uv run --frozen python -m agents.execution.verifier_probes --run` → 8/8 probes, 12 steps, all planned codes, refusals stored nothing. Re-recorded 01:2xZ per eval (record fdd4570242 at published commit 2be6728; the first record pointed at unpublished 01faf11 because of a rebase). Scoring now fails a probe on a wrong refusal code, stored refused bytes or a failed acceptance postcondition. Lesson: push code first, then `results.py add`, then commit results.jsonl.

Lead work order 01:04Z (59b6c33):
- (1) `policy_harness.py`: runs policy_rules.jsonl through a parser, scores correct / held / safe_wrong / leak / invalid / error. Adapters: `rules` = agents.lead.api.parse_rule_text (not on main yet → unavailable), `gemini` = alibi.llm FLASH + policy_parse prompt (only with --parser gemini; wait for S3 per lead). Tested with oracle, null, fake Gemini. ff068b4.
- (2) SP-25..32 HTTP handlers: done above (dba6019); no 00:20 route contract exists (lead NOTES confirms).
- (3) `out/judge_qa.md`: 20 Q&As with evidence paths; pending numbers marked.

Eval need 01:13Z: SP-47 at-rest probe `at_rest_probe.py`. Plants canaries in case 1 free text, persists + reads back via the store's app path, then scans the raw store and sidecars without the key (raw, utf16, base64 x3 alignments, hex, url, gunzip) for canaries and key material; redacted report. Controls 7/7 (plaintext, base64, ciphertext, key beside data, plaintext sidecar, missing store, no adapter). App store not_run: hook `alibi.passport.store.probe_adapter` is a placeholder name until the store is published.

Lead 01:27Z (d5f78a5): heavier work assigned, (a) then (b); (c) skipped (eval has agents.eval.settlement); (d) queued for Arnav.
- (a) `policy.py`: compile_policy / apply_policy / record_index exported from api; Policy row in CONTRACTS points here. Code text parser: fixture 40/40 0 leaks (tuned, record 9a0a1cdabb), holdout 12 written+committed first (294cb91): 7/12, 0 leaks (record d9601d98d6). Harness `--parser execution`, `--holdout`.
- (b) `approval_gate.py`: ApprovalGate (register_request, set_draft, approve, dispatch, policy_changed, source_corrected), CONSENT_MATRIX code -> rows + verbatim template; api.approval_gate(). Tests C1-C16 + F1/F3 + drift check against consent_matrix.md.

Lead 01:29Z (5 items): (3)(4) done earlier. (5) `tamper_demo.py` + `out/TAMPER_DEMO.md` (real alibi sign/verify; bytes 4589..4594 216000->261000; rejected). (2) `consent_flows_http.py` F1-F3 on ProbeSession, fake-app tests, LiveStack skipped until ALIBI_LIVE_CONFIG. (1) `out/regulatory_frame.md`, cited, 'not lawyers', (check) marks on commencement/case law.
- Lead parser harness: 18/40, 0 leaks; holdout 5/12, 0 leaks (records 41b4ec66ac, f62892b17d).
- SP-47 on real store: configured key -> blocked; default layout -> not_blocked (master.key beside passport.db). Record dfb59fcdbf.

Lead 01:57Z (db1e402): (1) `test_dispatch_conformance.py`, (2) `test_audit_coverage.py` done. S1 FINDINGS: C2 a new review (Maya removes rfd_2001) does not invalidate the earlier approval, so sign_and_package sends the pre-edit body with rfd_2001; C6 the superseded draft can still be approved. Audit gaps: no link entry; delivery receipt, disposition, follow-up are S2. (3) S2 security review when S2 merges (~02:30). (4) 09:00 judge drill needs the human presenter. (5) Gemini parser score after S3.
- `refusal_codes.py`: core ApprovalError problems -> consent_matrix codes (asked lead to route to S2).

04:00-04:40Z web + deck:
- Bank console staged at `agents/execution/web_bank/` (research copies it into web/apps/bank). Eval found an open proxy in /act/ -> fixed (fixed kinds, server-built paths, signed analyst session, same-origin); then a dotted-name session bug and a TS2345 in tests -> fixed (eabc727). Secrets: BANK_A_TOKEN, ANALYST_PASSCODE, BANK_SESSION_SECRET.
- Deck v2 staged at `agents/execution/deck/` (daa4511) from research's deck; README lists changes. Not built: npm not installed here (human's OK pending); asked research/eval to build. Retake landing after data's hero, bank shots after the console deploys.

- 06:26Z: with the human's OK, Node 22 + pnpm via `npm exec` (system Node untouched); staged console overlaid on a scratch copy of web/: `pnpm verify` green (astro check 0/0 x3, 74 tests, 3 builds). Formatted files copied back.

- 06:35Z: lead moved web/ to execution (e1be433). Landed eval's UI patch + bank console in web/ (63fa61b); pnpm verify green (astro 0/0 x3, 76 tests, 3 builds). Deploy needs Elio's Cloudflare account.

- 07:05Z: landing redesign (Mouaad's brief: scroll-driven passport stream) handed to Tamim/t7acct per Mouaad: `out/HANDOFF_LANDING.md`, starter `web/apps/alibi/src/lib/stream.ts` (3bbef59). Board need posted 6718530346; web/ still ours, so t7acct asks lead or sends a patch.
- 07:15Z: lead 07:02 (e8efea1) demo holder mode: `web/apps/alibi/src/lib/demoHolder.ts` + middleware (no Clerk when ALIBI_DEMO_HOLDER=1 + HOLDER_SESSION_SECRET; non-loopback host 403; /app, /phone, /connect and the phone view of / need the HMAC cookie), `/demo-sign-in/` + `/demo-session/`; demo.mjs binds 127.0.0.1, fresh holder secret, prints only hints for supplied credentials. Verify green (86 tests). Posted done to eval for a clean-checkout rerun.
- 07:12Z lead (27cb13a): until 09:30 lead/Elio owns web/apps/alibi/src/components/app/ and src/pages/app/ (mobile redesign); do not edit them. Their requests outside those folders land in agents/lead/out/MOBILE_NEEDS.md. Launcher + screenshots stay ours. Lead lists the landing as ours; it was handed to t7acct per Mouaad.
- 08:49Z lead (bbb14db): research owns web/apps/alibi (landing final, holder app); execution owns the rest of web/ (bank console, merchant, packages/ui, launcher, root config). 09:05Z: landed research's bank console redesign (19f0a56, verify green 96 tests); SP-47 scoped to the passport store in deck/judge_qa/controls (4b7393d). Not yet run against the real bank service.
- 09:20-10:00Z: lead's S5 post retracted 09:49 (S5 not on main; wait for a lead post with SHA+tag before verifying the sealed run). Bank console checked vs rehearsal mock bank on 8111/8112 (own state file data/alibi/exec_test/): all screens 200, Send -> POST /rfc accepted. Someone's stack runs on 8100-8102; leave it. Merchant patch landed (ca03c47), MOBILE_NEEDS 3 done (485db1e), 1/2/4/5/6 are research's. Tamper demo fixed per eval ae17c2d (8da8eb4).

## Decisions
- Probe pass rule: refusal probes pass when refused, control probes when accepted; missing or error outcomes are incomplete, never refused.
- Accepted-path steps for refusal probes (identical replay, receipt) are `checks`, not probe steps.

## Next
- When the passport store lands: wire `app_store()` to it (root, key_material, persist, read_all), run, post the outcome to eval.
- When agents.lead.api.parse_rule_text lands: run `uv run --frozen python -m agents.execution.policy_harness`, record with results.py, put counts on CONTROLS_SLIDE / judge_qa Q15. Gemini run after S3 merges.
- Eval need 01:08Z: handlers moved onto eval's ProbeSession (agents/eval/http_probe_runtime.py): routes `verifier_deliver`/`verifier_inbox`, fixtures alibi/merchant key seeds (hex) + key ids, optional `verifier_codes`; blocked = check passed incl. controls. Compatibility test runs eval's attempt() against a local HTTP bank over BankInbox.
- (superseded) Eval need 00:59Z: HTTP handlers done in `verifier_http.py`, exported as `api.verifier_http_handlers()` (probe_id -> callable(ctx) -> blocked | not_blocked | not_run). ctx contract in the module docstring: `route(name, **p)`, `http(method, path, recipient=, data=, headers=)`, `keys` (bank's pinned alibi + merchant demo keys), optional `now()`, `note()`. Refusal probes need a valid baseline accepted first; 404/405/5xx/transport/missing prereq = not_run. Tested against fake banks (correct, accept-all, refuse-all, down, 404, no routes/keys).
- When S2 routes land: check the delivery path and body schema match; S2 may use a different package schema than eval's verifier.
- After the S4 merge: recheck analyst screens against FRONTEND_NOTES.md.

## Open needs
- None. Waiting: lead's S5 SHA + sealed-candidate tag, then verify eval's sealed run. (Deploy to Cloudflare waits on Elio's account.)
