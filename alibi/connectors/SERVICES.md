# S2 simulated services: routes, shapes and rules

The contract the S2 modules build against. Owner: lead (session S2). The core API is
`alibi/CONTRACTS.md`; the case schema is `agents/data/README.md`; the mapping is `MAPPING.md`.

Conventions for every service:

- Money is integer pence with an ISO currency code; times are UTC ISO-8601 with `Z`; periods are
  half-open `[from, to)`.
- Every refusal is a 4xx with body `{"code", "reason", ...}` through `alibi.connectors.http.Refusal`
  (codes in `http.CODES`). Request validation errors are `400 invalid_request`. Reasons name ids,
  fields and codes, never payload contents or credentials.
- Each service has a simulated clock (`case_source.CaseClock`): `as_of` defaults to the case's
  first RFC `issued_at`; `POST /admin/advance?as_of=` moves it forward only (`409 clock_backwards`).
  Nothing with `received_at` / `available_from` / `issued_at` after `as_of` is visible.
- Cases load through `case_source.load_case_without_gold(n)` (`404 unknown_case` when hidden).
- Signing: `alibi.connectors.keys.connector_keystore()`; merchant events with kid `merchant_mock`
  (issuer `mock-marketplace`), requests with `bank_a` / `bank_b`. Ed25519 is deterministic, so
  re-signing identical bytes gives identical bytes.
- Tests use FastAPI's `TestClient`, `tmp_path`, `connector_keystore(tmp_path / "keys", seed=...)`,
  and never touch `data/alibi/`, the network or Gemini.
- Generated files (tokens, state) go under `data/alibi/` (git-ignored) and are never logged.

## Record envelope

Everything the merchant serves or pushes is

```json
{"event": {"...core MerchantEvent JSON..."}, "signature": {"alg": "Ed25519", "kid": "merchant_mock", "domain": "alibi:record:v1", "sig": "..."}, "extra": {"case_kind": "payout", "payout_reference": "PO-0001-01", "...": "..."}}
```

`{"event", "signature"}` alone is exactly what `alibi.api.ingest_merchant_event` takes; `extra`
is stripped before anything enters the passport. Webhook pushes carry only `event` and
`signature`.

## Mock merchant (`alibi/connectors/merchant.py`, port 8101)

`create_merchant_app(sink=None, *, keystore, passport_url=None, case_loader=load_case_without_gold) -> FastAPI`

Sink: `alibi.connectors.plug.Sink`, `sink(payload, received_at) -> {"status": inserted | duplicate |
corrected | stale | refused | error, "reason"?, "record_id"?, "revision"?}`. With `passport_url`
instead, the merchant POSTs the payload as JSON to `{passport_url}?received_at=<utc>`; a 2xx body
is the outcome (`status` key), a 4xx body gives `refused` with its `reason`, anything else is
`error`. With neither, pushes are recorded with outcome `not_delivered` and the records are
still served by the lookup routes.

