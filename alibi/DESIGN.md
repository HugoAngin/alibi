# Alibi S1 design: module contracts

Internal build contract for session S1 (foundation). `SPEC.md` is the product spec;
`CONTRACTS.md` documents the public `alibi.api`. This file pins the signatures and
behaviour of every core module so they can be built and tested independently.
Types come from `alibi/core/schemas.py`. Do not redefine them.

## Ground rules

- Money is integer pence + ISO currency. Merchant amounts are non-negative magnitudes;
  direction comes from `kind` (order +, refund -, fee -). Bank amounts are signed from the
  holder's view.
- Times are timezone-aware UTC. Every function that depends on the clock takes
  `now: datetime | None = None` and falls back to `schemas.utc_now()`.
- Periods are half-open `[from, to)`: use `Period.contains`.
- Hash and sign with `alibi.core.canon` (`canonical_bytes`, `digest`). IDs come from
  `canon.make_id(prefix, *parts)` (deterministic) unless noted.
- Verify signatures over the raw received JSON object, never a re-serialised model.
- Errors are typed exceptions with a human-safe message. Never log or print
  `ALIBI_MASTER_KEY`, `GEMINI_API_KEY` or private keys.
- Generated files (DB, keys, master key) live under `data/alibi/` by default. Tests use
  `tmp_path` and never touch `data/alibi/`.
- Plain functions and small modules. No network. No LLM.

## Canonical fixture values

Maya (subject `subj_maya`) resells clothes on the mock platform `threadline`
(issuer key `merchant_demo`). Her Bank A account is `acc_maya_bank_a`.

| Record | id | Amount (pence) | Notes |
| --- | --- | --- | --- |
| Orders | `ord_1001`..`ord_1004` | 60000, 72000, 48000, 60000 (= 240000) | October 2026, status `completed`, item detail with description + category |
| Refund | `rfd_2001` | 18000 | partial refund of `ord_1003`, status `completed` |
| Fee | `fee_3001` | 6000 | platform fees, status `completed` |
| Message | `msg_7001` | 216000 | "released to your seller balance" text; release to the platform balance, not a bank transfer |
| Payout | `po_5001` | 216000 | `payment_ref` `PO-5001`, status `initiated`, `related_record_ids` = the 4 orders, refund, fee |
| Settlement | `stl_88001` | 216000 | `payment_ref` `PO-5001`, `settlement_ref` `STL-88001`; only reachable by follow-up lookup |
| Bank credit | `txn_9001` | +216000 | booked `2026-11-03T10:00:00Z` |

Variants: `supported` (credit "THREADLINE PAYOUT PO-5001 STL-88001", refs `PO-5001`,
`STL-88001`; settlement `settled`, `effective_at` 2026-11-03T07:45:00Z, before booking),
`contradiction` (credit "INCOMING TRANSFER" with no refs; settlement status `pending` observed at
2026-11-03T12:00:00Z, after booking, no `effective_at`), `pending_correction` (`supported` plus a
signed revision 2 of `stl_88001` with status `pending` observed 2026-11-04T09:00:00Z,
`corrects_revision` 1, in `corrections`), `ambiguous` (a second payout `po_5002`, 216000,
`PO-5002`, before booking, with its own settled settlement `stl_88002`, and a credit with no
refs). The message `msg_7001` carries status `released_to_balance`. The retail example
(`retail_purchase.json`) has a self-supplied receipt `self_rcpt_4001` (4599) and debit
`txn_9002` (-4599). Each fixture file carries an `expected` block (development gold).

RFC A `rfc_a_001` (bank_a): source of `txn_9001`. RFC B `rfc_b_001` (bank_b): documented
sales and refunds over `[2026-10-01, 2026-11-01)`; expected totals 240000 and 18000.

## Status vocabulary (explicit fields)

Defined in `alibi/core/reconcile.py` and reused by the rules investigation:
`SETTLED_STATUSES = {"settled", "paid", "completed", "succeeded"}`,
`PENDING_STATUSES = {"pending", "processing", "in_transit", "initiated", "scheduled"}`,
`FAILED_STATUSES = {"failed", "reversed", "returned", "cancelled"}`. Any other status,
including `released_to_balance`, is not a settlement.

