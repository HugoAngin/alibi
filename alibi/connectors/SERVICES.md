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
| `GET /admin/lookups` | Served lookups, for eval's `lookups` count. | `{lookups: [{at, route, query, record_ids}]}` |
| `GET /admin/state` | | `{case_number, as_of, pushed, corrections, lookups_served}` |

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

## Bank feed client (`alibi/connectors/bank_feed.py`)

- `FeedServer(clock, account_ref, connection_id)` with pure methods `page(cursor, limit) -> dict` and
  `latest(txn_id) -> dict | None`; the bank service wraps them in the two routes above.
- Transports: an object with `page(cursor, limit) -> dict` and `txn(txn_id) -> dict | None`:
  `LocalFeed(server)` and `HttpFeed(base_url, token, client=None)` (httpx or a TestClient-like
  `.get(path, params=, headers=)`).
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

## Plug (`alibi/connectors/plug.py`)

- `webhook_sink(store, registry, *, subject_ref)` and `rfc_sink(store, registry)` (phase 1).
- Phase 2: `MerchantClient(transport)` with `settlement_lookup(payment_ref) -> {event, signature} | None`
  (the gateway's `SettlementLookup`) and `lookup(gateway=None, **one_key) -> [envelope]` (logs a
  `TraceEvent` via `gateway.log_step` when a gateway is given); `connect(...)` returning the
  `settlement_lookup` / `txn_refresh` kwargs for `alibi.api.run_investigation`; `bootstrap(...)`
  that seeds a passport from the running services (merchant seed, feed sync, imports, RFC issue
  and intake).

## Demo runner (`alibi/demo_services.py`)

`uv run --frozen python -m alibi.demo_services --case 1 --as-of <utc> --timeout 10 [--merchant-port 8101] [--bank-port 8102] [--passport-url URL] [--keys DIR] [--state DIR]`
starts both apps with one keystore, seeds both with the case at `as_of`, prints the URLs and the
tokens file path (never the tokens), serves until `--timeout` seconds pass or Ctrl-C, exits 0.

## Refusal codes used (to pin in `shared/CONTRACTS.md`)

`unauthenticated`, `not_found`, `unknown_case`, `unknown_event`, `unknown_record`, `unknown_request`,
`invalid_request`, `invalid_time`, `invalid_query`, `invalid_correction`, `invalid_disposition`,
`invalid_package`, `clock_backwards`, `request_expired`, `audience_mismatch`, `package_id_reused`,
`package_rejected`, `disposition_final`, `scope_wider_than_parent`, `sink_unavailable`.
