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
