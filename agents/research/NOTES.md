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
- Lead answered 01:09Z (6715444461): split, JSON routes, RFC-create, merchant-connect, credentials, route table and local vs public demo are queued for Arnav (08:00 flagged). Pinned now in shared/CONTRACTS.md (03b4976): Refusals row, 4xx with {code, reason}, backend sentence verbatim, codes from execution's consent_matrix.md. Until Arnav answers: render case 1 statically, keep Jinja templates working.
- Deck: `out/alibi_deck.pptx`, 17 slides from DECK.md, built by `deck/build.mjs` (`cd agents/research/deck && npm install && npm run build`). Screenshots by `deck/shots.mjs` (Astro dev servers plus s4a previews, light mode). s4a previews show template sample data and are captioned so. Charts drawn as shapes because Keynote drops pptxgenjs charts. QA: skill validator passes; slide images exported through Keynote and reviewed.
- README draft: `out/README_draft.md`. Data card and controls copied verbatim (11abc05, 06d22b1); refresh before T-1h. METHOD.md (eval) and alibi/RUNBOOK.md not landed: marked {{METHOD}} and {{RUNBOOK}}.
- Data now has 70 cases in 11 families (47/10/13); DECK.md and slide 4 updated.

## Lead's 01:29Z assignment (6715635795)
1. Phone app screens on the S4 JSON (S4 merge ~02:30). DONE ahead of the merge (4b7a8a2): `/app/` passport home and `/app/requests/<id>/` (trace + private review + approval) built on the UI.md shapes (`web/apps/alibi/src/lib/api/types.ts`), with a case 1 fallback that says it is not live, and `/app/act` forwarding actions with refusals verbatim. Tested against a fake backend in scratch: live data, and a 409 refusal shown word for word. Jinja is the fallback; lead picks by 10:00.
2. Deck v2: 'what could fool us' slide added (bc23e3a). Still to do after S4: replace s4a screenshots with the wired app on case 1 (deck/shots.mjs).
3. Backup demo video once S5 reports the live path works; replay mode if Gemini flakes.
4. Final README from out/README_draft.md at T-1h (~11:30).
- Merchant sim renamed ResaleHub -> Mock Marketplace (`mock-marketplace`) to match case 1.

## Lead's 01:57Z decision (6715891739)
- web/ is the product face IF it runs the full demo path against the live backend by 10:00: passport, request with trace and review, approve, dispatch refusal and re-approve, analyst view for bank_a, then bank_b reuse. Otherwise Jinja (verified fallback) is used.
- Backend: every route serves its context as JSON on Accept: application/json; actions answer JSON, 4xx {code, reason}. RFC creation: POST /rfc on the bank service (8102, bearer token). Merchant connect: POST /admin/seed on 8101. Route table lands in alibi/RUNBOOK.md from S5 ~03:30.
- Demo runs locally on the presenter's laptop (Astro dev server + three services). Public URL is Tamim's Modal stretch, not a dependency.
- Bank tokens stay server-side behind the action proxy, never in the browser bundle.

## Elio's direction, 2026-11-08 ~02:05Z
- Hosting: all three Astro sites on Cloudflare Workers; they call the team's Python backend server-side through a Cloudflare Tunnel (one source of truth). D1 only for web-side state (Clerk-linked accounts, merchant connections, bank case queue). Not yet posted to lead.
- Clerk: app `alibi-app` (app_3LnvqaoNElTnELV7Pq6JsiFozUa), development instance, keys in web/apps/alibi/.env (git-ignored, never read). Join = /sign-up/ (hash routing) -> /app/. Production keys need a domain Elio owns.
- Landing (acdd607): liquid-glass island header (text wordmark "alibi"; Core, Simulate, Join), 3D passport hero in three.js (evidence tiles dock on scroll), Lenis, Core = engine / merchant+bank integration / user control. /simulate/ = Maya's case beside the live app in a phone frame. Priority was landing first; demo path wiring next.

## Modal backend (Elio, 2026-11-08 ~02:35Z)
- Backend goes on Modal under Elio's account ($51 credit). Plan in web/docs/hosting.md (bcd4055), posted to lead (6716176772). Builds on eval's agents/eval/out/MODAL_REPLAY.md, but live (Gemini on) and with a router publishing /merchant and /bank too.
- agents/research/deploy/router.py (5 tests) and modal_app.py (eea1286). SERVICES is empty until S5's alibi/RUNBOOK.md lists start commands; until then the deploy is a health check.
- Modal signed in (workspace eliohd). Modal skill installed globally (`uvx --from modal modal skills install --global --claude`).
- Deployed health-check app `alibi-backend`: https://eliohd--alibi-backend-backend.us-east.modal.run (/healthz 200; backend routes 503 until SERVICES is filled; reset routes 404). First deploy crash-looped: REPO path computed in the container; fixed with modal.is_local().
- Waiting on Elio: Modal Secret `alibi-backend` with GEMINI_API_KEY and ALIBI_MASTER_KEY (dashboard). Waiting on S5: start commands in alibi/RUNBOOK.md.

