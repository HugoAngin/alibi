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

Eval need 00:45Z: SP-25..SP-32 verifier probes. Plan `out/verifier_probes.json` (12 steps, SP-31 identical retry uses `expected_outcome: accepted`). Eval exported helpers (d8f2aa3); local reference run against eval's BankInbox: `uv run --frozen python -m agents.execution.verifier_probes --run` → 8/8 probes, 12 steps, all planned codes, refusals stored nothing. Recorded in `out/results.jsonl`, split `reference` (kept apart from live HTTP).

## Decisions
- Probe pass rule: refusal probes pass when refused, control probes when accepted; missing or error outcomes are incomplete, never refused.
- Accepted-path steps for refusal probes (identical replay, receipt) are `checks`, not probe steps.

## Next
- Eval need 00:59Z: HTTP handlers done in `verifier_http.py`, exported as `api.verifier_http_handlers()` (probe_id -> callable(ctx) -> blocked | not_blocked | not_run). ctx contract in the module docstring: `route(name, **p)`, `http(method, path, recipient=, data=, headers=)`, `keys` (bank's pinned alibi + merchant demo keys), optional `now()`, `note()`. Refusal probes need a valid baseline accepted first; 404/405/5xx/transport/missing prereq = not_run. Tested against fake banks (correct, accept-all, refuse-all, down, 404, no routes/keys).
- When S2 routes land: check the delivery path and body schema match; S2 may use a different package schema than eval's verifier.
- After the S4 merge: recheck analyst screens against FRONTEND_NOTES.md.

## Open needs
- None.
