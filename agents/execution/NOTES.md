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

- 22:38Z lead pinned the disposition record in `shared/CONTRACTS.md` (f67902c). Console-UI vs verifier ownership in `alibi/` is queued for Arnav.

## Decisions
- Probe pass rule: refusal probes pass when refused, control probes when accepted; missing outcomes count as failed.

## Next
- Wait on the board. Likely next asks: policy, signing and receipt contracts (SPEC says execution supplies them early), or a reference verifier.

## Open needs
- None.
