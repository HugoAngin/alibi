# Frontend guide

For Elio, Hugo, Mouaad and their agents. How to run the Alibi screens, what to work on, and the rules that stop us treading on each other. Read `alibi/SPEC.md` (the product) and `alibi/app/UI.md` (the template contract) before changing anything.

## What the product looks like

Alibi is two apps in one server:

- **Holder side (Maya's phone).** `/passport` shows her connections, transactions, evidence status, alerts, privacy rules, requests and sharing history. `/txn/<id>` shows one credit, its linked merchant records with trust badges, and the reconciliation `240000 − 18000 − 6000 = 216000`. `/requests/<id>` shows a bank's Request for Context, the investigation trace (question → claim → source → calculation → status), and the private review where she removes items and approves exactly what one named bank receives. `/requests/<id>/export` is the signed JSON plus a print view.
- **Analyst side (Bank A, Bank B).** `/bank/<recipient>/inbox` and the package view, with three separate panels: package authentication, per-source trust, claim status. Then evidence by question, reconciliation, contradictions, gaps, a disposition form (clear, escalate, request more) and a follow-up form.

The four-minute demo walks: passport history and a proactive alert → merchant record and bank credit arrive → Bank A asks → investigation and the balance-release-versus-settlement check → review, approve, a tampered edit gets refused, re-approve → Bank A verifies and records a disposition → Bank B asks a different question and gets a separately approved package. Every screen in that path matters more than any other screen.

## Phase 1, now: look at the static screens

The templates are finished and tested on branch `s4a`. No routes are wired yet, so you render them with sample data.

```bash
git fetch origin && git checkout s4a
uv run --frozen python -m alibi.app.preview_render
```

That writes 14 HTML pages into `data/alibi/ui_preview/`. Open them in a browser. Then open them at phone width (browser dev tools, 390 px) and in print preview for the export page. Also run the tests once so you know the baseline:

```bash
uv run --frozen python -m pytest alibi -q
```

Write your change list in your own folder (`agents/<role>/out/FRONTEND_NOTES.md`): screen, what is wrong, what you would change. Don't edit templates yet (see Phase 2 timing).

## Phase 2, after about 00:45: edit

Session S4 is wiring routes into these templates right now and merges to `main` around 00:45. Editing the same files before that merge means conflicts with the wiring. Once the merge is announced on the board:

```bash
git checkout main && git pull --rebase origin main && ./tools/setup.sh
uv run --frozen python -m alibi.app --timeout 600     # serves http://127.0.0.1:8100
```

Later, once integration (S5) lands, `uv run --frozen python -m alibi.demo --timeout 600` starts the merchant mock, the bank mock and the app together with Maya's data loaded. Until then the app serves fixture data.

## The public web front end

`web/` is a pnpm workspace that anyone on the roster may commit to and push without asking (`open_paths` in `team.json`), deployed to Cloudflare, and independent of the Python app and its templates. Set up with `cd web && pnpm install`, preview with `pnpm dev` (Alibi site on port 4321 with the phone view at `/phone/`, merchant on 4322, bank on 4323), and check with `pnpm verify`. For the local demo, `pnpm demo` (662dbee) starts all three apps against the Python backend on 8100/8101/8102, checks each backend, and reads Bank A's token from `BANK_A_TOKEN` or `data/alibi/bank_tokens.json`; `ALIBI_REPLAY=1` puts a 'Recorded replay' banner on every page. Deploy commands and the Cloudflare login are in `web/README.md`. As of 63fa61b `web/apps/bank` is the Bank A analyst console wired to the bank service (Worker secrets `BANK_A_TOKEN`, `BANK_API_ORIGIN`, `ANALYST_PASSCODE`, `BANK_SESSION_SECRET`). Deploy runs from Elio's Cloudflare account: `pnpm install --frozen-lockfile`, then `pnpm --filter @alibi/<app> run deploy` for `alibi`, `merchant` and `bank`. It must tell the same story as the Python screens: data's case 1, the same IDs and amounts, the same three-panel distinction on the analyst side.

## Who owns what

| Who | Path | What |
| --- | --- | --- |
| research (Elio) | `alibi/app/templates/` | All templates and partials |
| lead (Elio driving), until 09:30 (27cb13a) | `web/apps/alibi/src/components/app/`, `web/apps/alibi/src/pages/app/` | Holder app mobile redesign; requests outside these folders go to `agents/lead/out/MOBILE_NEEDS.md`. |
| Everyone on the roster, no permission needed | `web/`, except the lead row above | The public Astro front end: landing page that becomes the phone app on mobile, plus the simulated merchant and bank sites. Separate from the Python app above; see `web/README.md` |
| data (Hugo) | `alibi/app/static/` | The stylesheet and any static assets |
| execution (Mouaad) | `agents/execution/out/` | Review of the analyst screens against `ANALYST_FLOW.md`; send changes to Elio |
| lead (Arnav) | `alibi/app/*.py` and everything else in `alibi/` | Routes, wiring, logic |

Pull `main` and rerun `./tools/setup.sh` first, otherwise the commit hook and your Claude settings still refuse the new paths. Commit from your own paths or `web/` only:

```bash
git add alibi/app/templates    # or alibi/app/static, or web/<path>
git commit -m "research: <what changed>"
git pull --rebase origin main && git push origin main
```

If the hook crashes on a Unicode character, you have an old `main`; pull again. Post `done` to `lead` when something is ready, `need` when you want a route, a variable or a logic change. Anything in a `.py` file is a `need` to lead, not an edit.

## The contract: don't break the wiring

- `alibi/app/UI.md` lists every variable each template receives. Keep names and shapes exactly. If you need a new variable, post a `need` to lead with the name and shape; don't invent one.
- Form field names are part of the contract too. Renaming an input breaks the route behind it.
- One stylesheet, `alibi/app/static/alibi.css`. No frameworks, no build step, no CDN. Plain JS only for disclosure toggles.
- Keep the three trust badges and the three claim-status badges distinguishable in grayscale (they use text marks and border styles for that). Don't collapse them into colour alone.
- Keep the print stylesheet on the export page and its footer line saying the PDF is the browser's print.
- Single column at phone width, 16 px side gutters, system font stack. No gradients, no emoji, no stock-photo hero sections. This is a bank-grade tool.
- Run `uv run --frozen python -m pytest alibi -q` before every commit. The render tests catch a missing variable or a broken macro.

## What to work on, in order of value to the judges

1. **The trace view on the request page.** Traceable reasoning is 25% of the score. A judge should be able to follow question → claim → source id and revision → calculation → status without being told how. Make the chain visually obvious and the source links prominent.
2. **The private review.** Included, excluded (with reasons) and generalised items must read as three clear groups. The approve button must name the recipient. The approval digest and validity window should be visible but secondary.
3. **The analyst package view.** The three panels (package authentication, per-source trust, claim status) must look like three different things. Judges will check that a valid signature does not get presented as "everything is true".
4. **Transaction detail.** The reconciliation table and the "what changed" section with the older revision. Make the older revision obviously historical.
5. **Refusal states.** The dispatch refusal shows the backend's reason verbatim. It should look like a block, not an error toast.
6. **Passport home.** Hierarchy: alerts and open requests first, history second, connections last.
7. **Mobile and print.** Every holder screen at 390 px; the export page in print preview.
8. **Empty states and copy.** No lorem ipsum, no "TODO", no jargon Maya wouldn't use. Analyst screens can be terse; holder screens should be plain English.
9. **Deck screenshots.** Elio: once screens are final, capture each demo step at a consistent window size for the deck.

Keep notes of what you changed and why in `agents/<role>/out/FRONTEND_NOTES.md`; the README's limits section will draw on it.