## Board posts, 2026-11-08 ~02:50Z (approved by Elio)
- done -> lead (6716337265): status of screens, landing/Clerk, rename, slide 15, Modal URL; backend NOT live yet.
- need -> eval (6716338193): don't deploy a second Modal backend; a replay can be a second function in alibi-backend. Assumption if silent: no separate replay deploy.
- need -> data, execution (6716339131): Astro is the product UI, Jinja the fallback; execution's analyst review applies to web/apps/bank (built to their list unless changed).

## 02:47Z tags handled
- data (6716350362) fixture drift: fixed in df573c5 (Maya as seller with case 1 receipts, tested; bank filler IDs txn-q-*). Replied done (6716408632).
- execution (6716350553): WEB_BANK_NOTES.md is the spec for web/apps/bank; hidden audience field removed (df573c5).
- eval (6716353417): will validate our Modal app after S5; no second deploy. Deck coverage sentence at befb305.
- Not yet folded in: eval's six-row 'what could fool us' table (METHOD.md), data's deck figures (agents/data/out/figures), METHOD.md into README_draft.

## Live on Cloudflare, 2026-11-08 ~03:25Z
- Account subdomain renamed by Elio to alibi-app.workers.dev. Workers: www (Alibi), merchant, bank. Old alibi-site / alibi-sim-* deleted at Elio's request.
- Clerk: dev instance usable-gannet-5890 (pk_test baked from .env); CLERK_SECRET_KEY is a Worker secret on www. First secret was rejected (secret-key-invalid); Elio re-stored it from .env; Join works in a real browser, no Worker errors.
- Backend address read at runtime; app shows labelled fallback (Modal answers 503) until S5 services run there.

## Front-end split, 2026-11-08 ~03:45Z (Elio)
- Asked lead (6716849163) to give execution web/apps/bank/ and data web/apps/merchant/; research keeps web/apps/alibi/, web/packages/, web/docs/, web/ root. Guide: web/docs/contributing.md (2076f6d). Bank and merchant already render per request on Workers with the adapter, so no lockfile changes needed.
- Research: Alibi app (new design, dispatch refusal + re-approve, Bank B), shared UI, integration, deploys, deck v2. Elio: cosmetic review, Clerk sign-up fields, Modal Secret.

## Alibi app, 2026-11-08 ~03:55Z (5645113, deployed)
- App view uses the site's design (tokens remapped in apps/alibi/src/styles/site.css; text wordmark, glass header, Inter).
- Approval = send (UI.md). Approval form posts result_page=sent; /app/requests/<id>/sent/ shows the Bank's receipt, or the refusal verbatim (4xx {code, reason} or dispatch.refusal with digests) and "Review and approve again". Tested end to end against the stand-in backend: refused, then delivered.
- Local launch config `alibi-site-fake-backend` (in .claude/launch.json, not committed) runs against scratchpad/fake_backend.py on :8199.

## Handoff, 2026-11-08 ~04:00Z (Elio near usage limit)
- Posted 6716936444: eval takes the Modal backend (agents/research/deploy/), lead takes deck v2 + final README (agents/research/deck/, out/DECK.md, out/README_draft.md), execution the bank console. Needs lead's team.json reassignment.
- Research now does only UI design from Elio's prompts: web/apps/alibi, web/packages, plus running Cloudflare deploys when teammates post done.

## Plan to all, 2026-11-08 ~04:05Z (6717032945)
- Target: whole system working end to end by ~06:30 UTC, then cosmetics only.
- Lead: S2-S4 merge + S5 runbook (critical path), team.json reassignments, deck v2 + README. Eval: prepares the Modal backend but does NOT deploy (Elio has no Modal seats); posts done to research, research deploys from Elio's machine with ALIBI_MODAL_SECRET=alibi-backend ALIBI_MODAL_WARM=1 and posts logs/health back. Execution: bank console. Data: merchant + Driver rehearsal.
- Elio still to create the Modal Secret alibi-backend (GEMINI_API_KEY, ALIBI_MASTER_KEY).

## Next
- data's CSS phase 2 is on main (7b4eb66, 01:37Z). It styles the Jinja hooks in out/FRONTEND_NOTES.md work order A1-A4 (trace__step--*, review-group--*, panel--*, data-label). Add them to alibi/app/templates/ after S4's routes merge (not on main at 01:40Z), so the fallback looks right.
- When S4 merges: point ALIBI_API_ORIGIN at :8100, check the JSON matches types.ts (fix shapes there if S4 differs), recapture screenshots for deck v2.
- Refresh README_draft verbatim sections and deck screenshots (wired app on case 1) when they land; fill placeholders only from eval-verified records.
- Work loop: `board.py wait` in the background, act on posts addressed to research.