| Route | Does | Response |
| --- | --- | --- |
| `GET /` | Service identity | `{service: "mock-marketplace", issuer_id, kid, case_number, as_of}` |
| `POST /admin/seed?case=N&as_of=` | Load case N (default 1) without gold, reset state, set the clock (default: first RFC `issued_at`), push every visible `merchant_deliveries` entry in arrival order, duplicates, conflicts and stale revisions included, each mapped with `map_merchant_event` and signed. `received_at` of each delivery is passed to the sink as the passport's clock. | `{case_number, subject_ref, issuer_id, as_of, pushed: [{seq, event_id, record_id, revision, received_at, outcome, reason}], counts: {inserted, duplicate, corrected, stale, refused, error, not_delivered}}` |
| `POST /admin/advance?as_of=` | Move the clock forward; push deliveries that became visible. | `{as_of, pushed: [...]}`; `409 clock_backwards` |
| `POST /admin/retry` body `{"event_id"}` | Re-send the exact cached bytes of a pushed delivery. | `{event_id, payload_sha256, same_bytes: true, outcome, reason}`; `404 unknown_event` |
| `POST /admin/correct` body `{"record_id", "changes": {case fields}, "correction_reason"}` | Take the latest visible revision of the record, apply the case-field changes, set `revision + 1`, `event_id` = `<old event_id>-r<revision>`, `correction_reason`; append it to the case pool at the clock time (so lookups and `GET /records` see it) and push it. Identity fields (`record_id`, `issuer_id`, `subject_ref`, `kind`) cannot change: `400 invalid_correction`. | `{event_id, record_id, revision, received_at, outcome, reason, record: {event, signature, extra}}`; `404 unknown_record` |
| `GET /records/{record_id}` | Every visible event for that record (all revisions, deduplicated by `event_id`). | `{as_of, query: {record_id}, records: [envelope...]}`; `404 not_found` when none |
| `GET /settlements?payout_reference=` | Payout records with exactly that reference visible now (pushed and lookup-only). No fuzzy matching. | `{as_of, query, records: [...]}` (empty list when none) |
| `GET /payouts?covers=rec-...` or `?payout_date=YYYY-MM-DD` or `?from=YYYY-MM-DD&to=YYYY-MM-DD` (inclusive, at most 31 days) or `?payout_reference=` | Payouts by one key, through `CaseClock.lookup`. Several keys or none: `400 invalid_query`. | `{as_of, query, records: [...]}` |
| `GET /admin/lookups` | Served lookups that passed validation (empty results included), for eval's `lookups` count. | `{lookups: [{at, route, query, record_ids}]}` |
| `GET /admin/deliveries` | The webhook log with the exact pushed payloads, in push order, so a passport that was not reachable at seed time replays the pushes (`clients.bootstrap`). | `{as_of, deliveries: [{seq, event_id, record_id, revision, received_at, outcome, reason, payload_sha256, payload: {event, signature}}]}` |
| `GET /admin/state` | | `{case_number, as_of, pushed: [log entries without payloads], corrections, lookups_served: int}` |

Merchant details settled during the build:

- Before any seed, every route but `GET /` (which returns `case_number` and `as_of` as `null`),
  `GET /admin/lookups` and `GET /admin/state` refuses `404 unknown_case`.
- `as_of` on seed and advance must be a full timezone-aware timestamp (`400 invalid_time`); it is
  floored to a whole UTC second, so pushes and lookups agree on visibility.
- Pushes follow the case's `merchant_deliveries` list order (the data layer's arrival order), which
  is also `gold.ingest` index order. Log entries carry `payload_sha256` so a retry can be checked.
- `/admin/correct` also refuses `revision` and `event_id` in `changes`, and an empty
  `correction_reason` (`400 invalid_correction`); a change that no longer fits the core schema is
  `400 invalid_correction` too. `event_id` of a correction is `<old event_id>-r<revision>`.
- `/payouts`: a malformed date is `400 invalid_time`; `from` without `to`, `from > to`, more than
  31 days, an empty value, several keys or none is `400 invalid_query`. `payout_date` accepts any
  ISO spelling `date.fromisoformat` takes and is normalised before the lookup.
- Results after a correction follow `agents.data.api.lookup`: pushed deliveries (corrections
  appended) first, then lookup-only records. Consumers must not rely on revision order.
- A sink that raises or returns an unknown status is logged as outcome `error` with the exception
  type only; the route never fails.
- Lookup-only records appear at their `available_from`, not at `status_as_of`: case 2's pending
  payout `PO-0002-01` is served from 2026-10-29T06:35:00Z, five minutes after its `status_as_of`
  06:30:00Z. That is the data layer's rule (`agents.data.api.lookup`).
- `/settlements` and `/payouts` serve a revision only while the record's latest visible revision
  also matches the query: after a correction moves `payout_reference`, the old reference returns
  an empty list and the new one the latest revision. `/records/{id}` still lists every revision.
- `/admin/correct` value shapes: `occurred_at`, `settled_at`, `status_as_of` must be UTC ISO-8601
  strings (normalised to whole seconds in the pool and the signed body alike), `covers_record_ids`
  a list of ids, `amount_minor` an integer, text fields strings; anything else is
  `400 invalid_correction` before the pool is touched.

