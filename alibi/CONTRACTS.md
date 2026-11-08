# Alibi public API (`alibi.api`)

Teammates import the passport functions from `agents.lead.api`, as RULES.md asks, or from
`alibi.api`. Both expose the same names, and `agents.lead.api.__all__` equals
`alibi.api.__all__`. This file documents each public function: its signature, what it does,
what it raises, and a JSON example.

Every example below was produced by running this build on the committed fixtures
(`alibi/fixtures/maya_supported.json` unless another variant is named) with a fixed clock of
`2026-11-07T12:00:00Z`. No amount is typed by hand, so every amount matches the fixtures. The
examples used a throwaway seeded demo keystore: key ids match yours, but signatures and public
keys differ. Evidence refs are written as `type:id@revision` keys to keep the examples short. The
API returns them as objects: `{"ref_type": "record", "ref_id": "po_5001", "revision": 1}`.

## What is built, and what is not

Built in this session (S1, foundation): the encrypted passport store, signed merchant
ingestion, self-supplied import, the bank feed, authenticated RFCs, the rules-based
investigation (`mode="rules"`) through the scoped tool gateway, the typed privacy policy, the
holder review, exact approval, signing, delivery records, and recipient-side verification.

**Not built yet** (future sessions). Nothing below exists, and the docs make no claim that it
works:

- `mode="agents"` and `mode="single_call"` investigations (the Gemini Matcher, Detective,
  Challenger and Curator, and the one-call baseline). `run_investigation` raises
  `NotImplementedError` for them.
- The mock merchant and bank HTTP services. Connectors are plain callables for now
  (`settlement_lookup`, `txn_refresh`), and `load_fixture` stands in for the webhook and feed.
- The passport dashboard and the bank analyst console.
- The PDF rendering of a package.

## Quick start

```python
from datetime import datetime, timezone
from agents.lead.api import (open_store, default_keystore, load_fixture, run_investigation,
                             build_review, approve, sign_and_package, verify_package)

now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)   # before the fixture RFCs expire
store = open_store()                     # data/alibi/passport.db
keys = default_keystore()                # data/alibi/keys
fx = load_fixture(store, "supported", keystore=keys, now=now)
run_investigation(store, "rfc_a_001", audience="bank_a",   # a request is (bank, request_id)
                  settlement_lookup=fx["settlement_lookup"], registry=keys.registry(), now=now)
draft = build_review(store, "rfc_a_001", audience="bank_a", now=now)   # body + private review
approval = approve(store, draft.draft_id, holder_id="holder_maya", now=now)
package = sign_and_package(store, approval.approval_id, keystore=keys, now=now)
result = verify_package(package.to_json_dict(), expected_audience="bank_a",
                        registry=keys.registry(), now=now)
store.close()
```

Pass `now` while the fixture RFCs are still open (RFC A expires at `2026-11-11T09:00:00Z`, RFC B at
`2026-11-12T09:00:00Z`): `receive_rfc` refuses an expired request, and `sign_and_package` refuses
to answer one. `load_fixture` defaults to this fixture clock and returns it as `fx["now"]`, so
pass `now=fx["now"]` to every later step.

The flow can be re-run. Running the quick start again on the same passport with the same `now`
(after a restart, say) returns the same draft (now `packaged`), the same approval (now
`consumed`) and the identical package, and records no second delivery.