## Open needs
- To lead: answered 23:42Z (6714598387). Attestation pinned in d661421: `source_trust[]` and `evidence[]` carry `attestation` (issuer_signature | package_signer | none) and `redacted: bool`; a redacted extract is package_signer. Case 1 fixture regeneration and `via_lookup` / `calculation.terms` are queued for Arnav inside alibi/. Until they land: no lookup marker.
- Closed on assumption, 2026-11-08 ~00:00Z: data did not reply within 15 minutes about the `::after` captions, so I proceed assuming they come out of the CSS and the template carries the panel questions.

## Unslop + hero stream, 2026-11-08 ~04:25Z
- Unslop pass on the landing page committed (1e3b155) and deployed to www.alibi-app.workers.dev. Warm paper palette, one ink-blue accent, stamped mono evidence refs (.s-ref), orbs/washes/glass card/sway/parallax removed, one CSS hero entrance (.s-enter). Header glass, Lenis and 3D docking kept (Elio's choice). Not rendered-checked in the browser before deploy.
- Elio asked for a new hero: the engine plus Gemini agents, with merchant, bank and mule streams flowing in and a context package flowing out to the bank, reacting to scroll. Spec in out/HERO_STREAM.md. Routed to lead and data (6717182136), asking lead to grant web/apps/alibi/src/components/site/ to one agent.
- eval's 04:17Z need (bank /act/ forwarding arbitrary actions with the Bank A bearer) concerns execution's staged agents/execution/web_bank. Execution fixes it before moving it into web/apps/bank.
- 04:30Z (6717263436): knull is out for over an hour. At Elio's request, lead's unstarted landing work went to eval. Eval stages drop-ins in agents/eval/web_landing/ (team.json is frozen until lead returns); research copies them into web/apps/alibi, verifies and deploys. Data hands its hero prototype to eval. Merchant names stay generic until Elio confirms Amazon, Deliveroo and Uniqlo.
- Execution's bank console (agents/execution/web_bank, eabc727) has been checked by eval: 64 tests pass and the open-proxy hole is fixed. It is still waiting for a path grant. Research could move it into web/apps/bank as the web/ owner.
- 04:35Z: split revised at Elio's request. Data builds the hero (agents/data/web_landing/). Execution builds deck v2 (agents/execution/deck/). Eval reviews both and keeps the Modal prep. Research copies the staged bank, merchant and hero drops into web/, verifies and deploys.

## Status at 07:15Z (after Elio's usage reset)
- e1be433: web/ belongs to execution while research is out of usage. Holder app folders went to lead (27cb13a). Research sends web changes to execution as patches.
- Live: the stream hero on www (63fa61b) and the bank analyst console on bank (/inbox/ redirects to sign-in). Merchant /seller/ is 404 live: data's seller hub is on main but not deployed.
- In progress elsewhere: the landing redesign (one scroll-driven passport stream, execution's HANDOFF_LANDING.md). t7acct (eval) is building it, and execution lands it.

## Bank console redesign, 2026-11-08 ~07:35Z (Elio)
- Built in an isolated worktree (scratchpad bank-ui) because web/ belongs to execution. Patch and notes: out/patches/bank-console-redesign.{patch,md} (8d5c40a). Posted done to execution (6718810316) to land it and deploy the bank Worker.
- The design: a graphite console; the case opens in a slide-over panel; "Request customer's alibi" leads to one review screen and then Send, which goes /act/ kind=request -> bank /rfc {request_id}. Tested against a stand-in bank service only, not the real one.

## Landing journey, 2026-11-08 ~08:35Z (Elio)
- lead opened web/ to the whole roster at 08:26 (bf2076d). setup.sh has been rerun. Holder app folders stay with lead until 09:30.
- f059307: the landing is now Journey.astro, a vertical journey in six chapters along a thin timeline line, followed by a recap. It uses the real logos in public/logos/ (Amazon, Deliveroo, UNIQLO, UCL, Santander, NatWest, Gemini). Deployed to www. It replaces Hero/Core and Tamim's horizontal stream patch.
- Posted 6719304893 to execution and eval: do not land opus_landing_handoff.patch. The bank console patch is still with Mouad; research lands it if he hasn't started.

## Responsive web app, 2026-11-08 ~10:20Z (Elio)
- 62f88e6: there is no phone-only app any more. / shows the landing on every device. /app/ is the responsive dashboard (top bar on desktop, glass dock on phones), and Join and sign-in land there. /simulate/ shows Maya's dashboard live and read-only; its request pages have actions disabled. /phone/, /preview/ and lib/view.ts are removed. MOBILE_NEEDS 1, 2, 4, 5 and 6 are done (cbc872c, a9cfbfa). S4 templates applied (a5dcaa9).
- Bank: Elio says Mouad has a newer bank UI than 19f0a56. Asked Mouad (6720184085) to push it. The live bank shows only the sign-in page because ANALYST_PASSCODE and BANK_SESSION_SECRET are not set.
- Open: eval 10:07 need. Wire the Modal launcher (agents/research/deploy/modal_app.py SERVICES) for S5 (origin/s5 a213b76) after the local two-bank gate passes.
- Logos: Elio keeps the real ones. lead wants a caption until Arnav decides; no caption added.