Admin routes are unauthenticated local demo controls. Lookup routes are open: the passport's
gateway binds lookups to the subject's own issuers.

## Mock bank (`alibi/bank/service.py`, port 8102)

`create_bank_app(*, keystore, registry, tokens=None, state_path=None, rfc_sink=None, case_loader=load_case_without_gold) -> FastAPI`

Identities: `bank_a` (analyst `analyst_a1`) and `bank_b` (analyst `analyst_b1`), plus the holder's
feed connection. Tokens: `tokens` dict `{"bank_a", "bank_b", "feed"}`, else environment
`ALIBI_BANK_A_TOKEN`, `ALIBI_BANK_B_TOKEN`, `ALIBI_FEED_TOKEN`, else `data/alibi/bank_tokens.json`
(created with random tokens, mode 0600; `load_tokens(path)`). Auth is `Authorization: Bearer <token>`;
missing or wrong gives `401 unauthenticated`. The recipient and analyst come from the token only;
posted `recipient`, `analyst` or `analyst_name` fields are ignored.

State (`alibi/bank/state.py`, `BankState`): in memory, or persisted as JSON at `state_path`
(atomic replace on every write). Per recipient: requests sent, inbox entries, dispositions,
audit. Rejected inbox entries keep reasons and digests only, never the package bytes.

The `rfc_sink` is `alibi.connectors.plug.RfcSink`: `sink(signed_request, recipient, account_refs, now) ->
{"status": accepted | refused, "reason"?}`; it is the passport's `receive_rfc`. Without one,
`forwarded` is `null` and the caller feeds the returned `rfc` to the passport itself.

