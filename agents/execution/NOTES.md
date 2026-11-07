# Notes

Role: `execution` (sirExile44). Writes only in `agents/execution/`. Spec: `docs/SPEC.md` (snapshot of `alibi/SPEC.md`). SPEC's execution role: reconciliation, privacy compilation/enforcement, exact approval, signing, package/source verification, minimal analyst console. Nobody edits `alibi/`.

## Assignment (lead, 21:40Z 2026-11-07)
Security probe list and analyst disposition flow as a checklist in `agents/execution/out`.

## Done (2026-11-07)
- ~22:35Z posted fyi pickup.
- `out/probes.json`: 47 probes (32 core, 5 demo, 2 controls) across consent, delivery, disclosure, policy, isolation, RFC, verification, ingest, injection, fabrication, availability, authority, secrets. Rendered to `out/PROBES.md` by `uv run --frozen python -m agents.execution.probes`.
- `out/ANALYST_FLOW.md`: verify → review (auth / source trust / claim status kept separate) → disposition (clear, escalate, request_more) → follow-up RFC with new request_id and fresh approval. Proposed disposition record shape.
- `api.py`: `load_probes`, `core_probes`, `probe_ids`, `summarise(outcomes)` → `probes_blocked` / `probes_attempted` + controls.
- Tests: `uv run --frozen python -m pytest agents/execution`.

- 22:38Z lead pinned the disposition record in `shared/CONTRACTS.md` (f67902c). 
- 22:42Z lead (9ae0ecf): verifier + bank service (inbox, delivery, receipts, dispositions) = build session S2 in `alibi/bank/`; console screens = S4 in `alibi/app/`. `PROBES.md` is the acceptance list for the S5 integration run, keep it current (edit `probes.json`, rerun `python -m agents.execution.probes`).

- 23:33Z lead need (df8cb9d): frontend Phase 1. Reviewed analyst screens on s4a (6466584, pytest alibi 22 passed) and wrote `out/FRONTEND_NOTES.md`: 6 must-fix (rejected packages still show evidence, identical verification panels, redacted extract badged issuer_verified, editable analyst name, optional reason, terminal dispositions still editable), 4 should-fix. Execution edits no templates; Elio owns `alibi/app/templates/`, Hugo `alibi/app/static/`, lead the `.py`.

## Decisions
- Probe pass rule: refusal probes pass when refused, control probes when accepted; missing outcomes count as failed.

## Next
- After the S4 merge (~00:45): recheck the analyst screens on main against FRONTEND_NOTES.md and PROBES.md.
- Wait on the board. Likely next asks: policy, signing and receipt contracts (SPEC says execution supplies them early), or a reference verifier.

## Open needs
- None.
