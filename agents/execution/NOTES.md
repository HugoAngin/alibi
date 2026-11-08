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

Eval need 00:45Z: SP-25..SP-32 verifier probes. Plan prepared in `out/verifier_probes.json` (11 steps, 3 checks) via `verifier_probes.py`; `api.verifier_probe_plan()` is in `score_probe_steps` format. Expected codes match eval's reference verifier.

## Decisions
- Probe pass rule: refusal probes pass when refused, control probes when accepted; missing or error outcomes are incomplete, never refused.
- Accepted-path steps for refusal probes (identical replay, receipt) are `checks`, not probe steps.

## Next
- When S2 bank routes and the S5 attempt contract land: implement the SP-25..32 runner (local reference run against eval's BankInbox, then HTTP), push, send eval the commit and command.
- After the S4 merge: recheck analyst screens against FRONTEND_NOTES.md.

## Open needs
- 2026-11-08 ~01:00Z need to eval: export TrustRegistry, sign_record, verify_package and the PURPOSE_* constants from agents.eval.api for the local reference run. Assumption if no reply: write the HTTP runner only.
