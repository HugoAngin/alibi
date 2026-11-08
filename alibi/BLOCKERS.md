# S4 blockers and limits

Recorded by the S4 wiring session (lead). Nothing here stopped the build; each item is a limit
the app shows honestly rather than works around.

## Rules engine verdicts on the team cases (not an S4 defect)

The app calls `run_investigation(mode="rules")` (S1). Its keyword classifier
(`alibi/core/investigate_rules.py`, S1 open issue 7) does not map two of data's pinned questions:

| Case | Request | Question | Rules verdict | Why |
| --- | --- | --- | --- | --- |
| 1 and 2 | `rfc-000n-01` (bank_a) | `q.breakdown` "Please show how this amount was calculated." / "How does the credit reconcile to your underlying sales, refunds and fees?" | `unresolved`, gap "The rules engine could not map this question to evidence it can check." | No source cue word (origin, where, explain...) and the request has no period. |
| 1 and 2 | `rfc-000n-02` (bank_b) | `q.sales_refunds_in_period` "Please provide documented marketplace sales and refunds between ..." | `unresolved`, gap "The question names sales and refunds without asking for a net figure..." | The engine answers one total per question, or a net total with a net cue; data asks for two totals in one question. |

`q.source_of_funds` is `supported` on case 1 and `contradicted` on case 2, as the demo needs.
Command that shows it: `uv run --frozen python -m pytest alibi/tests/test_app_cases.py -q`.

Fix belongs to the engine owner (S1 core) or to the `agents` mode when it lands: either classify
"how was this calculated / reconcile" as a breakdown question and sum several kinds when asked
for "sales and refunds", or let data split Bank B's question. The UI labels every run
"rules-only" and shows the gap text verbatim.

## `mode="agents"` is not importable

`alibi.api.run_investigation` raises `NotImplementedError` for any mode but `rules`, and
`alibi/agents/` holds prompts only (no package, no `policy_parse.py`). The app therefore runs
`mode="rules"` with a visible "rules-only" label and uses `alibi.core.policy.parse_rule_text`
for `/rules`. Nothing is faked as an agent result.

## Alert schema gaps (core)

`alibi.core.schemas.AlertKind` has no `settled_not_received`, and `Alert.severity` allows only
`info | warning`. The monitor skips the `settled_not_received` rule and stores
`settlement_discrepancy` as `warning` where data's rules say `high`.

## Template commits are blocked by ownership (needs lead's human)

`team.json` gives `alibi/app/templates/` to `research`, so the pre-commit hook refuses lead's
commits of the hook-and-binding edits the S4 brief asked for (ten templates and the new
`partials/when.html`). The edits are in the worktree and saved as
`agents/lead/out/s4_templates.patch` (apply with `git apply agents/lead/out/s4_templates.patch`
from the repo root). Until research commits them (or `team.json` adds the path to lead),
`alibi/tests/test_app_routes.py` and `alibi/tests/test_ui_preview.py` pass only in a checkout
that has the patch applied. No override was used.

## Refusal codes beyond the consent matrix (a `need` to lead, not posted from this session)

`shared/CONTRACTS.md` pins the refusal codes to `agents/execution/out/consent_matrix.md` and says
a new code is a `need` to `lead`. The app needed codes the matrix lacks (`not_authenticated`,
`recipient_mismatch`, `cross_site_post`, `item_not_in_draft`, `request_already_answered`,
`approval_consumed`, `draft_missing`, `package_rejected`, `not_a_package`, `bad_request`,
`bad_period`, `bad_decision`, `reason_required`, `question_required`, `parent_not_found`,
`package_not_found`, `rule_not_pending`, `rule_not_active`, `policy_rejected`,
`investigation_failed`, `review_failed`, `bank_unreachable`, `bank_bad_response`,
`delivery_rejected`, `not_dispatched`, `approval_invalid`, `unknown_recipient`). This session may
not edit `shared/` or post to the board, so the full list with meanings is in `alibi/app/UI.md`
("Routes, JSON and credentials") for lead to pin or for execution to add to the matrix.

## S5: the S4 template bindings still cannot be committed (ownership)

Recorded by the S5 integration session (lead), 2026-11-08. `alibi/app/templates/` is owned by
research in `team.json`, so the pre-commit hook refuses lead's commit of the ten edited templates
and `partials/when.html` from `agents/lead/out/s4_templates.patch` (the S4 bindings for a null
`period`, `txn_ids`, `via_lookup`, `timing`, the `when()` macro and the stylesheet hooks). The
patch is applied in this worktree, uncommitted, so `uv run --frozen python -m pytest alibi -q`
is green here. On a checkout without it, `alibi/tests/test_app_routes.py` (21 tests) and
`alibi/tests/test_ui_preview.py` (9 tests) fail with `'None' has no attribute 'from'` from
`requests/detail.html`. Command that shows it: `git stash push -u -m s5-templates -- alibi/app/templates && uv run --frozen python -m pytest alibi/tests/test_ui_preview.py -q`
(then `git stash pop`). Fix: research commits the patch (`git apply agents/lead/out/s4_templates.patch`
from the repo root) or `team.json` adds `alibi/app/templates/` to lead. No override was used.

Update 2026-11-08 ~10:00Z: research applied the S4 patch on `main` (a5dcaa9), so the S4 bindings
are committed. The remaining uncommitted delta is `agents/lead/out/s5_templates.patch` (57 lines):
the Send button after approval in `requests/detail.html` (QA P0 3) and the `not_attempted` branch
of `requests/dispatch.html` (QA P0 4). Research applies it with `git apply agents/lead/out/s5_templates.patch`.