| Route | Auth | Does | Response |
| --- | --- | --- | --- |
| `POST /admin/seed?case=N&as_of=` | none | Load the case: subject, account_ref, bank rows (feed), RFCs. Reset all recipient state. Set the clock. | `{case_number, subject_ref, account_ref, as_of, recipients: ["bank_a", "bank_b"]}` |
| `POST /admin/advance?as_of=` | none | Clock forward only. | `{as_of}`; `409 clock_backwards` |
| `GET /whoami` | recipient | | `{recipient, analyst}` |
| `POST /rfc` body `{}` or `{"request_id"}` or `{"request": {...}}` | recipient | Issue a Request for Context. `{}`: the case's first unissued RFC whose audience is this recipient. `request_id`: that case RFC (must have this audience, else `404 unknown_request`, same reason as a missing id). `request`: a custom request body; `issuer` and `audience` are forced to this recipient, `subject_ref` must be the case subject, scope rule applies. Mapped with `map_rfc`, signed with this bank's key (request domain). Refused with `409 request_expired` when the clock is at or past `expires_at`. Re-issuing an issued id returns the stored bytes (200). Forwarded to the `rfc_sink` when configured. | `201 {recipient, request_id, rfc: {request, signature}, account_refs: [account_ref], forwarded}` |
| `GET /rfc` | recipient | Requests this recipient sent. | `{requests: [{request_id, parent_request_id, issued_at, expires_at, scope: {txn_ids} or {period}, question_ids, source: case | follow_up | custom, answered_by: [package_id]}]}` |
| `GET /rfc/{request_id}` | recipient | One sent request. | `{request_id, rfc, account_refs}`; `404 not_found` |
| `POST /deliver` body = signed Context Package JSON | recipient | 1. Strict JSON object with `package_id` (`400 invalid_package`). 2. `digest = canon.digest(raw)`. Same `package_id` already in this inbox: same digest returns the stored receipt and status (200 or 422 as first time); different digest `409 package_id_reused`, nothing stored. 3. `body.audience != recipient`: `409 audience_mismatch`. 4. `body.request_id` not issued by this recipient: `409 unknown_request`. 5. `alibi.api.verify_package(raw, expected_audience=recipient, registry, now=clock, expected_subject=rfc.subject_ref, expected_request_id=rfc.request_id)`. Receipt bound to `package_id` and digest: `Receipt(receipt_id = make_id("rcp", recipient, package_id, digest), package_id, package_sha256 = digest, recipient, received_at = clock, accepted, reasons)`. Rejections (steps 3 to 5) store an inbox entry with status `rejected`, the receipt and reasons, no bytes. Audit every outcome. | accepted: `200 {status: "verified", receipt, authentication, sources, claims}`; rejected: `422 package_rejected` (or `409 audience_mismatch` / `409 unknown_request`) with `{code, reason, receipt, rejection: {authentication, errors}}` |
| `GET /inbox` | recipient | | `{recipient, as_of, packages: [{package_id, request_id, received_at, status: verified | rejected, package_sha256, receipt_id, expires_at?, disposition: decision or null, reasons? (rejected only)}], requests_sent: [...]}` |
| `GET /packages/{package_id}` | recipient | Only this recipient's inbox. Unknown or another recipient's: `404 not_found`, reason "no package {id} in this inbox" either way. | verified: `{status: "verified", receipt, authentication, sources, claims, package: {package_id, issued_at, body_sha256, body: {request_id, parent_request_id, audience, case_ref, scope, questions, claims, expires_at, attachments: [metadata only: attachment_id, ref, form, source_trust, issuer_id, generalised_fields]}}, dispositions: [...], follow_ups: [request_id]}`; rejected: `{status: "rejected", receipt, rejection: {reasons}}` and no other key |
| `GET /packages/{package_id}/attachments` | recipient | Full attachments with content and issuer signatures. | verified: `{package_id, attachments: [...]}`; rejected: `409 package_rejected`; `404 not_found` |
| `GET /packages/{package_id}/raw` | recipient | The exact stored package JSON, for the independent verifier. | verified only; else as above |
| `POST /packages/{package_id}/disposition` body `{decision, reason, question_ids?, requested_evidence?, questions?, txn_ids?, period?, request_id?}` | recipient | Pinned Disposition record: `{disposition_id, package_id, package_digest, request_id, recipient, analyst, decision, reason, question_ids, requested_evidence, decided_at, supersedes}`. `decision` one of `clear`, `escalate`, `request_more` and a non-empty `reason`, else `400 invalid_disposition`. Rejected package: `409 package_rejected`. A terminal (`clear`/`escalate`) disposition already recorded: `409 disposition_final`. `supersedes` names the previous disposition of this package (after a `request_more`). `request_more` creates a follow-up RFC: `request_id` from the body (unused) or `<parent>-f<n>`, `parent_request_id` = the package's request, scope inherited from the parent (the body may narrow it: a subset of `txn_ids` or a period inside the parent's; wider is `409 scope_wider_than_parent`), questions from `questions` or one per `requested_evidence` entry (`q.followup_<n>`), at least one, `issued_at` = clock, `expires_at` = clock + 7 days, signed, stored in requests sent, forwarded to the `rfc_sink`. | `200 {disposition, follow_up: null or {request_id, rfc: {request, signature}, account_refs, forwarded}}` |
| `GET /packages/{package_id}/dispositions` | recipient | | `{dispositions: [...]}` |
| `GET /audit` | recipient | This recipient's audit entries. | `{entries: [{at, actor, action, outcome, package_id?, request_id?, detail}]}` |
| `GET /feed/transactions?cursor=&limit=` | feed token | The read-only feed (`alibi/connectors/bank_feed.py`): visible bank rows in arrival order from the cursor. Cursor is opaque text `c:<n>` (rows already served); `limit` default 100, max 500. | `{connection_id, account_ref, as_of, source_version: "mock_bank_feed.v1", transactions: [case rows], next_cursor, has_more}` |
| `GET /feed/transactions/{txn_id}` | feed token | Latest visible row for one transaction. | `{as_of, source_version, transaction: row}`; `404 not_found` |

Bank details settled during the build:

- Before a seed every recipient route (audit included) is `404 not_found`; admin routes work.
- `POST /rfc` only issues case RFCs whose `issued_at <= as_of` (`404 unknown_request` otherwise), and
  refuses a custom request whose `issued_at` is after the clock (`400 invalid_request`). So at the
  default clock (2026-11-02T09:00Z) `bank_b`'s `rfc-0001-02` (issued 2026-11-04T14:00Z) needs
  `POST /admin/advance?as_of=2026-11-04T14:00:00Z` (or seeding there) first.