## core/store.py

```python
class StoreError(Exception)            # base; message is safe to show
class IdempotencyConflict(StoreError)  # same (connector, event_id) or idempotency key, different bytes
class RevisionConflict(StoreError)     # same (record_id, revision) with different content
class NotFound(StoreError)

@dataclass(frozen=True)
class PutResult:
    status: Literal["inserted", "duplicate", "corrected", "stale"]
    record: EvidenceRecord | BankTxn   # the stored row (the existing one on duplicate)
    active: bool                        # is this row the active revision now
    invalidated: dict[str, list[str]]   # {"links": [...], "claims": [...], "drafts": [...], "approvals": [...]}

DEFAULT_DB_PATH = Path("data/alibi/passport.db")
def load_master_key(key_path: Path) -> bytes   # ALIBI_MASTER_KEY env, else key file (created 0600)

class Store:
    def __init__(self, path=DEFAULT_DB_PATH, *, master_key: bytes | None = None,
                 key_path: Path | None = None)   # key_path default: <db dir>/master.key
    def close(self); __enter__/__exit__
    def transaction(self)   # context manager, BEGIN IMMEDIATE, re-entrant, serialises writers

    # merchant evidence ("sources"): append-only revisions with an active flag
    def put_source(self, record: EvidenceRecord, *, raw_payload: dict) -> PutResult
    def get_source(self, record_id: str, revision: int | None = None) -> EvidenceRecord
    def source_history(self, record_id: str) -> list[EvidenceRecord]     # ascending revision
    def list_sources(self, subject_ref: str, *, kinds=None, active_only=True) -> list[EvidenceRecord]
    def get_raw_payload(self, record_id: str, revision: int) -> dict

    # bank feed
    def put_bank_txns(self, txns: list[BankTxn], *, connector: str, cursor: str | None = None,
                      connection_id: str | None = None) -> list[PutResult]
    def get_bank_txn(self, txn_id: str, revision: int | None = None) -> BankTxn
    def txn_history(self, txn_id: str) -> list[BankTxn]
    def list_bank_txns(self, subject_ref: str, *, active_only=True) -> list[BankTxn]
    def get_cursor(self, connector: str, connection_id: str) -> str | None

    # derived results, each stored with dependency tokens ("record:<id>", "txn:<id>")
    def save_link(self, link: Link); def list_links(self, *, txn_id=None, state="active") -> list[Link]
    def save_claim(self, claim: Claim); def list_claims(self, *, request_id=None, audience=None, state="active") -> list[Claim]
    def save_alert(self, alert: Alert); def list_alerts(self, subject_ref: str, *, state=None) -> list[Alert]
    def save_policy(self, policy: Policy); def get_policy(self, policy_id: str, version: int | None = None) -> Policy
    def latest_policy(self, subject_ref: str) -> Policy | None
    def save_request(self, rfc: RequestForContext) -> Literal["inserted", "duplicate"]
    def get_request(self, request_id: str, *, audience: str | None = None) -> RequestForContext  # bare id shared by banks: AmbiguousRequest
    def list_requests(self, *, subject_ref=None, audience=None) -> list[RequestForContext]
    def save_draft(self, draft: Draft); def get_draft(self, draft_id: str) -> Draft; def list_drafts(self, *, request_id=None, audience=None) -> list[Draft]
    def save_approval(self, approval: Approval); def get_approval(self, approval_id: str) -> Approval
    def list_approvals(self, *, request_id=None, audience=None, status=None) -> list[Approval]
    def save_package(self, package: ContextPackage, *, approval_id: str) -> Literal["inserted", "duplicate"]
    def get_package(self, package_id: str) -> ContextPackage
    def list_packages(self, *, audience=None, request_id=None) -> list[ContextPackage]
    def save_delivery(self, delivery: Delivery); def list_deliveries(self, *, audience=None, package_id=None) -> list[Delivery]
    def append_audit(self, event: AuditEvent); def list_audit(self, *, target_id=None) -> list[AuditEvent]
    def append_traces(self, events: list[TraceEvent]); def list_traces(self, run_id: str) -> list[TraceEvent]
    def invalidate_dependents(self, token: str, reason: str, *, now=None) -> dict[str, list[str]]
    # status helpers
    def update_draft_status(self, draft_id, status, reason=None) -> Draft
    def update_approval(self, approval: Approval) -> None   # e.g. mark consumed with package_id
    def set_alert_state(self, alert_id, state) -> Alert
    def get_package(self, package_id, *, audience=None) -> ContextPackage  # other audience -> NotFound
# module level
SELF_SUPPLIED_PREFIX = "self_"; class NamespaceError(StoreError)
def dependency_refs(obj) -> list[EvidenceRef]; def dependency_tokens(obj) -> list[str]
def load_master_key(key_path, *, create=True) -> bytes
```

