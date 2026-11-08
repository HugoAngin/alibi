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
- None.
