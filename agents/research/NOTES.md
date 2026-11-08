# Notes

## Assignment
From `lead` (Arnav, `Knull`), board post 6713363970 at 2026-11-07 21:40Z: deck skeleton mapped to the five rubric rows in `agents/research/out`. Nobody edits `alibi/`. The spec snapshot is `docs/SPEC.md` (alibi/SPEC.md is not on main).

## Done
- 2026-11-07: `out/DECK.md`, 17 slides plus appendix. Each slide has a working title, claim, evidence and supplier. It includes a rubric coverage table, the TASK-required slides (user, data, benchmark, results, limits, controls), the demo timing map, claims the deck must not make, and a placeholder registry (PH-01 to PH-45) mapping each number to a results key, system, split and owner.
- Demo and data slides use cases 1, 2, 25 and 39 from `agents/data/out/cases/`. I checked the amounts, refs and timings against the files.

## Decisions
- Numbers stay `{{PH-nn}}` until `eval` verifies them. Development numbers are labelled as such and never shown as held-out.
- Slides 13 and 15 have no drafted headline. It is written only after the numbers land, including if Alibi does not beat a baseline.

## Gaps raised in DECK.md
- All three closed by lead in c44079b (22:44Z): results keys `unsafe_proposals`, `lookups`, `observed_inputs`; sealed gold keeps `requires_retrieval`; models `gemini-3.8-flash` and `gemini-4-argon` pinned. DECK.md registry updated to match.

## Frontend (from lead, post 6714508805, 23:33Z)
- Research now owns `alibi/app/templates/` (team.json, setup.sh rerun). Data owns `alibi/app/static/`.
- Phase 1 done: change list in `out/FRONTEND_NOTES.md` (25 items, tagged T template / L lead / D data). Reviewed s4a 6466584 from a git-archive copy in scratch, not by switching this checkout's branch. Baseline `pytest alibi`: 22 passed.
- Phase 2: edit templates only after lead announces the S4 merge (~00:45). Then pull, rerun setup.sh, run `uv run --frozen python -m alibi.app --timeout 600` and `pytest alibi -q` before each commit.
- 23:36Z: execution (6714534193) and data (6714542491, a `need` to research) posted their notes. Both are merged into the work order at the top of `out/FRONTEND_NOTES.md`. data's requested hooks (step classes, review-group sections, data-label, home order) are items A1–A3 and 13.
- Elio approves each board post that tags someone. He approved these two:
  - 2026-11-07 ~23:45Z `need` to lead (6714587871): wire case 1 fixture, not the preview sample; attestation on source_trust/evidence; `trace[].sources[].via_lookup`.
  - 2026-11-07 ~23:45Z `need` to data (6714588820): hooks accepted; drop the `::after` panel captions; holder dates via a `when()` macro.

## Web foundation (Elio's request, 2026-11-08 ~00:00Z)
- `web/` (moved from `agents/research/web/` on 2026-11-08 ~01:05Z after lead added it to research's paths in 621b0a2): pnpm workspace with three Astro 6 sites for Cloudflare. `apps/alibi`: landing page plus a phone app view at the same `/` (server-side switch in `src/lib/view.ts`), and `/connect` consent. `apps/merchant`: simulated ResaleHub with "Connect to my Alibi" in settings and at checkout. `apps/bank`: simulated fraud console with "Request customer's Alibi". `packages/ui`: tokens, logomark, both buttons, URL contracts.
- Modelled on Elio's portfolio (`~/Documents/WebDev/eliohaddad.me`): same layering, prettier, eslint, vitest, `pnpm verify`.
- Astro 6 rather than 7: Astro 7.4 needs Node >= 22.23.4 and this machine's default is 22.14. `@astrojs/cloudflare` 13.7 with passthrough images and an in-memory session driver, so deploy provisions no Images binding or KV.
- `pnpm verify` passes (14 tests). Not deployed. Not wired to the Python app in `alibi/`. Design to come.

## Lead's 01:03Z assignment (6715391239)
- Scope: lead wanted Astro as landing page plus demo frame only. Elio decided Astro is the product front end; the backend is alibi/ and the Jinja templates are a fallback. Posted to lead 01:0xZ (6715431514) with `web/docs/backend-integration.md` (JSON contexts and actions, refusals verbatim, RFC-create and merchant-connect endpoints, credentials, route table; local demo vs public backend URL to decide by 08:00).
- Deck: `out/alibi_deck.pptx`, 17 slides from DECK.md, built by `deck/build.mjs` (`cd agents/research/deck && npm install && npm run build`). Screenshots by `deck/shots.mjs` (Astro dev servers plus s4a previews, light mode). s4a previews show template sample data and are captioned so. Charts drawn as shapes because Keynote drops pptxgenjs charts. QA: skill validator passes; slide images exported through Keynote and reviewed.
- README draft: `out/README_draft.md`. Data card and controls copied verbatim (11abc05, 06d22b1); refresh before T-1h. METHOD.md (eval) and alibi/RUNBOOK.md not landed: marked {{METHOD}} and {{RUNBOOK}}.
- Data now has 70 cases in 11 families (47/10/13); DECK.md and slide 4 updated.

## Next
- Refresh README_draft verbatim sections and deck screenshots (wired app on case 1) when they land; fill placeholders only from eval-verified records.
- Work loop: `board.py wait` in the background, act on posts addressed to research.

## Open needs
- To lead: answered 23:42Z (6714598387). Attestation pinned in d661421: `source_trust[]` and `evidence[]` carry `attestation` (issuer_signature | package_signer | none) and `redacted: bool`; a redacted extract is package_signer. Case 1 fixture regeneration and `via_lookup` / `calculation.terms` are queued for Arnav inside alibi/. Until they land: no lookup marker.
- Closed on assumption, 2026-11-08 ~00:00Z: data did not reply within 15 minutes about the `::after` captions, so I proceed assuming they come out of the CSS and the template carries the panel questions.
