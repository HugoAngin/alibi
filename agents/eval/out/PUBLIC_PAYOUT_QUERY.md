# Public payout query: connector-backed `lookup_payouts` (public development only)

Status: staged, not committed. This is a public 80-case development diagnostic only. It is not
an unseen result and does not touch the frozen captures, root's candidate 5 capture, or the
other staged patches.

It closes `alibi/REPORTS/S3.md` open issue 2 (the agents' `lookup_payouts_covering` and
`lookup_by_date` were served from the passport listing, not the merchant). It also wires a
bounded rules fallback for public cases 42 (`orders_and_weekday`) and 43 (`date_only`).

## Files

- `PUBLIC_PAYOUT_QUERY.patch` (sha256 `ad992b9f…4e74`): against `/tmp/retr-eval-work`, which
  is main plus the staged retrieval and one-pass patches. `git apply --check` passes there.
  It does not apply to bare main.
- `PUBLIC_PAYOUT_QUERY_AFTER_ANSWER.patch` (sha256 `9dc63d67…5876`): the same change against
  that tree plus `ANSWER_ACCURACY.patch` (sha256 `3517b18b…6d`). The two conflict only in the
  `api.py` and `one_pass.py` signatures, where both add a keyword argument. Use this one if
  ANSWER lands first.

Touched: `alibi/gateway.py`, `alibi/connectors/clients.py`, `alibi/api.py`,
`alibi/agents/runner.py`, `alibi/agents/actions.py`, `alibi/baselines/one_pass.py`
(pass-through only), `alibi/core/investigate_rules.py`, `alibi/adapter.py`, and a new
`alibi/tests/test_payout_query.py` with 8 tests. No schema, mapping, scorer, gold, policy,
package or model-call changes.

## What it does

- **`Gateway.lookup_payouts(covers_record_id=… | payout_date=…)`**, through a new optional
  `payout_query(key, value)` connector. This is `MerchantClient.payout_query`, which reuses
  `MerchantClient.lookup` (`/payouts?covers=` / `?payout_date=`). `connect()` returns it, so the
  adapter's real merchant connector supplies it. There is no fixture fake. It runs the same
  checks as `lookup_settlement`:
  - **Scope before any connector call.**
    - `covers_record_id` must be one of this subject's active, issuer-verified order, refund
      or fee records. Another subject's record, a self-supplied record or another record kind
      is refused.
    - `payout_date` must be a real `YYYY-MM-DD` day, no later than the clock, and 0–5 days
      before one of the subject's own bank credits.
  - **Every result** must be a payout or settlement for this subject. It must come from the
    covered record's issuer (for a date query, from one of the subject's verified issuers),
    and it must cover the record or fall on that day. It must then pass signature
    verification through `ingest_merchant_event` with connector `merchant_lookup`, and a
    post-ingest check.
  - **All or nothing.** If any result fails, or the merchant returns more than 10, the whole
    answer is rejected and nothing is kept.
- **Rules fallback (`fetch_by_payout_clue`).** It runs only when the credit, the passport and
  issuer-named references all fail to match, and only when a payout query connector exists.
  It reads explicit clues in issuer-verified message text and makes at most 2 queries:
  - `#<digits>` that equals the `payment_ref` of an issuer-verified order record from the same
    issuer. This queries by `covers_record_id`.
  - An explicit `<day> <Month>` date. Any weekday written with it must agree, the date must be
    no later than the text, and it must fall in the match window before the credit. This
    queries by `payout_date`. A weekday on its own is never used.

  The returned payout must still match the credit by amount, currency and date. There is no
  reference guessing and no table of case ids. The rules strategy string in `adapter.py`
  declares the new step, and the adapter's `lookups` count now includes `lookup_payouts`.
- **Agents.** `lookup_payouts_covering` and `lookup_by_date` call `gateway.lookup_payouts`
  when the gateway has a payout query connector, and keep the passport listing otherwise. The
  existing check that the `record_id` is already in the retrieved material is unchanged.
- **One-pass.** It accepts `payout_query` and hands it to its gateway, but never calls it. Its
  fixed strategy is unchanged.

## Evidence

Run from the work copy (`/tmp/payout-query-work` or `/tmp/pq-stack`):

- `uv run --frozen python -m pytest -q alibi/tests/test_payout_query.py`: 8 passed. The tests
  cover:
  - covered-record and date queries that ingest signed payouts;
  - refusal of a foreign-subject record, a self-supplied record and a non-component record,
    with no connector call;
  - whole-answer rejection for another issuer or signer, another subject, a payout that does
    not cover the record, a non-payout kind, a tampered signature, and more than 10 results;
  - dates outside the window, after the booking, malformed or impossible;
  - an unavailable connector or registry;
  - public 42 and 43 end to end, with gold removed from the case;
  - agent actions routed through the gateway.
- Focused neighbouring suites: 560 passed on the base patch (gateway, settlement batch,
  adapter, agents foundation and runner, investigate rules, eval rules reference, one-pass,
  connectors). On the ANSWER stack: 373 passed, including `test_answer_accuracy` and
  `test_core_investigation_fixes`. The full suite was not run.
- I compared rules outputs on all 80 public cases, base against patched, on both stacks. This
  compares outputs only and does not use gold or the scorer. Only cases 42 and 43 changed, and
  there were no errors:
  - 42: `unresolved`/unmatched → `supported`/linked `rec-0042-022`, found through order
    `#1291`.
  - 43: `unresolved`/unmatched → `supported`/linked `rec-0043-018`, found through `24 June`.

## Not done / residual

- The agents' `Material.lookup_count` still counts only `lookup_settlement` and
  `refresh_txn`, so a merchant-backed covering or date action is not counted in the agents'
  `retrieval.lookups`.
- The prompts were not changed. Live Gemini has never chosen these actions (S3 report), so
  agents-mode gains on 42 and 43 are untested. No model calls were made.
- Case 42's weekday-only clue ("went out on Sunday") is deliberately not used. It resolves
  through the order number.