- A custom request with `parent_request_id` must name one of this recipient's own sent requests
  and obeys the follow-up scope rule. A custom or body-supplied request id must be an Ident, must
  not be any case RFC id and must be unused by this recipient (`400 invalid_request` /
  `invalid_disposition`), so one bank cannot squat the other's id in the passport.
- `forwarded` outcomes are `accepted`, `refused` or `error` (a raising sink is reported as
  `error` with the exception type; the request is still stored).
- Rejected inbox entries keep only the receipt, `{authentication, errors}` and digests; ids
  quoted in reasons and audit are quoted only when they pass the core `Ident` pattern.
- Disposition bounds: `reason` at most 300 characters, `question_ids` Idents, `requested_evidence`
  entries at most 1000 characters (`400 invalid_disposition`).
- Non-JCS numbers or deeply nested JSON on `/deliver` are `400 invalid_package`.
- The disposition's `package_digest` is the bare SHA-256 hex of the canonical package (the same
  value as the receipt's `package_sha256`).
- A custom `POST /rfc` request body is shape-checked before mapping: `txn_ids` a list of strings,
  `period` an object, `questions` a list (`400 invalid_request`).

## Bank feed client (`alibi/connectors/bank_feed.py`)

- `FeedServer(clock, account_ref, connection_id)` (positional or keyword) with pure methods
  `page(cursor, limit) -> dict` and `latest(txn_id) -> dict | None`; the bank service wraps them in
  the two routes above. A cursor beyond the rows ever served, a non-ASCII-digit cursor or a limit
  outside 1..500 raises `Refusal("invalid_query")`.
- Transports: an object with `page(cursor, limit) -> dict` and `txn(txn_id) -> {as_of,
  source_version, transaction: row} | None` (the route body, because `refresh` needs the feed's
  `as_of` for provenance): `LocalFeed(server)` and `HttpFeed(base_url, token, client=None)` (httpx
  or a TestClient-like `.get(path, params=, headers=)`). Non-2xx answers other than a 404 on the
  txn route, non-JSON bodies and wrong shapes raise `FeedError` naming the status or step only.
- `FeedClient.sync` validates every page before writing (`FeedError` for a page that is not a
  page or reports `has_more` without advancing the cursor), audits a row it cannot map as a
  refused batch, and leaves the cursor untouched on any failure. The cursor is positional per
  connection: re-seeding the bank to another case under the same connection id and store would
  skip rows, so use a fresh store or connection id per case.
- `FeedClient(transport, store, *, subject_ref, connection_id)`:
  `sync(now=None) -> {"pages", "inserted", "duplicate", "corrected", "stale", "cursor"}` reads from
  the store's cursor (`store.get_cursor("bank_feed", connection_id)`), maps rows with
  `map_bank_row` (provenance `retrieved_at` = the feed's `as_of`, `cursor` = the page's
  `next_cursor`) and calls `alibi.api.ingest_bank_txns(..., cursor=next_cursor)`, which commits
  the batch and the cursor together. A refused batch leaves the cursor where it was and raises
  `IngestError`. `refresh(txn_id) -> dict | None` returns the latest row as `BankTxn` JSON for the
  gateway's `txn_refresh` (provenance connection = this connection).

## Self-supplied import (`alibi/connectors/self_supplied.py`)

- `import_case_imports(store, clock, *, since=None) -> list[PutResult]`: the visible imports,
  mapped with `map_import`, through `alibi.api.import_self_supplied` at each `received_at`.
- `import_record(store, record, *, subject_ref, now) -> PutResult`: one holder-supplied record,
  case-shaped (mapped) or already core-shaped (passed through). Whatever it claims, the stored
  record is `self_supplied` under a `self_` id.

## Plug (`alibi/connectors/plug.py`, `alibi/connectors/clients.py`)

