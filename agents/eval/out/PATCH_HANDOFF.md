# Tested app fixes prepared by eval

The application tree has one owner. These patches were developed and tested in disposable
source copies without modifying that owner's checkout. They apply against main after
`354f9e0`. Original frozen results remain unchanged.

## Approval order with an equal clock

`API_DRAFT_ORDER.patch` fixes a remaining issue after the bank-scoping fix. Sorting drafts
by `(created_at, draft_id)` uses a content hash to order reviews built at the same instant.
A previously packaged draft can therefore be selected instead of the newly approved one,
causing a false digest-mismatch refusal. The store already returns insertion order, so
a stable timestamp sort preserves the correct build order for ties. Consent digest checks
are retained.

The patch adds four public tests, including a deterministic reversed-hash-order test,
cross-bank current-draft isolation, cross-bank approval preservation and same-clock review.
The deterministic test fails on current main and passes with the patch. The delegated
review also observed the original cross-bank test failing randomly because each test makes
fresh signing keys, which change the body hash and therefore the draft-id order.

## Demo port check

`DEMO_PORT_PROBE.patch` changes only the test's port probe. On macOS, a closed listener's
TIME_WAIT sockets can make a bind without `SO_REUSEADDR` fail, despite no leaked server.
The probe now mirrors uvicorn's reuse behavior on non-Windows platforms. A real active
listener is still detected; Windows behavior is unchanged.

## Validation

- Both patches pass `git apply --check` against main at preparation time.
- Current main: the deterministic same-clock regression fails.
- Patched disposable copy: all four added approval regressions and the real three-server
  demo smoke pass (5 tests). The first scratch attempt lacked the repo's `agents` import
  path; after supplying it, the tests ran and passed.
- Current main after `354f9e0`: existing API suite passes all 36 tests.

Apply with `git apply agents/eval/out/API_DRAFT_ORDER.patch` and
`git apply agents/eval/out/DEMO_PORT_PROBE.patch`, then run:

```sh
uv run --frozen python -m pytest -q alibi/tests/test_api.py alibi/tests/test_api_draft_order.py alibi/tests/test_demo.py
```

Public accuracy patches for unresolved evidence and one-pass handling are in preparation.
