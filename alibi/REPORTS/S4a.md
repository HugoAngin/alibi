# Session S4a report: static UI

Scope: Jinja2 templates, one stylesheet, `UI.md` and `preview_render.py` under `alibi/app/`.
No routes or application logic. Branch `s4a`, worktree `av-s4a`. Date 2026-11-07.

## What works

Every command runs from the repo root.

| Item | Proof |
| --- | --- |
| All 14 preview pages render from 10 screen templates plus base and partials | `uv run --frozen python -m alibi.app.preview_render` prints `wrote 14 pages to .../data/alibi/ui_preview` |
| Test suite: render, None-leak, badge, reconciliation, verbatim refusal, replay banner, no-JS checks | `uv run --frozen python -m pytest alibi -q` → `22 passed` |
| Browse the previews | open `data/alibi/ui_preview/index.html` (stylesheet copied next to it) |
| Contract for the wiring session | `alibi/app/UI.md` lists every variable per template; `alibi/app/preview_render.py` `ctx_*` functions are the executable version |

On Windows set `PYTHONIOENCODING=utf-8` when printing rendered HTML to the console; the badge marks are non-cp1252.
The pre-commit hook `tools/check_ownership.py` fails to decode the UTF-8 diff under the cp1252 default. Commit with `PYTHONUTF8=1 git commit ...`. That is an environment setting, not a hook bypass. Noted for `lead`'s human; `tools/` is not in this session's scope.

## Screens

| Screen | Template | Preview files |
| --- | --- | --- |
| Passport home: connections, transactions with link-state badges, evidence status, alerts, rules, requests, sharing history | `passport/home.html` | `passport_home` |
| Transaction detail: linked sources with trust badges and signature state, code-computed reconciliation `240000 - 18000 - 6000 = 216000` with `£2,160.00`, claims, "what changed" with the older revision viewable in a disclosure | `passport/transaction.html` | `transaction_detail` |
| Alerts: facts behind each alert, suggested checks, mock merchant lookup form; unknown result labelled Unverified | `passport/alerts.html` | `alerts`, `alerts_lookup_unverified` |
| Rules: plain-English input, typed interpretation (fields plus JSON), confirm or reject, active rules, held unresolved rules | `passport/rules.html` | `rules`, `rules_interpretation` |
| Request detail: the RFC, run-investigation control with call budget, trace (question → claim → sources → lookups → calculation → status), private review (included with remove, excluded with reasons, generalised, gaps, approve button naming the recipient, digest, validity window) | `requests/detail.html` | `request_detail` |
| Export: signed JSON link, print-friendly rendering, print footer stating the PDF is the browser's print of this page | `requests/export.html` | `export` |
| Dispatch result: success with receipt; refusal with the backend reason verbatim in `<pre>` | `requests/dispatch.html` | `dispatch_success`, `dispatch_refused` |
| Analyst inbox: packages bound to this recipient, requests sent | `analyst/inbox.html` | `analyst_inbox` |
| Analyst package view: three separate panels (package authentication, per-source trust, claim status), evidence by question, reconciliation, contradictions, gaps, disposition form (clear / escalate / request_more, analyst name), follow-up request form | `analyst/package.html` | `analyst_package` |
| Audit list with filter and pagination; replay banner partial when `replay_mode` is true | `audit/list.html`, `partials/replay_banner.html` | `audit`, `audit_replay` |

Shared: `base.html` (single layout, nav, flashes, replay banner include), `partials/badges.html` (trust, claim, link, status macros), `partials/money.html` (integer pence → `£2,160.00`, raw `216000 GBP`), `static/alibi.css` (screen, dark mode via `prefers-color-scheme`, print).

Badges carry a text mark and a distinct border style, so the three trust states and three claim states are distinguishable in grayscale: ● solid-filled, ◆ 2px solid, ○ dashed; ✓ solid, ✕ double, ? dashed.

## How it was built

Foundation (contract, CSS, base, macros) written by the lead session. The ten screen templates were written by four parallel agents against that contract, each followed by a skeptical review agent that re-rendered, walked the brief line by line, flipped every None-able value, and fixed defects (one found: a severity badge class with no CSS rule; export page wording). The lead then added the missing status colour rules, assembled `UI.md`, and ran the full suite.

## What failed

Nothing in scope. One environment problem, worked around: the pre-commit hook crashes on UTF-8 diffs under cp1252 (see above).

## Untested or left to the wiring session

- No browser or visual test. The previews were rendered and read as HTML; nobody opened them in a browser in this session. Dark mode and the print stylesheet are untested visually.
- `UI.md` wiring notes from the reviewers, worth reading before building routes:
  - `dispatch.html` treats `outcome == "success"` with `receipt == None` as refused. Always set `receipt` on success.
  - `review.claims[].href` in the sample points at `/requests/<id>/trace#q1`; on the detail page the trace step ids are `trc_<question_id>`.
  - `ctx_rules` puts a `kind` entry in `interpretation.policy.fields` as well as `policy.kind`, so the typed policy shows "kind" twice. Drop it from `fields` in the real context if unwanted.
  - Audit pagination does not clamp `page.number`; pass consistent values.
  - Follow-up period inputs are plain text fields holding ISO strings, not `datetime-local`.
  - Form field names per template are listed in `UI.md`.
- `home.html` hard-codes `/alerts`, `/rules`, `/requests` as section header links. They match `holder_nav` in the sample; change them if routes differ.
- Nothing in this session calls Gemini, reads `.env`, or touches `data/` other than `data/alibi/ui_preview/`.