Behaviour:

- Idempotency for merchant evidence is by `(record.connector, record.issuer_id,
  record.event_id)` plus `record.payload_sha256`, which must equal `canon.digest(raw_payload)`
  (else `StoreError`). Event keys are scoped to the issuer, so two issuers can reuse an
  event id. Identical bytes return the existing row with status `duplicate` and change nothing.
  Same key, different bytes raises `IdempotencyConflict`.
- Namespaces: self-supplied record ids start with `self_` and issuer evidence may not use
  that prefix (`NamespaceError`), so an import can never occupy an issuer's record id.
- The same `(record_id, revision)` arriving under a different event: identical record content
  (compare `digest(record.to_event().to_json_dict())` minus `event_id`) is a `duplicate`;
  otherwise `RevisionConflict`.
- A higher revision than the active one is a correction: append it as active, keep the old
  row (inactive), and call `invalidate_dependents("record:<id>", reason)`: active links and
  claims depending on it become `invalidated` with the reason; drafts in `pending`/`approved`
  become `invalidated`; approvals in `pending` become `invalidated`. Delivered packages are
  never altered; append an `AuditEvent(action="evidence_superseded")` per affected package.
  Status `corrected`.
- A lower revision than the active one is stored as inactive history (status `stale`) and
  never replaces the active row. Nothing is invalidated.
- Bank txns: idempotency key `(connector, txn_id, revision)` + sha256 of the txn JSON minus
  `provenance` (retrieval time varies per fetch), same rules. A higher revision invalidates
  dependents of `txn:<id>`. The whole batch and its cursor commit in one transaction; on any
  error nothing (cursor included) is written. Cursors are kept per `(connector,
  connection_id)`; `connection_id` defaults to the batch's `provenance.connection_id`, and a
  batch with a cursor must come from one connection.