- `plug.webhook_sink(store, registry, *, subject_ref)` and `plug.rfc_sink(store, registry)`: the
  passport's merchant webhook and RFC intake as in-process callables (`{"status": inserted |
  duplicate | corrected | stale | refused | error}` and `{"status": accepted | refused | error}`).
- `clients.MerchantClient(client)` (an `httpx.Client` with `base_url`, or a `TestClient`):
  `settlement_lookup(payment_ref) -> {event, signature} | None` (the gateway's `SettlementLookup`,
  the highest visible revision, `extra` stripped), `lookup(gateway=None, question_id=None,
  **one_key) -> [envelope]` (with a gateway, one `tool_call` trace event by actor `connector`,
  tool `merchant_lookup`, with the query and a summary of ids, revisions, kinds and statuses),
  `seed`, `advance`, `retry`, `correct`, `deliveries`, `state`.
- `clients.BankClient(client, token, recipient=)`: `issue_rfc`, `requests_sent`, `deliver(package) ->
  (status, body)` (never raises on a rejection), `inbox`, `package`, `attachments`, `disposition`,
  `audit`, `seed`, `advance`. Refusals raise `ClientError(status, code, reason)`.
- `clients.connect(merchant, feed) -> {"settlement_lookup", "txn_refresh"}` to splat into
  `alibi.api.run_investigation(store, request_id, registry=registry, now=now, **connect(m, f))`.
- `clients.bootstrap(store, registry, *, case, merchant, banks, feed=None, as_of=None, sink=None)`
  seeds a passport from the services: merchant seed (pushes, or a replay of `GET /admin/deliveries`
  into the sink when the merchant has none), bank seeds, feed sync, visible imports, and each
  bank's visible RFCs issued and received. Idempotent.
- `plug` re-exports the client names lazily, so `from alibi.connectors.plug import connect` works.
- Client rules: lookup key values must match the core `Ident` pattern (`ValueError` otherwise);
  `lookup(record_id=)` returns `[]` only on a `404 not_found`, any other 404 (an unseeded merchant's
  `unknown_case`) raises; a 2xx answer that is not the contract shape raises
  `ClientError(status, "malformed_response", reason)` (a client-side code, not an HTTP refusal).
  Through the gateway these surface as `Unavailable`, never a crash.
- `bootstrap` replays `GET /admin/deliveries` into the sink when any push reports an outcome other
  than `inserted`, `duplicate`, `corrected`, `stale` or `refused`, or when a pushed revision is
  missing from this store (the merchant pushed into another passport); the summary carries
  `replayed_from: pushes | webhook_log`. With no `as_of` it keeps the merchant's current clock when
  the merchant already serves the case; an explicit `as_of` always wins.

## Demo runner (`alibi/demo_services.py`)

`uv run --frozen python -m alibi.demo_services --case 1 --as-of <utc> --timeout 10 [--merchant-port 8101] [--bank-port 8102] [--passport-url URL] [--keys DIR] [--state DIR]`
starts both apps with one keystore, seeds both with the case at `as_of`, prints the URL line (with
the bound ports) only once both servers listen, plus the tokens file path (never the tokens), serves
until `--timeout` seconds pass or Ctrl-C (checked every 0.5 s), exits 0. A server that cannot bind is
reported within 15 s as `{"error": "server did not start", "servers": [...]}` then
`{"stopped": true, "failed": [...]}` with exit 1. A bad `--as-of` or case exits with a one-line
message before any key or token file is created.

## Refusal codes used (to pin in `shared/CONTRACTS.md`)

`unauthenticated`, `not_found`, `unknown_case`, `unknown_event`, `unknown_record`, `unknown_request`,
`invalid_request`, `invalid_time`, `invalid_query`, `invalid_correction`, `invalid_disposition`,
`invalid_package`, `method_not_allowed`, `clock_backwards`, `request_expired`, `audience_mismatch`,
`package_id_reused`, `package_rejected`, `disposition_final`, `scope_wider_than_parent`,
`sink_unavailable`. Unknown paths are `404 not_found` ("no such route"), wrong methods
`405 method_not_allowed`, and a route's plain `ValueError` or `TypeError` is `400 invalid_request`
naming only the exception type.