Request ids are chosen by banks, so a request is identified by `(audience, request_id)`. Two
banks may use the same id without blocking or seeing each other. Holder calls therefore always
name the bank: `run_investigation` and `build_review` take `audience` with a request_id.
`load_fixture` returns the pairs as `fx["requests"]`. A bare request_id still resolves while only
one bank has used it (the app, the demo cases and teammates' acceptance tests call bare ids);
once several banks have used it the call raises `AmbiguousRequest`, and the store resolves a bare
id to the request received first. Asking for `("bank_a", id)` gets `NotFound` when bank_a never
sent it, never the other bank's request.

## Conventions

- **Money** is integer pence with an ISO 4217 currency code. Floats, booleans and numeric
  strings are rejected. Merchant amounts are non-negative magnitudes, and the direction comes
  from `kind` (an order adds, a refund or fee subtracts). Bank amounts are signed from the
  holder's view (a credit is positive). The canonical fixture reconciles like this:

```json
{
  "currency": "GBP",
  "sales_pence": 240000,
  "refunds_pence": 18000,
  "fees_pence": 6000,
  "net_pence": 216000,
  "credit_pence": 216000,
  "difference_pence": 0,
  "balanced": true
}
```

- **Times** are timezone-aware UTC, written as ISO-8601 with a trailing `Z`. Every function that
  depends on the clock takes `now` (default: the current UTC time). The exception is
  `load_fixture`, which defaults to the fixture clock `2026-11-07T12:00:00Z`. Pass `now` in
  tests and replays.
- **Periods** are half-open, `[from, to)`: `from` is inside, `to` is not. RFC B asks about
  October 2026 like this, so a record at `2026-11-01T00:00:00Z` is outside it:

```json
{
  "from": "2026-10-01T00:00:00Z",
  "to": "2026-11-01T00:00:00Z"
}
```

- **Ids** are deterministic where the core derives them (`canon.make_id(prefix, *parts)`), so a
  replay produces the same ids.
- The store comes first in every function. Signing and ingest take an optional `keystore` or
  `registry`, defaulting to the demo keystore under `data/alibi/keys`. `verify_package` only
  reads the pinned `data/alibi/keys/trust_registry.json`. Tests always pass their own.
- **Audit.** Every step appends an `AuditEvent` (actor, time, target id, action, outcome, and a
  detail holding ids and reasons only): the ingest functions and `receive_rfc` (written by the
  core), `run_investigation`, `record_policy`, `build_review`, `approve` and
  `sign_and_package`. Refusals are audited too: `IngestError` (by the core), `PolicyError` from
  `record_policy` (counts only, never the rule texts), `PackageError` from `build_review`, and
  `ApprovalError` from `approve` and `sign_and_package`. A retry that returns an existing
  approval or package is audited as `duplicate`. Argument errors (`ValueError`, `TypeError`,
  `NotFound`, `NotImplementedError`) are raised with no audit event. Read them with
  `store.list_audit()`.

## Three separate enums

`SourceTrust`, `LinkState` and `ClaimStatus` are separate on purpose. A credit can be linked to
a payout, and every signature can verify, while the source-of-funds claim stays unresolved.

```json
{
  "SourceTrust": [
    "issuer_verified",
    "bank_observed",
    "self_supplied"
  ],
  "LinkState": [
    "unmatched",
    "ambiguous",
    "linked"
  ],
  "ClaimStatus": [
    "supported",
    "contradicted",
    "unresolved"
  ]
}
```

- `SourceTrust` says where a source came from and how it was authenticated. It is not a truth
  label. `issuer_verified` means an issuer signature checked against the pinned registry,
  `bank_observed` means read from the authenticated bank feed, and `self_supplied` means
  imported by the holder with no issuer attestation.
- `LinkState` says whether a bank transaction is matched to a source record.
- `ClaimStatus` says whether evidence supports a stated claim. `contradicted` means evidence
  conflicts with the claim. `unresolved` means the needed evidence is unavailable.

## Signing: domains and the envelope

Ed25519 over canonical JSON (RFC 8785). The signed bytes are `domain` + a NUL byte + JCS(payload),
so a signature made in one domain never verifies in another. A key verifies only in the domain
its pinned purpose allows, and only for the issuer it is pinned to.

| Domain | Key purpose | Demo keys (issuer) |
| --- | --- | --- |
| `alibi:record:v1` | record_signing | `merchant_demo` (threadline) |
| `alibi:package:v1` | package_signing | `alibi_demo` (alibi) |
| `alibi:request:v1` | request_signing | `bank_a` (bank_a), `bank_b` (bank_b) |

The envelope travels beside the signed object (here, the signature of the payout event below):

```json
{
  "alg": "Ed25519",
  "kid": "merchant_demo",
  "domain": "alibi:record:v1",
  "sig": "892cdb68b0c5973cf6b837ee7efad3aad40d44d97d7f866dfa6d24de84d1f520f7a6c90862a4c38380bd8ac74665fb0ad55d2c617d38bd0327fe664698bc4800"
}
```

Signatures are always checked over the raw JSON object that was received, never over a
re-serialised model.

## Trust registry format

`default_keystore()` writes `trust_registry.json` beside the keys. It maps each key id to its
public key (raw 32 bytes, hex), purpose and issuer. Verifiers pin to it, and it holds no
private material:

```json
{
  "alibi_demo": {
    "alg": "Ed25519",
    "public_key": "5a984541fcbdbd851aaf4a5795cfd3a289567bf211fb7ff7d0f84c0d8ade3d8b",
    "purpose": "package_signing",
    "issuer_id": "alibi"
  },
  "bank_a": {
    "alg": "Ed25519",
    "public_key": "4fc03fe21392e117ddae28b05a5283eb4e5b37df2a99ee185524c9cebe18e987",
    "purpose": "request_signing",
    "issuer_id": "bank_a"
  },
  "bank_b": {
    "alg": "Ed25519",
    "public_key": "2b8f0a1b31912c784fe4ead369fe580548d7f1c5d8f15b18e99fcbdb36415180",
    "purpose": "request_signing",
    "issuer_id": "bank_b"
  },
  "merchant_demo": {
    "alg": "Ed25519",
    "public_key": "7149fe8ea2af8954d5c01920313d03d0e1f10c34fc361a1fa2ba64eb8a2c9fb9",
    "purpose": "record_signing",
    "issuer_id": "threadline"
  }
}
```

Load one with `TrustRegistry.load(path)`, or take it from a keystore with `keystore.registry()`.
`verify_package(registry=None)` reads `data/alibi/keys/trust_registry.json` read-only. It never
creates keys and never rewrites the file, so a recipient can pin the registry alone.

## Self-supplied evidence: the `self_` namespace

Evidence the holder imports goes through `import_self_supplied` and is always stored at
`self_supplied` trust, whatever the payload claims. Its record ids always start with `self_`
(`rcpt_4002` becomes `self_rcpt_4002`), and issuer evidence may never use that prefix. So an
import can never take over an issuer's record id. Signatures and trust claims in an import are
dropped, and the audit event names them:

```json
{
  "given": {
    "record_id": "rcpt_4002",
    "source_trust": "issuer_verified"
  },
  "stored": {
    "record_id": "self_rcpt_4002",
    "source_trust": "self_supplied"
  },
  "audit_detail": "inserted self_rcpt_4002 revision 1; trust self_supplied; stripped trust claims: source_trust; record id moved into the self_ namespace"
}
```

## Where files live

| What | Default location | Notes |
| --- | --- | --- |
| Passport database | `data/alibi/passport.db` | SQLite. Every payload column is Fernet-encrypted canonical JSON. |
| Master key | `ALIBI_MASTER_KEY`, else `data/alibi/master.key` | A Fernet key. The file sits beside the database and is created with mode 0600 on first use. It never enters the database. |
| Demo signing keys | `data/alibi/keys/<kid>.pem` | PKCS#8 PEM, mode 0600, created on first use by `default_keystore()`. |
| Trust registry | `data/alibi/keys/trust_registry.json` | Public keys only. `default_keystore()` rewrites it; `verify_package` only reads it. |

`data/` is git-ignored. Never print or log `ALIBI_MASTER_KEY`, `GEMINI_API_KEY` or a private
key. To start the demo from an empty passport, delete `data/alibi/passport.db` and
`data/alibi/master.key` together, then load a fixture again. Keep the keys if banks have
pinned the registry.

## Errors

Every error message is safe to show: it names ids, fields and reasons, never payload contents.

| Error | Raised when |
| --- | --- |
| `IngestError` (`.reason`) | An inbound merchant event, import, bank batch or RFC is refused: bad schema, missing or invalid signature, wrong subject, connection or recipient, an expired request, or an out-of-scope reference. A refused audit event is written. `load_fixture` also raises it, before loading or auditing anything, for a `now` at or past a fixture RFC's expiry. |
| `ApprovalError` (`.problems`) | An approval cannot be granted or used. `.problems` lists every reason. A refused audit event is written. |
| `PolicyError` | A rule or policy is malformed. `record_policy` writes a refused audit event. |
| `RuleClarificationError` (`.interpretations`) | A subclass of `PolicyError`. A plain-English rule could not be resolved, so nothing was saved. The refused audit event holds counts only, never the rule texts. |
| `PackageError` | A draft cannot be built: no investigation yet, or the latest one is out of date. A refused audit event is written. |
| `StoreError` | The base store error. Its subclasses are `NotFound` (no such id or revision), `IdempotencyConflict` (same key, different bytes), `RevisionConflict` (same revision, different content), `NamespaceError` (a record id in the wrong namespace) and `AmbiguousRequest` (a bare request_id that several banks used; pass `audience`). A passport created before store schema version 3 is refused with a `StoreError`; load the fixtures again into a new one. |
| `SignatureError` (`.reason`) | A key, envelope or signature fails a check. |
| `GatewayError` | A gateway call fails. Its subclasses are `ScopeError`, `BudgetExceeded`, `Unavailable` and `NotFoundError`. The rules investigation turns these into typed unresolved reasons, so they reach callers only when they use a `Gateway` directly. |
| `NotImplementedError` | `run_investigation` was given a mode that is not built. |
| `ValueError` | `now` is not a timezone-aware datetime. `verify_package` returns a rejection instead. |
| `AmbiguousRequest` | A holder call (`run_investigation`, `build_review`) was given a bare request_id that several banks have used. Request ids are per bank, so name the bank with `audience`. |

## Functions

### `open_store`

```python
open_store(path=None, *, master_key=None) -> Store
```

Opens or creates the encrypted passport database (default `data/alibi/passport.db`). The
master key is `master_key`, else `ALIBI_MASTER_KEY`, else `master.key` beside the database. A
wrong key fails when the store opens. Use the store as a context manager or call `close()`.
Its read methods (`list_sources`, `get_draft`, `list_deliveries`, `list_audit`, and so on) are
documented in `alibi/core/store.py`.

Raises `StoreError` (bad key, unreadable file).

### `default_keystore`

```python
default_keystore(key_dir=None) -> DemoKeystore
```

Returns the demo keystore (`data/alibi/keys` by default), creating any missing key and
rewriting `trust_registry.json`. It holds the keys `merchant_demo`, `alibi_demo`, `bank_a` and
`bank_b`. The registry it writes is shown under [Trust registry format](#trust-registry-format).

### `load_fixture`

```python
load_fixture(store, variant="supported", *, keystore=None, connection_id="conn_bank_a_maya",
             include_rfcs=True, now=None) -> dict
```

Loads a committed fixture (`supported`, `contradiction`, `pending_correction`, `ambiguous` or
`retail_purchase`) through the real ingest paths. Merchant events are signed with the
keystore's merchant key and ingested by webhook. Bank txns go through the feed for
`connection_id`. Self-supplied items go through the import. RFCs are signed with the bank keys
and go through `receive_rfc` (RFC A names `txn_ids`, so Bank A's account is passed as its
`account_refs`). A `connection_id` other than the fixture's is written into each txn's
provenance, so the feed is delivered by that connection. Lookup-only settlements stay outside the passport until an
investigation retrieves them with `settlement_lookup`. Replaying is idempotent: every put
returns `duplicate` and no row is added. `requests` lists each loaded request as
`{"request_id", "audience"}`, which is what the holder calls need. `request_ids` lists only the
ids.

`now` defaults to the fixture clock `2026-11-07T12:00:00Z`, not the wall clock, because the
fixture RFCs expire (RFC A at `2026-11-11T09:00:00Z`). The result's `now` is the clock used:
pass it to the later steps, since `sign_and_package` refuses to answer an expired request. A
`now` at or past an RFC's expiry raises `IngestError` before anything is loaded or audited:

```json
{
  "reason": "fixture request(s) rfc_a_001 expired by 2026-11-11T09:00:00Z; nothing was loaded. Pass a now before 2026-11-11T09:00:00Z, such as the fixture clock 2026-11-07T12:00:00Z"
}
```

Returns (callables, the gold block and the clock shown as placeholders):

```json
{
  "variant": "supported",
  "subject_ref": "subj_maya",
  "record_ids": [
    "ord_1001",
    "ord_1002",
    "ord_1003",
    "rfd_2001",
    "ord_1004",
    "fee_3001",
    "msg_7001",
    "po_5001"
  ],
  "txn_ids": [
    "txn_9001"
  ],
  "request_ids": [
    "rfc_a_001",
    "rfc_b_001"
  ],
  "requests": [
    {
      "request_id": "rfc_a_001",
      "audience": "bank_a"
    },
    {
      "request_id": "rfc_b_001",
      "audience": "bank_b"
    }
  ],
  "settlement_lookup": "<callable: payment_ref -> signed settlement event, or None>",
  "txn_refresh": "<callable: txn_id -> latest bank txn JSON, or None>",
  "corrections": [],
  "statuses": {
    "ord_1001": "inserted",
    "ord_1002": "inserted",
    "ord_1003": "inserted",
    "rfd_2001": "inserted",
    "ord_1004": "inserted",
    "fee_3001": "inserted",
    "msg_7001": "inserted",
    "po_5001": "inserted",
    "txn_9001": "inserted"
  },
  "expected": "<the fixture's expected block (development gold)>",
  "now": "<datetime 2026-11-07T12:00:00Z: the clock used>"
}
```

`corrections` holds signed payloads ready for `ingest_merchant_event` (the
`pending_correction` variant has one). A second load of the same fixture returns:

```json
{
  "statuses": {
    "ord_1001": "duplicate",
    "ord_1002": "duplicate",
    "ord_1003": "duplicate",
    "rfd_2001": "duplicate",
    "ord_1004": "duplicate",
    "fee_3001": "duplicate",
    "msg_7001": "duplicate",
    "po_5001": "duplicate",
    "txn_9001": "duplicate"
  }
}
```

Raises `IngestError` if a payload is refused.

### `ingest_merchant_event`

```python
ingest_merchant_event(store, payload, *, registry=None, connector="merchant_webhook",
                      subject_ref=None, now=None) -> PutResult
```

Verifies and stores one issuer-signed merchant event. The payload is
`{"event": ..., "signature": ...}` as a dict, JSON text or bytes. The signature is mandatory.
It is checked over the raw `event` object in domain `alibi:record:v1` with a key pinned for
`event.issuer_id`. The record is stored as `issuer_verified`. With `subject_ref`, an event about
any other subject is refused. Idempotency is by `(connector, issuer_id, event_id)` plus the
payload digest. A higher revision of a record is a correction: it becomes the active revision,
the old one is kept, and the links, claims, pending or approved drafts and pending approvals
that depended on it are invalidated. A lower revision is kept as `stale` history.

The signed wire shape (the payout `po_5001` from the fixture):

```json
{
  "event": {
    "amount_pence": 216000,
    "currency": "GBP",
    "event_id": "evt_po_5001_r1",
    "issuer_id": "threadline",
    "kind": "payout",
    "occurred_at": "2026-11-02T08:30:00Z",
    "payment_ref": "PO-5001",
    "record_id": "po_5001",
    "related_record_ids": [
      "ord_1001",
      "ord_1002",
      "ord_1003",
      "ord_1004",
      "rfd_2001",
      "fee_3001"
    ],
    "revision": 1,
    "schema_version": "alibi.merchant_event.v1",
    "status": "initiated",
    "subject_ref": "subj_maya"
  },
  "signature": {
    "alg": "Ed25519",
    "kid": "merchant_demo",
    "domain": "alibi:record:v1",
    "sig": "892cdb68b0c5973cf6b837ee7efad3aad40d44d97d7f866dfa6d24de84d1f520f7a6c90862a4c38380bd8ac74665fb0ad55d2c617d38bd0327fe664698bc4800"
  }
}
```

The `PutResult` (`signed_body`, the exact event above, is left out here):

```json
{
  "status": "inserted",
  "active": true,
  "invalidated": {
    "links": [],
    "claims": [],
    "drafts": [],
    "approvals": []
  },
  "record": {
    "event_id": "evt_po_5001_r1",
    "record_id": "po_5001",
    "revision": 1,
    "issuer_id": "threadline",
    "subject_ref": "subj_maya",
    "kind": "payout",
    "occurred_at": "2026-11-02T08:30:00Z",
    "amount_pence": 216000,
    "currency": "GBP",
    "payment_ref": "PO-5001",
    "status": "initiated",
    "related_record_ids": [
      "ord_1001",
      "ord_1002",
      "ord_1003",
      "ord_1004",
      "rfd_2001",
      "fee_3001"
    ],
    "schema_version": "alibi.evidence.v1",
    "source_trust": "issuer_verified",
    "connector": "merchant_webhook",
    "received_at": "2026-11-07T12:00:00Z",
    "payload_sha256": "279a0932ba34c1a842641496f19997629f8564b1848668f1f0d09a8634166914",
    "issuer_signature": {
      "alg": "Ed25519",
      "kid": "merchant_demo",
      "domain": "alibi:record:v1",
      "sig": "892cdb68b0c5973cf6b837ee7efad3aad40d44d97d7f866dfa6d24de84d1f520f7a6c90862a4c38380bd8ac74665fb0ad55d2c617d38bd0327fe664698bc4800"
    }
  }
}
```

Sending the same bytes again returns:

```json
{
  "status": "duplicate",
  "active": true
}
```

A correction from the `pending_correction` variant (`fx["corrections"][0]`, its event shown)
arriving after Maya approved a draft:

```json
{
  "amount_pence": 216000,
  "correction_reason": "Status restated: the transfer had not completed and is still pending with the receiving bank.",
  "corrects_revision": 1,
  "currency": "GBP",
  "event_id": "evt_stl_88001_r2",
  "issuer_id": "threadline",
  "kind": "settlement",
  "occurred_at": "2026-11-04T09:00:00Z",
  "payment_ref": "PO-5001",
  "record_id": "stl_88001",
  "related_record_ids": [],
  "revision": 2,
  "schema_version": "alibi.merchant_event.v1",
  "settlement_ref": "STL-88001",
  "status": "pending",
  "subject_ref": "subj_maya"
}
```

```json
{
  "status": "corrected",
  "active": true,
  "record": {
    "record_id": "stl_88001",
    "revision": 2
  },
  "invalidated": {
    "links": [],
    "claims": [
      "clm_cc8751048e05f4a468325189"
    ],
    "drafts": [
      "drf_8522ad872123e0d3d32c1743"
    ],
    "approvals": [
      "apr_2e338a61dd6525420fb1ff74"
    ]
  }
}
```

Raises `IngestError`.

### `ingest_bank_txns`

```python
ingest_bank_txns(store, txns, *, subject_ref, connection_id, cursor=None,
                 now=None) -> list[PutResult]
```

Stores one connection's feed batch, with its cursor, in one transaction. Nothing is written
unless the whole batch is valid. Each txn is validated as `BankTxn`. A txn without
`subject_ref` gets the connection's subject, and one naming another subject is refused. Every
txn must come from `connection_id`, which must also be the connection that delivered any txn
id or account already held. Idempotency is by `(txn_id, revision)` plus content, with
provenance excluded.

The fixture credit `txn_9001`:

```json
{
  "account_ref": "acc_maya_bank_a",
  "amount_pence": 216000,
  "booked_at": "2026-11-03T10:00:00Z",
  "currency": "GBP",
  "description": "THREADLINE PAYOUT PO-5001 STL-88001",
  "provenance": {
    "connection_id": "conn_bank_a_maya",
    "connector": "bank_feed",
    "retrieved_at": "2026-11-03T10:05:00Z",
    "source_version": "bank_a_feed.v1"
  },
  "refs": [
    "PO-5001",
    "STL-88001"
  ],
  "revision": 1,
  "schema_version": "alibi.bank_txn.v1",
  "source_trust": "bank_observed",
  "subject_ref": "subj_maya",
  "txn_id": "txn_9001"
}
```

The result for that batch:

```json
[
  {
    "status": "inserted",
    "active": true,
    "txn_id": "txn_9001",
    "revision": 1
  }
]
```

Raises `IngestError`.

### `import_self_supplied`

```python
import_self_supplied(store, payload, *, subject_ref, now=None) -> PutResult
```

Imports the holder's own receipt or export at `self_supplied` trust (see
[the `self_` namespace](#self-supplied-evidence-the-self_-namespace)). No signature is needed
and none is honoured. A missing `event_id` or `record_id` is derived from the payload,
`revision` defaults to 1 and `issuer_id` to `self_supplied`. A given `issuer_id` stays as data
and never becomes trust. A `subject_ref` other than the holder's is refused.

The retail receipt from `retail_purchase.json`:

```json
{
  "amount_pence": 4599,
  "currency": "GBP",
  "event_id": "evt_self_rcpt_4001_r1",
  "issuer_id": "northgate_retail",
  "items": [
    {
      "category": "homeware",
      "description": "Rechargeable desk lamp, matte black",
      "quantity": 1,
      "unit_amount_pence": 4599
    }
  ],
  "kind": "receipt",
  "occurred_at": "2026-11-05T13:20:00Z",
  "payment_ref": "NGR-731942",
  "record_id": "self_rcpt_4001",
  "related_record_ids": [],
  "revision": 1,
  "schema_version": "alibi.merchant_event.v1",
  "status": "paid",
  "subject_ref": "subj_maya",
  "text": "Northgate Retail. Receipt NGR-731942. 1 x Rechargeable desk lamp, matte black: £45.99. Total £45.99, paid by card."
}
```

The stored record (excerpt):

```json
{
  "status": "inserted",
  "record": {
    "event_id": "evt_self_rcpt_4001_r1",
    "record_id": "self_rcpt_4001",
    "revision": 1,
    "issuer_id": "northgate_retail",
    "kind": "receipt",
    "amount_pence": 4599,
    "currency": "GBP",
    "payment_ref": "NGR-731942",
    "source_trust": "self_supplied",
    "connector": "self_supplied_import"
  }
}
```

Raises `IngestError`.

### `receive_rfc`

```python
receive_rfc(store, payload, *, authenticated_issuer, registry=None, account_refs=None,
            now=None) -> RequestForContext
```

Accepts a bank's Request for Context, as `{"request": ..., "signature": ...}` or a bare
request. The authenticated bank must be both the request's `issuer` and its `audience`, which
is the recipient binding. A signature, when present, must verify in domain `alibi:request:v1`
for that issuer. The request must not have expired, and its subject must be one the passport
holds. Every `txn_id` must belong to that subject and sit on one of `account_refs`, the bank's
own accounts. A request naming `txn_ids` without `account_refs` is refused (fail closed). A
`parent_request_id` must name an earlier request from the same issuer for the same subject,
and the follow-up must stay inside that parent's scope: the same scope kind (a
transaction-scoped parent takes no `period`, a period-scoped parent needs one), a `period`
inside the parent's, and `txn_ids` within the parent's `txn_ids` (or, for a period-scoped
parent, booked inside its period). A wider follow-up is refused with a reason that starts
`scope_wider_than_parent:`. Request ids are the issuer's own namespace: the same bank
resending a `request_id` with identical content is idempotent and with different content is
refused, while another bank may use the same id for its own request without conflict (the
store keys requests by `(issuer, request_id)`; `store.get_request(request_id, issuer=...)`
tells them apart and the bare id resolves to the request received first under it). Another bank's use of that id neither conflicts nor shows: a parent is
looked up among this bank's own requests only, and naming another bank's request is refused
exactly like naming none. Holder calls (`run_investigation`, `build_review`) pass `audience`
when several banks used an id; a bare id that several banks used raises `AmbiguousRequest`.

RFC A, signed by `bank_a`:

```json
{
  "request": {
    "audience": "bank_a",
    "expires_at": "2026-11-11T09:00:00Z",
    "issued_at": "2026-11-04T09:00:00Z",
    "issuer": "bank_a",
    "questions": [
      {
        "question_id": "q1",
        "text": "Please explain the source of the £2,160.00 credit received on 3 November 2026 (txn_9001). Which payment does it correspond to, and what records support that?"
      }
    ],
    "request_id": "rfc_a_001",
    "schema_version": "alibi.rfc.v1",
    "subject_ref": "subj_maya",
    "txn_ids": [
      "txn_9001"
    ]
  },
  "signature": {
    "alg": "Ed25519",
    "kid": "bank_a",
    "domain": "alibi:request:v1",
    "sig": "202ea6cd2b587d872cc2198f1240c750550edeeb03c2e7f8c2d96533948c83c66de5c12f80f3a87da399e88446d81ed66422c2736a6becb48976055766b8050d"
  }
}
```

The same request presented by `bank_b` is refused with this `IngestError.reason`:

```json
"the authenticated sender is not the request's issuer"
```

Raises `IngestError`.

### `run_investigation`

```python
run_investigation(store, request, *, mode="rules", audience=None, gateway=None,
                  settlement_lookup=None, txn_refresh=None, budget=12, registry=None,
                  now=None) -> InvestigationResult
```

Investigates a stored request and saves its links, claims and trace. `request` is either a
request_id together with `audience`, the bank that sent it, or the stored `RequestForContext`,
whose own audience is used.
- A bare request_id that several banks used raises `AmbiguousRequest`; pass `audience`.
- An `audience` that contradicts the model raises `StoreError`.
- A different model under the same key raises `IdempotencyConflict`.

Each claim carries `audience`. Run ids are `make_id("run", audience, request_id, "rules", n)`, so another
bank's runs never share them. Without `gateway`, a `Gateway` is built for the request's subject
and the next run id, with the given connector callables, budget and registry (the registry
verifies settlement lookups). The rules engine matches by reference, then by amount and date.
It always retrieves the matched payout's settlement, reconciles the amounts in code and takes
each claim status from explicit fields only. Matching amounts alone never support a claim.

The result for RFC A (excerpt):

```json
{
  "run_id": "run_17444153fb44e6825f5e2924",
  "request_id": "rfc_a_001",
  "mode": "rules",
  "calls_used": 5,
  "unavailable": [],
  "links": [
    {
      "link_id": "lnk_fdf12250f382e5af8ba0198d",
      "txn_ref": "txn:txn_9001@1",
      "link_state": "linked",
      "record_ref": "record:po_5001@1",
      "candidates": [
        "record:po_5001@1"
      ],
      "method": "reference",
      "state": "active"
    }
  ],
  "claims": [
    {
      "claim_id": "clm_cc8751048e05f4a468325189",
      "audience": "bank_a",
      "question_id": "q1",
      "status": "supported",
      "statement": "Credit txn_9001 of 216000 pence GBP is payout po_5001 (reference PO-5001), settled as stl_88001 before the credit was booked.",
      "evidence_refs": [
        "txn:txn_9001@1",
        "record:po_5001@1",
        "record:stl_88001@1",
        "record:ord_1001@1",
        "record:ord_1002@1",
        "record:ord_1003@1",
        "record:ord_1004@1",
        "record:rfd_2001@1",
        "record:fee_3001@1"
      ],
      "calculation": {
        "method": "net_payout",
        "currency": "GBP",
        "terms": [
          {
            "ref": "record:ord_1001@1",
            "kind": "order",
            "sign": 1,
            "amount_pence": 60000
          },
          {
            "ref": "record:ord_1002@1",
            "kind": "order",
            "sign": 1,
            "amount_pence": 72000
          },
          {
            "ref": "record:ord_1003@1",
            "kind": "order",
            "sign": 1,
            "amount_pence": 48000
          },
          {
            "ref": "record:ord_1004@1",
            "kind": "order",
            "sign": 1,
            "amount_pence": 60000
          },
          {
            "ref": "record:rfd_2001@1",
            "kind": "refund",
            "sign": -1,
            "amount_pence": 18000
          },
          {
            "ref": "record:fee_3001@1",
            "kind": "fee",
            "sign": -1,
            "amount_pence": 6000
          }
        ],
        "total_pence": 216000,
        "target_ref": "txn:txn_9001@1",
        "target_pence": 216000,
        "difference_pence": 0,
        "balanced": true,
        "timing": {
          "settlement_ref": "record:stl_88001@1",
          "credit_ref": "txn:txn_9001@1",
          "settlement_status": "settled",
          "settlement_effective_at": "2026-11-03T07:45:00Z",
          "status_observed_at": "2026-11-03T08:00:00Z",
          "booked_at": "2026-11-03T10:00:00Z",
          "ok": true,
          "reason": "Status settled, effective at or before the credit's booking time: settled in time to support the credit."
        }
      },
      "state": "active"
    }
  ],
  "trace": "<10 TraceEvent objects>"
}
```

One trace event, the settlement lookup. Its result is a summary of ids, revisions, kinds and
statuses, never amounts or text:

```json
{
  "trace_id": "trc_79943a7ef2b5153b301546d6",
  "run_id": "run_17444153fb44e6825f5e2924",
  "seq": 4,
  "at": "2026-11-07T12:00:00Z",
  "actor": "gateway",
  "step": "tool_call",
  "tool": "lookup_settlement",
  "request_id": "rfc_a_001",
  "args": {
    "payment_ref": "PO-5001"
  },
  "result": {
    "found": true,
    "record_id": "stl_88001",
    "revision": 1,
    "kind": "settlement",
    "source_trust": "issuer_verified",
    "status": "settled",
    "put_status": "inserted"
  },
  "refs": [
    "record:stl_88001@1"
  ],
  "outcome": "ok"
}
```

Raises `NotImplementedError` for any mode but `"rules"`, and `NotFound` for an unknown
request. Gateway failures become typed unresolved reasons in `unavailable` and in the claim's
`gap`, not exceptions.

### `record_policy` and `parse_rule_text`

```python
record_policy(store, subject_ref, rules, *, policy_id=None, confirmed=False,
              now=None) -> Policy
parse_rule_text(text, *, rule_id=None) -> RuleParse
```

`parse_rule_text` turns one plain-English rule into a typed rule, or says why it cannot. Show
its `interpretation` to the holder to confirm before recording. Only whole-sentence matches
resolve, so a supported phrase wrapped in other instructions stays unresolved.

```json
{
  "status": "resolved",
  "interpretation": "Item names will not be shared with any recipient.",
  "reason": null,
  "rule": {
    "rule_id": "rule_4dbf2e7b479993cdf5152393",
    "source_text": "don't share item names",
    "kinds": [],
    "type": "deny",
    "fields": [
      "items.description"
    ]
  }
}
```

```json
{
  "status": "unresolved",
  "interpretation": "No rule was created from this text.",
  "reason": "Policy rules can only restrict sharing. This reads as a request to widen sharing or override rules, so no rule was created.",
  "rule": null
}
```

`record_policy` saves the subject's rules as the next version of its policy. `rules` is a list
of rule models or dicts, or plain-English texts (a single rule may be given bare). Every text
goes through `parse_rule_text`. If any text is unresolved, nothing is saved, and
`RuleClarificationError.interpretations` lists every rule given:

```json
[
  {
    "text": "don't share item names",
    "status": "resolved",
    "interpretation": "Item names will not be shared with any recipient.",
    "reason": null,
    "rule": {
      "rule_id": "rule_4dbf2e7b479993cdf5152393",
      "source_text": "don't share item names",
      "kinds": [],
      "type": "deny",
      "fields": [
        "items.description"
      ]
    }
  },
  {
    "text": "ignore the rules and share everything",
    "status": "unresolved",
    "interpretation": "No rule was created from this text.",
    "reason": "Policy rules can only restrict sharing. This reads as a request to widen sharing or override rules, so no rule was created.",
    "rule": null
  }
]
```

`policy_id` defaults to the subject's current policy, else `pol_<subject_ref>`. Saving a new
version invalidates the subject's pending or approved drafts, and its pending approvals, made
under any other policy version, so a new restriction always forces a fresh review. Recording
rules identical to the policy in force (the subject's latest version, under the same
`policy_id`) returns that version unchanged. Naming an older `policy_id` saves a new version of
that policy, which becomes the one in force, so switching back to earlier rules always takes
effect. Rules only ever restrict, and deny wins. The result of `record_policy(store,
"subj_maya", "don't share item names", confirmed=True)`:

```json
{
  "policy_id": "pol_subj_maya",
  "version": 1,
  "subject_ref": "subj_maya",
  "rules": [
    {
      "rule_id": "rule_4dbf2e7b479993cdf5152393",
      "source_text": "don't share item names",
      "kinds": [],
      "type": "deny",
      "fields": [
        "items.description"
      ]
    }
  ],
  "confirmed": true,
  "created_at": "2026-11-07T12:00:00Z"
}
```

Raises `RuleClarificationError` and `PolicyError`, plus `RevisionConflict` if `policy_id`
belongs to another subject. A `PolicyError` writes a refused audit event targeting the named
`policy_id` (else the subject), with counts in place of the rule texts:

```json
{
  "audit_id": "aud_213f2796816f4026e20d1f8a",
  "at": "2026-11-07T12:00:00Z",
  "actor": "holder",
  "action": "record_policy",
  "target_id": "subj_maya",
  "outcome": "refused",
  "detail": "refused: 1 of 2 rule text(s) need clarification; nothing was saved"
}
```

### `build_review`

```python
build_review(store, request_id, *, audience, now=None, removed=()) -> Draft
```

Builds and saves the draft the holder reviews for the request `(audience, request_id)`.
`audience` names the bank that sent the request; a bare id works while one bank alone used it and
raises `AmbiguousRequest` once several have.
Only that bank's claims are used, and the body's `case_ref` is derived from the bank and the
request. It uses the claims of the request's latest
investigation run, meaning the run recorded last, whatever clock it ran on (all its claims must
still be active), every source and txn those claims cite (the
cited revisions, which are the active ones while the claims are active), and the subject's
latest policy, if any. The body is default-minimum: only cited evidence goes in, as a full
issuer-signed record when the policy allows every field, otherwise as a redacted extract.
`removed` names sources the holder took out (record or txn ids). The returned draft holds the
recipient-facing `body` and the private `review`, which is never sent. Building the same review
again at the same moment returns the stored draft and keeps its status. With no policy,
`policy_version` is the digest of the empty rule list (`policy_<hash>`); with one, it is
`<policy_id>@v<version>`.

The draft for RFC A with no policy (excerpt):

```json
{
  "draft_id": "drf_8522ad872123e0d3d32c1743",
  "request_id": "rfc_a_001",
  "audience": "bank_a",
  "status": "pending",
  "policy_version": "policy_cf1cbb66a638b4860a516671",
  "body_sha256": "b8b3861868c2e66128f876e599b4742fbd0c3d5b30ced8b41b52677556acf78a",
  "created_at": "2026-11-07T12:00:00Z",
  "source_revisions": [
    "record:fee_3001@1",
    "record:ord_1001@1",
    "record:ord_1002@1",
    "record:ord_1003@1",
    "record:ord_1004@1",
    "record:po_5001@1",
    "record:rfd_2001@1",
    "record:stl_88001@1",
    "txn:txn_9001@1"
  ],
  "body": {
    "request_id": "rfc_a_001",
    "audience": "bank_a",
    "case_ref": "case_ac1d4904e078bd5b42bc73d5769db6364633ccc8cc51e6e7ace5065197ef1c7c",
    "scope": {
      "txn_ids": [
        "txn_9001"
      ]
    },
    "claims": [
      {
        "question_id": "q1",
        "status": "supported",
        "statement": "Credit txn_9001 of 216000 pence GBP is payout po_5001 (reference PO-5001), settled as stl_88001 before the credit was booked.",
        "evidence_refs": [
          "record:fee_3001@1",
          "record:ord_1001@1",
          "record:ord_1002@1",
          "record:ord_1003@1",
          "record:ord_1004@1",
          "record:po_5001@1",
          "record:rfd_2001@1",
          "record:stl_88001@1",
          "txn:txn_9001@1"
        ]
      }
    ],
    "attachments": [
      {
        "attachment_id": "att_84488b9d60915bc20be9798a",
        "ref": "record:fee_3001@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_d2cf313175639b5b5ce61e27",
        "ref": "record:ord_1001@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_9e06e54f213388cb6895cc20",
        "ref": "record:ord_1002@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_35dcb9b600121f9028875349",
        "ref": "record:ord_1003@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_dac5f8acbd2ad074a7a63e17",
        "ref": "record:ord_1004@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_c0251536ef951297ab44ca50",
        "ref": "record:po_5001@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_d060cc933e54ae22c7a68f9e",
        "ref": "record:rfd_2001@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_eaef231a3b70cbb45df34f9d",
        "ref": "record:stl_88001@1",
        "form": "full_record",
        "source_trust": "issuer_verified"
      },
      {
        "attachment_id": "att_699c4ea3f24b44ef1354d27d",
        "ref": "txn:txn_9001@1",
        "form": "redacted_extract",
        "source_trust": "bank_observed"
      }
    ],
    "expires_at": "2026-11-14T12:00:00Z"
  },
  "review": {
    "included": [
      {
        "ref": "record:fee_3001@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:ord_1001@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:ord_1002@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:ord_1003@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:ord_1004@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:po_5001@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:rfd_2001@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "record:stl_88001@1",
        "reason": "full issuer-signed record; cited by q1"
      },
      {
        "ref": "txn:txn_9001@1",
        "reason": "redacted extract; cited by q1"
      }
    ],
    "excluded": [],
    "withheld_fields": [
      {
        "ref": "txn:txn_9001@1",
        "field": "account_ref",
        "reason": "not in the shareable field set"
      },
      {
        "ref": "txn:txn_9001@1",
        "field": "provenance",
        "reason": "not in the shareable field set"
      }
    ],
    "gaps": [],
    "claim_changes": []
  }
}
```

After `record_policy(..., "don't share item names", ...)`, the next draft attaches the orders as
redacted extracts without item names, and the review says why:

```json
{
  "policy_version": "pol_subj_maya@v1",
  "attachment": {
    "attachment_id": "att_9e06e54f213388cb6895cc20",
    "ref": "record:ord_1002@1",
    "form": "redacted_extract",
    "source_trust": "issuer_verified",
    "issuer_id": "threadline",
    "content": {
      "amount_pence": 72000,
      "currency": "GBP",
      "issuer_id": "threadline",
      "items": [
        {
          "category": "outerwear",
          "quantity": 1,
          "unit_amount_pence": 42000
        },
        {
          "category": "footwear",
          "quantity": 1,
          "unit_amount_pence": 30000
        }
      ],
      "kind": "order",
      "occurred_at": "2026-10-11T09:30:00Z",
      "record_id": "ord_1002",
      "related_record_ids": [],
      "revision": 1,
      "source_trust": "issuer_verified",
      "status": "completed"
    },
    "generalised_fields": []
  },
  "review_withheld_fields": [
    {
      "ref": "record:ord_1002@1",
      "field": "items.description",
      "reason": "denied by policy",
      "rule_id": "rule_4dbf2e7b479993cdf5152393"
    }
  ]
}
```

Raises `PackageError` when the request has no investigation yet, or when its latest run was
invalidated by newer evidence (run the investigation again), and writes a refused audit event:

```json
{
  "audit_id": "aud_2a05c3d1964fe7dab20e6b9b",
  "at": "2026-11-07T12:00:00Z",
  "actor": "alibi",
  "action": "build_review",
  "target_id": "rfc_a_001",
  "outcome": "refused",
  "detail": "refused for bank_a: request rfc_a_001 has no investigation results yet; call run_investigation first"
}
```

Raises `NotFound` for an unknown request.

### `approve`

```python
approve(store, draft_id, *, holder_id, now=None) -> Approval
```

Records the holder's approval of one exact pending draft for its named recipient. The approval
binds the SHA-256 of the canonical body, the audience, the request id, the policy version and
every source revision the draft depends on, for 15 minutes. The draft becomes `approved`. An
approval is never a reusable credential: a second request or recipient needs its own draft and
a fresh approval.

```json
{
  "approval_id": "apr_2e338a61dd6525420fb1ff74",
  "request_id": "rfc_a_001",
  "audience": "bank_a",
  "body_sha256": "b8b3861868c2e66128f876e599b4742fbd0c3d5b30ced8b41b52677556acf78a",
  "policy_version": "policy_cf1cbb66a638b4860a516671",
  "source_revisions": [
    "record:fee_3001@1",
    "record:ord_1001@1",
    "record:ord_1002@1",
    "record:ord_1003@1",
    "record:ord_1004@1",
    "record:po_5001@1",
    "record:rfd_2001@1",
    "record:stl_88001@1",
    "txn:txn_9001@1"
  ],
  "holder_id": "holder_maya",
  "draft_id": "drf_8522ad872123e0d3d32c1743",
  "approved_at": "2026-11-07T12:00:00Z",
  "expires_at": "2026-11-07T12:15:00Z",
  "status": "pending"
}
```

Approving again a draft the same holder already approved (the draft is now `approved` or
`packaged`) returns that same approval, pending or consumed, and writes a `duplicate` audit
event. So a retried or replayed flow reaches the stored package through `sign_and_package`.
An expired approval is not renewed: build the review again at the current time for a fresh
draft. A different holder is refused ("only a pending draft can be approved"). The retry's
audit event:

```json
{
  "audit_id": "aud_6074703b8a1bdc035aa79464",
  "at": "2026-11-07T12:00:00Z",
  "actor": "holder_maya",
  "action": "approve",
  "target_id": "apr_2e338a61dd6525420fb1ff74",
  "outcome": "duplicate",
  "detail": "retry for draft drf_8522ad872123e0d3d32c1743: returned the existing pending approval"
}
```

Raises `ApprovalError` (and writes a refused audit event) when the draft is unknown or not
pending (and not already approved by this holder), the policy changed since it was built, a
source it cites changed, or the body has expired.

### `sign_and_package`

```python
sign_and_package(store, approval_id, *, keystore=None, now=None) -> ContextPackage
```

Dispatch. Inside one store transaction, serialised against corrections, it:

1. reloads the approval, its draft, the request, the current policy version and the current
   source revisions;
2. refuses unless the approval is still valid for exactly that body: still pending, unexpired,
   same body digest, recipient, request and policy version, and the same source revisions;
3. signs `{schema_version, package_id, issued_at, body, body_sha256}` with `alibi_demo` in domain
   `alibi:package:v1`;
4. saves the package, marks the approval `consumed` with the `package_id` and the draft
   `packaged`, and records a `sent` delivery to the audience.

A retry for an approval already consumed by a stored package returns that identical package,
with no second delivery.

The package (excerpt: 9 attachments, of which the settlement full record and the bank
txn extract are shown):

```json
{
  "schema_version": "alibi.package.v1",
  "package_id": "pkg_20ca710b85c4221443fd54c6",
  "issued_at": "2026-11-07T12:00:00Z",
  "body": {
    "schema_version": "alibi.package_body.v1",
    "request_id": "rfc_a_001",
    "audience": "bank_a",
    "case_ref": "case_ac1d4904e078bd5b42bc73d5769db6364633ccc8cc51e6e7ace5065197ef1c7c",
    "scope": {
      "txn_ids": [
        "txn_9001"
      ]
    },
    "questions": [
      {
        "question_id": "q1",
        "text": "Please explain the source of the £2,160.00 credit received on 3 November 2026 (txn_9001). Which payment does it correspond to, and what records support that?"
      }
    ],
    "claims": [
      {
        "question_id": "q1",
        "status": "supported",
        "statement": "Credit txn_9001 of 216000 pence GBP is payout po_5001 (reference PO-5001), settled as stl_88001 before the credit was booked."
      }
    ],
    "attachments": [
      {
        "attachment_id": "att_eaef231a3b70cbb45df34f9d",
        "ref": {
          "ref_type": "record",
          "ref_id": "stl_88001",
          "revision": 1
        },
        "form": "full_record",
        "source_trust": "issuer_verified",
        "issuer_id": "threadline",
        "content": {
          "amount_pence": 216000,
          "currency": "GBP",
          "effective_at": "2026-11-03T07:45:00Z",
          "event_id": "evt_stl_88001_r1",
          "issuer_id": "threadline",
          "kind": "settlement",
          "occurred_at": "2026-11-03T08:00:00Z",
          "payment_ref": "PO-5001",
          "record_id": "stl_88001",
          "related_record_ids": [],
          "revision": 1,
          "schema_version": "alibi.merchant_event.v1",
          "settlement_ref": "STL-88001",
          "status": "settled",
          "subject_ref": "subj_maya"
        },
        "issuer_signature": {
          "alg": "Ed25519",
          "kid": "merchant_demo",
          "domain": "alibi:record:v1",
          "sig": "bd3b1b70469d68cd972c560a0990df217d2dea4fe98632eee4f1fec2d26a47a1dcef6deea7ed5f29b5c443be5f3fd133f3ed70565f823b87ca619c22a3643b01"
        },
        "generalised_fields": []
      },
      {
        "attachment_id": "att_699c4ea3f24b44ef1354d27d",
        "ref": {
          "ref_type": "txn",
          "ref_id": "txn_9001",
          "revision": 1
        },
        "form": "redacted_extract",
        "source_trust": "bank_observed",
        "content": {
          "amount_pence": 216000,
          "booked_at": "2026-11-03T10:00:00Z",
          "currency": "GBP",
          "description": "THREADLINE PAYOUT PO-5001 STL-88001",
          "refs": [
            "PO-5001",
            "STL-88001"
          ],
          "revision": 1,
          "source_trust": "bank_observed",
          "txn_id": "txn_9001"
        },
        "generalised_fields": []
      }
    ],
    "expires_at": "2026-11-14T12:00:00Z"
  },
  "body_sha256": "b8b3861868c2e66128f876e599b4742fbd0c3d5b30ced8b41b52677556acf78a",
  "signature": {
    "alg": "Ed25519",
    "kid": "alibi_demo",
    "domain": "alibi:package:v1",
    "sig": "ae567933906fa7ac018258b3bdb1a9d90152c3492744a84308d5bc8fb174a5983459a037c13b6d6a53993b1518fb2a98b22956201237f804d8b7df54f15ea80b"
  }
}
```

The delivery record:

```json
{
  "delivery_id": "dlv_b5a24bc70b21ddb4b85bd855",
  "package_id": "pkg_20ca710b85c4221443fd54c6",
  "request_id": "rfc_a_001",
  "audience": "bank_a",
  "package_sha256": "b87ac3c90dfdd7a596d90eb362d391e2ee4ff022019e1a14a8ac5bf34c6aaf3f",
  "delivered_at": "2026-11-07T12:00:00Z",
  "status": "sent"
}
```

Any validation failure raises `ApprovalError` and writes a refused audit event. Editing an
approved draft's body gives:

```json
[
  "the package body changed after it was approved"
]
```

```json
{
  "audit_id": "aud_061a48ded410a323a7de3ed5",
  "at": "2026-11-07T12:00:00Z",
  "actor": "alibi",
  "action": "sign_and_package",
  "target_id": "apr_2e338a61dd6525420fb1ff74",
  "outcome": "refused",
  "detail": "refused: the package body changed after it was approved"
}
```

A correction after approval (the `pending_correction` example above) gives:

```json
[
  "the draft is invalidated (record:stl_88001 superseded by revision 2), not approved",
  "the approval was invalidated: record:stl_88001 superseded by revision 2",
  "source record:stl_88001 changed after approval (approved revision 1, now revision 2)"
]
```

### `verify_package`

```python
verify_package(package, *, expected_audience, registry=None, now=None,
               expected_subject=None, expected_request_id=None,
               store=None) -> VerificationResult
```

The recipient-side check against the pinned registry. It never raises and never writes:
unusable input, including a `now` that is not timezone-aware, comes back as `accepted: false`.
With `registry=None` it reads the pinned `data/alibi/keys/trust_registry.json` read-only. It
creates no keys and never rewrites that file, and a missing or unreadable file rejects the
package:

```json
{
  "accepted": false,
  "authentication": {
    "valid": false,
    "reasons": [
      "no usable pinned trust registry at data/alibi/keys/trust_registry.json (FileNotFoundError); pass the registry to verify against"
    ]
  },
  "sources": [],
  "claims": [],
  "errors": [
    "no usable pinned trust registry at data/alibi/keys/trust_registry.json (FileNotFoundError); pass the registry to verify against"
  ]
}
```

It gives three separate answers:

- `authentication`: the schema, body digest, recipient binding to `expected_audience` (and,
  when given, `expected_request_id` and `expected_subject`), expiry and the Alibi package
  signature. It says nothing about whether the claims are true.
- `sources`: trust per attachment. A claimed issuer signature is checked against the registry
  in domain `alibi:record:v1` for the attachment's issuer, and its content must match the
  attachment's ref. A failed claimed signature rejects the package. Self-supplied items, bank
  txns and redacted extracts are reported at lower trust.
- `claims`: the status per question, with `calculation_valid` recomputed from the disclosed
  amounts.

`accepted` is true only when authentication is valid and no source check failed.

**Subject binding.** The body's `case_ref` is
`alibi.core.package.case_binding(audience, parent_request_id or request_id, subject_ref)`:
`case_` + sha256 over (`alibi:case:v1`, audience, root request id, subject). It is the same for
every package of one case to one recipient, it does not disclose the subject, and a recipient
that knows its own request's subject recomputes it. With `expected_subject`, a `case_ref` that
does not match is an authentication failure (`the package is not bound to the expected
subject: ...`), so a package whose attachments are all redacted extracts with `subject_ref`
withheld still cannot verify for another subject. Signed sources must also name that subject.

**Defaults from the stored RFC.** With `store` (the passport store, read only), and when the
caller leaves `expected_subject` or `expected_request_id` unset, both default from the stored
request the package names for `expected_audience` (`store.get_request(body.request_id,
issuer=expected_audience)`): `subject_ref` and `request_id`. An explicit argument always wins.
When the store does not hold that request the defaults stay empty and the package verifies as
without a store, so an unknown request is not a rejection. Bank-side callers that open the
passport store should pass it; callers that cannot must pass both values themselves.

Bank A verifying the package above (excerpt: 2 of 9 sources shown; the digests and `case_ref`
values in these examples predate the subject binding, so they do not recompute from the
fixture today):

```json
{
  "accepted": true,
  "package_sha256": "b87ac3c90dfdd7a596d90eb362d391e2ee4ff022019e1a14a8ac5bf34c6aaf3f",
  "authentication": {
    "valid": true,
    "package_id": "pkg_20ca710b85c4221443fd54c6",
    "audience": "bank_a",
    "kid": "alibi_demo",
    "reasons": []
  },
  "sources": [
    {
      "attachment_id": "att_eaef231a3b70cbb45df34f9d",
      "ref": "record:stl_88001@1",
      "form": "full_record",
      "claimed_trust": "issuer_verified",
      "attestation": "issuer_signature",
      "signature_valid": true,
      "lower_trust": false,
      "note": "Issuer signature verified with pinned key merchant_demo for issuer threadline."
    },
    {
      "attachment_id": "att_699c4ea3f24b44ef1354d27d",
      "ref": "txn:txn_9001@1",
      "form": "redacted_extract",
      "claimed_trust": "bank_observed",
      "attestation": "package_signer",
      "lower_trust": true,
      "note": "Bank transaction from the holder's bank feed, attested only by the package signer (lower trust)."
    }
  ],
  "claims": [
    {
      "question_id": "q1",
      "status": "supported",
      "evidence_refs": [
        "record:fee_3001@1",
        "record:ord_1001@1",
        "record:ord_1002@1",
        "record:ord_1003@1",
        "record:ord_1004@1",
        "record:po_5001@1",
        "record:rfd_2001@1",
        "record:stl_88001@1",
        "txn:txn_9001@1"
      ],
      "calculation_valid": true
    }
  ],
  "errors": []
}
```

Bank B verifying Bank A's package:

```json
{
  "accepted": false,
  "authentication": {
    "valid": false,
    "package_id": "pkg_20ca710b85c4221443fd54c6",
    "audience": "bank_a",
    "kid": "alibi_demo",
    "reasons": [
      "the package is addressed to bank_a, not bank_b"
    ]
  },
  "errors": [
    "the package is addressed to bank_a, not bank_b"
  ]
}
```

## Running the tests

From the repo root:

```bash
uv run --frozen python -m pytest alibi -q                       # the whole Alibi suite
uv run --frozen python -m pytest alibi/tests/test_api.py -q     # this API, end to end
```

Tests use pytest's `tmp_path` for every database, key and file, never touch `data/alibi/`,
and make no network or LLM calls. The conftest fixtures `keystore`, `registry` and `now`
(`2026-11-07T12:00:00Z`) give each test its own demo keys and a fixed clock.

## Known issues

- Request ids are per bank (`(issuer, request_id)`), so two banks may reuse an id. Everything
  that names a bare `request_id` (claims, drafts, approvals, packages, `run_investigation`,
  `build_review`) resolves a shared id to the request received first under it at the store level, while
  the holder API (`run_investigation`, `build_review`) refuses a bare id that several banks
  used with `AmbiguousRequest` and takes `audience=` to name the bank. Fixtures use distinct ids.
- `verify_package` binds the subject through `case_ref` only when it is given
  `expected_subject`, or `store` and the request is held. Without either it still rejects
  attachments that name more than one subject. Packages signed before the binding landed carry
  the old `case_ref` and fail a subject-bound verification.
- Request `case_ref` groups a follow-up with its direct parent, not with the root of a longer
  chain (`parent_request_id or request_id`), as before.
- The rules investigation catches double counting only within one run. Two RFCs that each ask
  about a credit backed by the same payout can each get `supported`.
- Calculation signs (not an issue, a rule): `net_payout` and `net_total` net kinds with
  `SIGN_BY_KIND`, so refunds and fees subtract. Every other total covers one kind with one sign:
  positive amounts (the rules engine's refunds total is +18000) or the kind's `SIGN_BY_KIND`
  direction (a receipt total compared with a bank debit). Bank B gets both RFC B answers
  `supported`.

## Differences from DESIGN.md

The code's public behaviour wins. These API functions take arguments DESIGN.md does not list:

- Every function that depends on the clock takes `now`.
- `receive_rfc` takes `account_refs`, because the core refuses `txn_ids` without the bank's
  accounts.
- `build_review` takes `removed`, which is passed to `build_body`.
- `verify_package` takes `expected_subject` and `expected_request_id`, which are passed to the
  core, and `store`, which defaults them from the stored request.
- `load_fixture` also returns `statuses`, `expected` and `now`. Its clock defaults to the
  fixture clock `2026-11-07T12:00:00Z`, not the wall clock.
- `Gateway` also takes `request_id`, which `run_investigation` sets.
- `approve` returns the existing approval when the same holder approves a draft that is already
  `approved` or `packaged`, so the whole flow can be re-run.
- `verify_package(registry=None)` reads the pinned `trust_registry.json` instead of opening the
  demo keystore, so verifying never creates keys.
- A request is identified by `(audience, request_id)`, not by `request_id` alone. Store schema
  version 3 keys the requests table by both columns.
  - `store.get_request` takes `audience` (alias `issuer`). It is the internal lookup; a bare id
    resolves to the request received first, and `strict=True` (what the public API uses) raises
    `AmbiguousRequest` once several banks used it.
  - `list_claims`, `list_drafts` and `list_approvals` take `audience`.
  - `Claim` carries `audience`.
  - `next_run_id(store, request_id, *, audience)` derives run ids from the bank as well.
  - `case_ref` is `make_id("case", audience, parent_request_id or request_id)`.
  - The public API resolves a bare request_id only while one bank alone has used it;
    `run_investigation` and `build_review` take `audience` and raise `AmbiguousRequest` for a
    shared bare id, and `load_fixture` returns the `{"request_id", "audience"}` pairs as
    `requests`. The `build_review` refusal audit names the bank.

## Adapter (`alibi.adapter`)

The adapter is the system-under-test harness: it runs one team case through the simulated
services into a fresh passport and emits one prediction per RFC in the shape of
`alibi/eval/predictions.md` (`alibi/eval/schemas/prediction.schema.json`). Every emitted object
is validated with `alibi.eval.jsonschema_lite` before it is returned.

```python
from alibi.adapter import run_case
predictions = run_case(1, "rules")                       # one dict per RFC of case 1
predictions = run_case(case_dict, "agents", client=fake) # a case dict (gold is stripped), a test client
```

`run_case(case, mode, as_of=None, *, client=None, run_id=None, replay=None, root=None)`:

- `case` is a case number (`agents.data.api.load_case`; the lockbox hides sealed numbers) or a
  case dict in the data schema. Cases are loaded only through `agents.data.api`
  (`load_case`, `without_gold`, `lookup` behind the mock merchant); gold is removed either way.
- `mode` is `rules` (the core engine, `alibi.core.investigate_rules`), `one_pass` (the single-call
  Gemini baseline, `alibi.baselines.one_pass`; alias `single_call`) or `agents` (the Gemini roles
  pipeline, `alibi.agents.runner`; alias `alibi`). `alibi.api.run_investigation(mode=...)`
  dispatches the same way and takes `client` for the model modes.
- `as_of` answers every RFC issued by that moment at that moment. Without it each RFC is
  answered at its own `issued_at` (below).
- `client` is the model client for the model modes (anything with the `GeminiClient.generate`
  signature, e.g. `alibi.llm.fake.FakeClient`). Without one the adapter builds
  `GeminiClient.for_run(mode, run_id)` (a fresh log under `data/alibi/llm_logs/`, every row
  scoped to the case through `client.scoped(case_number=n)`) and wraps it with
  `alibi.agents.replay.make_client(case=f"case{n}", variant=mode, live=client)`, so
  `ALIBI_REPLAY=1` serves the recording in `alibi/fixtures/replays/case<n>_<mode>.json` and a
  live run records it. The process cap is `ALIBI_LLM_CAP` and the response cache stays on.
  `replay=True` wraps a given client in the recorder too (tests leave it off).
- `root` moves the per-case store folder; the default is
  `data/alibi/adapter/<mode>/case_<n>/`, emptied before each run.

What one run does, through the real paths only:

1. A fresh `Store`, `keys.connector_keystore(<root>/keys)` and its registry (passed to every
   passport call; the default keystore would drop the merchant and app issuer keys).
2. The mock merchant (`alibi.connectors.merchant.create_merchant_app`) with a
   `plug.webhook_sink` over the store and the mock bank (`alibi.bank.service.create_bank_app`)
   with a `plug.rfc_sink`, both as in-process `TestClient`s with fresh bearer tokens for
   `bank_a`, `bank_b`, `feed` and `admin` (never written or printed). The services load the
   case through a loader bound to this case only, so a sealed case given as a dict works.
3. `connectors.clients.bootstrap(store, registry, case=n, merchant=MerchantClient(...),
   banks={bank_a, bank_b}, feed=FeedClient(HttpFeed over the bank client, feed token),
   clock=CaseClock(case, first issued_at))`: the merchant pushes its visible signed webhooks,
   the bank is seeded and the feed synced with its cursor, the visible holder imports go
   through the self-supplied import, and each bank issues its visible RFCs into `receive_rfc`.
   A holder policy on the case (`holder_policy` or `policy`) is recorded with `record_policy`;
   otherwise the subject has no policy.
4. **The clock.** RFCs are answered in `issued_at` order, each with `now=issued_at`. When the
   next RFC is issued later than the clock, the adapter advances the merchant and bank clocks
   (`POST /admin/advance`), which pushes the newly visible deliveries, syncs the feed, imports
   what became visible and issues the newly visible RFCs, and only then answers it. The first
   RFC is always answered before the clock moves.
5. Per RFC: `run_investigation(store, request_id, audience=rfc.audience, mode=mode,
   registry=registry, now=issued_at, **connect(merchant, feed))` (the one-pass mode calls
   `one_pass.run_one_pass` directly so the pre-policy proposal is available, and the adapter
   then writes the same `run_investigation` audit row, actor `one_pass`, that
   `api.run_investigation(mode="one_pass")` writes, so every mode's run is in the passport's
   audit), then `build_review(store, request_id, audience=..., now=issued_at)` for the
   policy-filtered draft.

No lookahead: an RFC is answered with what the services had delivered or made available by its
`issued_at` and nothing later. No development case has data arriving between its RFCs, so the
clock-advance path is pinned by `alibi/tests/test_adapter.py::
test_an_rfc_is_answered_before_later_data_lands_and_the_next_one_sees_it` on a synthetic copy of
case 1 whose lookup-only settlement and one extra order arrive between the two RFCs: Bank A's
answer is `unresolved settlement_not_found` after one empty lookup and never mentions either
record; Bank B, answered after the advance, observes the extra order. Every prediction's refs
are also checked against `agents.data.api.known_at(case, issued_at, include_lookup=True)`.

The prediction object:

| Field | Source |
| --- | --- |
| `links` | The active link in the store for the case `holder_claim.txn_id`: `state`, `payout_record_ids` (the linked payout plus every payout record the supported source-of-funds claim cites, so a settlement batch lists all of them), `candidate_record_ids`, `method`. |
| `claims` | The stored `Claim` of the run per question: `status`; `gap` is the pinned code at the head of `Claim.gap` for an unresolved answer (null otherwise, as gold); `facts` is `Claim.facts`; `evidence_refs` are the claim refs plus the calculation terms in gold syntax (`bank:<txn_id>`, `merchant:<record_id>` for issuer-verified records whether pushed or looked up, `import:<record_id>` for self-supplied records with the `self_` namespace stripped); `statement`, `rationale`, `calculation`. |
| `package_claims` | Extra (ignored by the scorer): the draft body claim per question with the `claim_changes` reason from the review, so a downgrade by the policy gate is visible. |
| `package` | The draft body: `{request_id, audience, empty, attachments[{ref, form, fields (extracts: the disclosed field names), content, source_trust}]}`. |
| `exclusions` | The `excluded` entries of the draft review plus every active record and bank entry of the subject that is neither attached nor cited (`not needed to answer the questions`). Never part of the package. |
| `proposed_package` | The pre-policy proposal, same shape with `claims`: rules, the unfiltered cited set; one_pass, the model `disclosures` and claims from `OnePassRun.proposal`; agents, the Curator kept set from the trace notes (the cited set of the claims when the Curator gave no answer). |
| `cost` | `model_calls`, `tokens` (agents: `runner.run_stats`; one_pass: `OnePassRun`; rules: 0), `tool_calls` (gateway calls), `latency_ms` (investigation plus review), `cost_usd_estimate` 0 (no price list is pinned; `cost_note` says so). |
| `retrieval` | `lookups`, `observed_inputs` (agents and one_pass from their counters; rules: gateway `lookup_settlement` calls and the active records plus bank entries of the subject after the run) and the predeclared `strategy` (`alibi.adapter.STRATEGIES`). |
| `failure` | `null`, or typed: an engine exception (`error`), a gateway or model budget refusal (`budget_exceeded`), an outage or missing key (`unavailable`), a schema failure (`invalid_output`). In the model modes a failed role or model call is read from the result (`unavailable` entries, the one-pass `generate` note) and reported here; the engines never raise for it. |
| `system`, `issued_at` | The mode label and the clock the request was answered at. |

Retrieval strategy per mode (`retrieval.strategy`): **rules**: deterministic; list the records
and bank entries of the subject, and when no stored payout settles the credit, look up the
settlement for the payout reference the credit carries or the subject records name, one call
per reference. **one_pass**: `FIXED_RETRIEVAL`, look up any payout reference named in a
`balance_release` message, then one model call. **agents**: adaptive; the Detective proposes
checks and the Challenger requests at most one lookup per round (settlement by reference,
payouts by covered record or date) within an action budget of 3 per question and 3 rounds.

CLI:

```
uv run --frozen python -m alibi.adapter --mode rules --cases 1-80 --out data/alibi/predictions
uv run --frozen python -m alibi.adapter --modes rules,one_pass,agents --cases 1,2 --families demo
uv run --frozen python -m alibi.adapter --mode rules --cases 1-80 --as-of 2026-11-04T14:00:00Z
```

It writes one `<out>/<mode>.jsonl` per mode, first line `{"system": {"name", "model",
"commit"}}`, then one prediction per request; `--out` must be under `data/alibi`
(`ALIBI_DATA_DIR` moves it; exit code 2 otherwise, and 2 when no visible case matches). It
prints one progress line per case and, last, one JSON summary per mode: `{mode, cases,
requests, failures, lookups, observed_inputs, model_calls, tokens, out}`. A case whose seeding
raises is written as one typed `error` failure per RFC, so the file stays complete. The same
runs from Python: `run_cases(cases, mode, out=Path, as_of=None, run_id=None, client=None,
log=None, root=None)` returns the summary; `root` is `run_case`'s store folder. Score with
`uv run --frozen python -m alibi.eval.scorer --predictions data/alibi/predictions/rules.jsonl --by-family`.

Tests: `alibi/tests/test_adapter.py` (no Gemini: `FakeClient` for the model modes; stores under
the test `tmp_path`, the CLI under a `tmp_path` `ALIBI_DATA_DIR`).