- Derived results are saved against current evidence: a link, claim, draft or approval that
  cites a revision which is no longer active is stored already `invalidated` ("cites
  record:x@1 but revision 2 is now active"). Terminal states never change: invalidated links,
  claims, drafts and approvals, packaged drafts, consumed approvals (an identical re-save is
  allowed). An existing delivery may change only `status` and `receipt_id`.
- Integrity: every decrypted object must match its row's clear columns, and the active
  revision must be the highest stored one; otherwise `StoreError` ("integrity check failed").
- Typed errors only: out-of-range revisions are `NotFound`, malformed arguments `StoreError`.
  Required scope arguments (`subject_ref`, `run_id`) of `None` raise instead of matching all.
- `trace_id` and `audit_id` must be unique per distinct event: re-appending identical events is
  a no-op, a different event under an existing id raises `IdempotencyConflict`.
- Dispatch (current revisions -> sign -> save package -> approval consumed -> draft packaged ->
  delivery) must run inside one `with store.transaction():` so it is serialised against
  corrections.
- Requests are keyed by `(request_id, audience)`, because request ids are the bank's own and
  one bank must never block or detect another's (schema version 3). The same key with
  identical content is a duplicate, and different content raises `IdempotencyConflict`. Claims
  carry `audience`, and run ids and `case_ref` include it. Packages: same rule keyed by
  `package_id`. Policies: append-only
  `(policy_id, version)`; same version with different content raises `RevisionConflict`.
- Encryption at rest: every payload column holds Fernet(canonical JSON). Clear columns hold
  only identifiers, revision numbers, active/status flags, dependency tokens and timestamps
  needed for integrity. Amounts, references, descriptions, items and text are only inside
  the encrypted payload. Lookups decrypt the subject's rows and filter in Python.
- Master key: explicit `master_key` arg, else env `ALIBI_MASTER_KEY` (a Fernet key; invalid
  value raises `StoreError` without echoing it), else `key_path` (generated with
  `Fernet.generate_key()`, written 0600). The key never goes into the database.
- Dependency tokens: link = txn_ref + record_ref + candidates; claim = evidence_refs +
  calculation.cited_refs; draft/approval = source_revisions; package = attachment refs and
  claim evidence refs.

## core/reconcile.py

```python
class ReconcileError(ValueError); class MixedCurrencyError(ReconcileError)
class DoubleCountError(ReconcileError); class MissingAmountError(ReconcileError)
SIGN_BY_KIND = {ORDER: 1, RECEIPT: 1, REFUND: -1, FEE: -1}
def term_for(item: EvidenceRecord | BankTxn, *, sign: int | None = None) -> CalcTerm
def sum_terms(items, *, method: str, signs=SIGN_BY_KIND, currency: str | None = None) -> Calculation
def compare_to_target(calc: Calculation, target: EvidenceRecord | BankTxn) -> Calculation
def settlement_effective_time(settlement: EvidenceRecord) -> datetime | None
def check_settlement_timing(settlement: EvidenceRecord, credit: BankTxn) -> TimingCheck
def reconcile_credit(credit: BankTxn, *, components, payout=None, settlement=None) -> Calculation
def credit_supported(calc: Calculation) -> bool   # balanced and timing present and ok
```

- `sum_terms` rejects mixed currencies, a record_id (or txn_id) cited twice even at different
  revisions, missing amounts and kinds with no sign. The returned `Calculation` lists every
  cited ref in `cited_refs`.
- Effective time: a settled status with `effective_at` uses `effective_at`; settled without
  it uses `occurred_at` (the time the status was observed); any other status has no effective
  time. Timing is ok only when the effective time is `<= credit.booked_at`. A settlement
  effective after the booking cannot support the credit. Reasons distinguish "pending at or
  after booking" from "pending before booking, no evidence it settled in time".
- `reconcile_credit` sums components (orders - refunds - fees; any other kind raises) against
  the credit amount, attaches the settlement `TimingCheck`, and cites components, payout,
  settlement and credit in `cited_refs`. Payout and settlement are held to the same rule: a
  different currency raises `MixedCurrencyError`; a different amount makes that record the
  target, so `balanced` is False and the difference shows. With a payout, the settlement must
  carry the payout's `payment_ref` (else `ReconcileError`). Only kind `settlement` records have
  an effective time; anything else in the settlement slot (e.g. the balance-release message)
  gives `timing.ok=False` with reason "not a settlement record".
- `credit_supported` re-derives its verdict from explicit fields (txn target, balanced with
  zero difference, timing for that credit, settled status, effective time at or before
  booking). Recompute any Calculation from an untrusted source with `reconcile_credit`.
- `SIGN_BY_KIND`: order +1, refund -1, fee -1, receipt -1 (a receipt documents a purchase).
  Totals beyond the canonical JSON range (2**53 - 1) raise `ReconcileError`.

## core/policy.py

```python
RECORD_FIELDS: frozenset[str]   # allowlisted extract fields for merchant evidence
TXN_FIELDS: frozenset[str]      # allowlisted extract fields for bank txns
FULL_RECORD_META_FIELDS: frozenset[str]   # opaque ids allowed inside a full signed record

class PolicyError(ValueError)
@dataclass
class FilterResult:
    ref: EvidenceRef
    included: bool
    fields: dict[str, Any]         # extract content after deny/generalise/withhold
    full_record_ok: bool           # every field of the signed body may go out unchanged
    excluded_reason: str | None; excluded_rule: str | None
    withheld: list[tuple[str, str, str | None]]     # (field, reason, rule_id)
    generalised: list[tuple[str, str, str | None]]  # (field, how, rule_id)

@dataclass
class CompiledPolicy:
    policy_version: str
    rules: list[PolicyRule]
    def recipient_allowed(self, audience: str, kind: RecordKind | None = None) -> tuple[bool, str | None]
    def filter_record(self, record: EvidenceRecord, *, audience: str, now=None) -> FilterResult
    def filter_txn(self, txn: BankTxn, *, audience: str, now=None) -> FilterResult

def compile_policy(policy: Policy | list[PolicyRule] | None, *, policy_version: str | None = None) -> CompiledPolicy

@dataclass
class RuleParse:
    status: Literal["resolved", "unresolved"]
    rule: PolicyRule | None
    interpretation: str        # plain-English restatement for the holder to confirm
    reason: str | None
def parse_rule_text(text: str, *, rule_id: str | None = None) -> RuleParse
```

- Allowlist for records: `record_id, revision, kind, issuer_id, occurred_at, effective_at,
  amount_pence, currency, payment_ref, settlement_ref, status, items.description,
  items.category, items.quantity, items.unit_amount_pence, related_record_ids,
  corrects_revision, correction_reason, source_trust`. For txns: `txn_id, revision,
  amount_pence, currency, booked_at, description, refs, source_trust`. Everything else
  (`text`, `subject_ref`, `account_ref`, provenance, signatures) is withheld from extracts.
- Deny wins over every other rule. A denied kind excludes the whole record. A denied field
  is withheld even if a generalise rule also names it.
- Generalise: `items.description` + `category` becomes the item's category, or is withheld
  if there is none; timestamps + `day`/`month` become `YYYY-MM-DD`/`YYYY-MM`. Anything a
  rule cannot evaluate (no generaliser, missing timestamp for an age rule, unknown field) is
  withheld, never passed through.
- Age: records older than `now - max_age_days` (records: `occurred_at`; txns: `booked_at`)
  are excluded. Recipient: an audience outside `allow` gets nothing in scope.
- `full_record_ok` is true only when the record is issuer-verified, included, and every key
  of `signed_body` (items flattened as `items.<key>`) is in `RECORD_FIELDS |
  FULL_RECORD_META_FIELDS` with nothing withheld or generalised.
- `parse_rule_text` handles a handful of phrasings with regexes ("don't share item names",
  "nothing older than 90 days", "only Bank A", "only share with Bank B", "show categories
  instead of item names", "don't share refunds") and returns `unresolved` for anything else.
  Only whole-sentence matches resolve, so a valid phrase wrapped in an injection stays
  unresolved. The LLM parser arrives later with the same signature.
- As built: `FULL_RECORD_META_FIELDS = {schema_version, event_id, subject_ref}`;
  `recipient_allowed` returns `(allowed, blocking_rule_id)`; `kinds` scopes every rule type
  (a deny rule with `kinds` and `fields` withholds those fields on those kinds; only a deny
  rule without fields excludes whole records); an unknown `items.<x>` withholds all item
  detail, any other unknown field (or a sub-path of a shareable field, `signed_body` or
  `issuer_signature`) excludes every source in the rule's scope; the age boundary keeps a
  source dated exactly `now - max_age_days`.
- `filter_record(record, *, audience, now=None, excluded_ids=())`: ids of records that are not
  shared are withheld from `related_record_ids` (reason "refers to a record that is not
  shared"), and `full_record_ok` is False when the signed body names one. Callers filter every
  record once, collect the excluded ids, then filter again with `excluded_ids`.

## core/approval.py

```python
class ApprovalError(Exception)   # .problems: list[str]
DEFAULT_APPROVAL_TTL = timedelta(minutes=15)
def approve(draft: Draft, *, holder_id: str, now=None, ttl=DEFAULT_APPROVAL_TTL) -> Approval
def current_revisions(store, refs: list[EvidenceRef]) -> dict[str, int | None]   # token -> active revision
def check_approval(approval, *, body, audience, request_id, policy_version, revisions: dict[str, int | None], now=None) -> list[str]
def require_valid(...same...) -> None   # raises ApprovalError(problems)
```

Approval binds `sha256(JCS(body))`, audience, request_id, policy version and the source
revisions the draft depends on, with a validity window. Only a `pending` draft can be
approved. `check_approval` returns every problem: status not `pending`, expired, body digest
mismatch, audience or request mismatch, policy version changed, any source revision changed
or missing.

## core/package.py

```python
DEFAULT_PACKAGE_TTL = timedelta(days=7)
def build_body(request, claims, evidence, policy, *, now=None, expires_at=None) -> tuple[PackageBody, Review]
def make_draft(request, body, review, *, policy_version: str, source_revisions, now=None) -> Draft
def package_signing_payload(package: ContextPackage | dict) -> dict
def sign_package(body, approval, keystore, *, policy_version, revisions, now=None, kid="alibi_demo") -> ContextPackage
def verify_package(package: dict | str | bytes | ContextPackage, registry, *, expected_audience: str, now=None) -> VerificationResult
```

- `build_body`: compiles the policy, checks the recipient, filters every evidence item, and
  attaches it as a `full_record` (issuer-verified and `full_record_ok`: content is the exact
  `signed_body`, with its issuer signature), a `redacted_extract` (allowlisted, filtered
  fields, attested by the package signer), or excludes it with a reason in the review.
  Claims become `PackageClaim`s whose `evidence_refs` keep only disclosed refs (plus the
  request's own `txn_ids`, which the recipient already holds). A `supported` claim that lost
  cited evidence is downgraded to `unresolved` with a generic gap ("Some supporting evidence
  was withheld at the account holder's request") and a `ClaimChange` in the review. A
  calculation is kept only when every term is disclosed. Questions with no claim get an
  `unresolved` claim. Finally a forbidden-content scan replaces any withheld string value
  (4+ characters) found anywhere in the body (statements, rationales, gaps, contents) with
  `[withheld]`, recorded in the review. Gaps never name the withheld category or merchant.
- Policy integration (from the policy module as built): filter in two passes (first to find
  excluded records, then again with `excluded_ids` so references to them are withheld); decide
  the attachment form only from `FilterResult.full_record_ok`; when an extract withholds
  `issuer_id`, also leave `Attachment.issuer_id` unset. The forbidden-content scan reads values
  from the source records (FilterResult holds paths, not values): every string value of a
  withheld or generalised field (original item descriptions, exact timestamps), free text,
  and the ids and issuer of excluded records. Non-string values are skipped.
- `sign_package` revalidates the approval (`require_valid`), then signs
  `{schema_version, package_id, issued_at, body, body_sha256}` with domain
  `alibi:package:v1`. `package_id = make_id("pkg", approval.approval_id, approval.body_sha256)`.
- `verify_package` returns `VerificationResult` with three separate parts: `authentication`
  (schema, body digest, recipient binding to `expected_audience`, expiry, Alibi signature
  with a `package_signing` key), `sources` (per attachment: a claimed issuer signature is
  checked against the pinned registry with the record domain and the attachment's issuer;
  content must match the attachment's ref; a failure rejects the package; self-supplied or
  redacted items are reported at lower trust), and `claims` (status per question, with a
  recomputed `calculation_valid` against disclosed attachment amounts). `accepted` is true
  only when authentication is valid and no claimed issuer signature failed.

## core/ingest.py

```python
CONNECTOR_WEBHOOK = "merchant_webhook"; CONNECTOR_LOOKUP = "merchant_lookup"
CONNECTOR_IMPORT = "self_supplied_import"; CONNECTOR_BANK = "bank_feed"
class IngestError(Exception)
def ingest_merchant_event(store, registry, payload, *, connector=CONNECTOR_WEBHOOK, subject_ref=None, now=None) -> PutResult
def import_self_supplied(store, payload, *, subject_ref: str, now=None) -> PutResult
def ingest_bank_txns(store, txns, *, subject_ref: str, connection_id: str, connector=CONNECTOR_BANK, cursor=None, now=None) -> list[PutResult]
def receive_rfc(store, registry, payload, *, authenticated_issuer: str, now=None) -> RequestForContext
```

- Merchant events arrive as `{"event": {...}, "signature": {...}}` (dict, JSON str or bytes,
  parsed with `canon.parse_json`). The issuer path claims issuer provenance, so a signature is
  mandatory and is verified over the raw `event` object with domain `alibi:record:v1` and
  `issuer_id=event.issuer_id`. Failures raise `IngestError` and write a refused audit event.
- Self-supplied imports drop any `signature`, `source_trust` or other trust claim from the
  user's payload, fill `event_id`/`record_id` deterministically when missing, default
  `revision` 1 and `issuer_id` "self_supplied", and always store `self_supplied` trust. The
  stored `record_id` is always in the `self_` namespace: a user-supplied id without the prefix
  becomes `self_<id>`, a missing one `self_<digest>`. A `subject_ref` that differs from the
  holder's is rejected.
- `receive_rfc` accepts `{"request": {...}, "signature": {...}}` or a bare request. The
  authenticated issuer must equal `issuer` and `audience` (recipient binding). A present
  signature must verify with domain `alibi:request:v1` for that issuer. The subject must be
  one the passport holds, every `txn_id` must belong to it, and a `parent_request_id` must
  exist with the same issuer and subject. Out-of-scope references are rejected.

## gateway.py

```python
class GatewayError(Exception); class ScopeError(GatewayError); class BudgetExceeded(GatewayError)
class Unavailable(GatewayError); class NotFoundError(GatewayError)
SettlementLookup = Callable[[str], dict | None]   # payment_ref -> signed merchant event payload
TxnRefresh = Callable[[str], dict | None]          # txn_id -> bank txn JSON
class Gateway:
    def __init__(self, store, *, subject_ref, run_id, registry=None, budget=12,
                 settlement_lookup=None, txn_refresh=None, now=None)
    calls_used: int; trace: list[TraceEvent]
    def get_source(self, record_id, revision=None) -> EvidenceRecord
    def get_txn(self, txn_id) -> BankTxn
    def list_records(self, *, kinds, period: Period | None = None) -> list[EvidenceRecord]
    def list_candidates(self, txn_id, window: timedelta = timedelta(days=5), kinds=(RecordKind.PAYOUT,)) -> list[EvidenceRecord]
    def lookup_settlement(self, payment_ref) -> EvidenceRecord | None
    def refresh_txn(self, txn_id) -> BankTxn
```

Every call checks the budget first (refused calls past the budget raise `BudgetExceeded`),
then subject scope (`ScopeError` for another subject's data), then runs and appends a
`TraceEvent` with a short result summary (ids, revisions, kinds, statuses, never full
payloads). Connector lookups are injected callables; the default raises `Unavailable`.
`lookup_settlement` ingests what the connector returns through `ingest_merchant_event` with
connector `merchant_lookup` so the passport keeps it, and returns the stored record.
`list_candidates` returns active records of the given kinds with `occurred_at` in
`[booked_at - window, booked_at + window)`.

## core/investigate_rules.py

```python
def run_investigation(request, store, gateway, mode="rules", *, now=None, window=timedelta(days=5)) -> InvestigationResult
```

The rules baseline. Deterministic matching by reference, then amount and date window; fixed
retrieval (always call `gateway.lookup_settlement` for the matched payout's `payment_ref`,
falling back to a settlement already in the passport); reconciliation in code; claim status
from explicit fields only. One claim per question. Links, claims and the trace are saved to
the store. `mode` other than `"rules"` raises `NotImplementedError`.
