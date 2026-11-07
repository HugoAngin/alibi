"""Alibi core contracts (pydantic v2).

Conventions
-----------
* Money is integer pence (minor units) with an ISO 4217 currency code. Floats, bools and
  numeric strings are rejected for money fields.
* Timestamps must carry a timezone. They are normalised to UTC and serialise as ISO-8601
  with a trailing ``Z``.
* Periods are half-open: ``[from, to)``.
* ``SourceTrust``, ``LinkState`` and ``ClaimStatus`` are three separate enums on purpose:
  a linked credit and a verified signature can coexist with an unresolved claim.
* Unknown fields are rejected (``extra="forbid"``), so padded or malformed payloads fail
  validation instead of being carried along.
* Merchant record amounts are non-negative magnitudes; the direction comes from ``kind``
  (order adds, refund and fee subtract). Bank amounts are signed from the holder's view
  (credit > 0, debit < 0).

Hash or sign ``model.to_json_dict()`` (see ``alibi.core.canon``). Signature checks always run
over the raw JSON object that was received, never over a re-serialised model.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    Strict,
    StringConstraints,
    model_validator,
)

SCHEMA_MERCHANT_EVENT = "alibi.merchant_event.v1"
SCHEMA_EVIDENCE = "alibi.evidence.v1"
SCHEMA_BANK_TXN = "alibi.bank_txn.v1"
SCHEMA_RFC = "alibi.rfc.v1"
SCHEMA_PACKAGE_BODY = "alibi.package_body.v1"
SCHEMA_PACKAGE = "alibi.package.v1"


# --------------------------------------------------------------------------- scalar types


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "timestamp needs a timezone; use UTC ISO-8601 such as 2026-11-03T10:00:00Z")
    return value.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    """Format a timezone-aware datetime as UTC ISO-8601 with a trailing Z."""
    value = _require_utc(value)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += f".{value.microsecond:06d}"
    return text + "Z"


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries a timezone, returning it in UTC."""
    return _require_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_require_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]
MAX_SAFE_INT = 2**53 - 1  # I-JSON / RFC 8785 safe integer range: every amount must hash
Pence = Annotated[int, Strict(), Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)]
NonNegativePence = Annotated[int, Strict(), Field(ge=0, le=MAX_SAFE_INT)]
PositiveInt = Annotated[int, Strict(), Field(ge=1)]
NonNegativeInt = Annotated[int, Strict(), Field(ge=0)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")]
Ref = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,63}$")]
Status = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
FieldPath = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class AlibiModel(BaseModel):
    """Base for every contract: strict about unknown fields, serialises by alias."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-ready dict: UTC 'Z' times, enum values, aliases, ``None`` fields omitted."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# --------------------------------------------------------------------------- enums


class RecordKind(StrEnum):
    ORDER = "order"
    PAYOUT = "payout"
    REFUND = "refund"
    FEE = "fee"
    SETTLEMENT = "settlement"
    RECEIPT = "receipt"
    MESSAGE = "message"


MONEY_KINDS = frozenset({
    RecordKind.ORDER, RecordKind.PAYOUT, RecordKind.REFUND, RecordKind.FEE,
    RecordKind.SETTLEMENT, RecordKind.RECEIPT,
})


class SourceTrust(StrEnum):
    """Where a source came from and how it was authenticated. Not a truth label."""

    ISSUER_VERIFIED = "issuer_verified"  # issuer signature checked against the pinned registry
    BANK_OBSERVED = "bank_observed"      # read from the authenticated bank feed
    SELF_SUPPLIED = "self_supplied"      # imported by the holder; no issuer attestation


class LinkState(StrEnum):
    """Whether a bank transaction is matched to a source record."""

    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    LINKED = "linked"


class ClaimStatus(StrEnum):
    """Whether evidence supports a stated claim. Contradicted: evidence conflicts with the
    claim. Unresolved: the necessary evidence is unavailable."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"


class DerivedState(StrEnum):
    """Lifecycle of derived results (links, claims). Corrections invalidate, never delete."""

    ACTIVE = "active"
    INVALIDATED = "invalidated"


class SigningDomain(StrEnum):
    RECORD = "alibi:record:v1"    # issuer-signed merchant records
    PACKAGE = "alibi:package:v1"  # Alibi-signed context packages
    REQUEST = "alibi:request:v1"  # bank-signed requests for context


class KeyPurpose(StrEnum):
    RECORD_SIGNING = "record_signing"
    PACKAGE_SIGNING = "package_signing"
    REQUEST_SIGNING = "request_signing"


class ApprovalStatus(StrEnum):
    PENDING = "pending"          # granted by the holder, not yet used to sign a package
    CONSUMED = "consumed"        # a package was signed under it
    INVALIDATED = "invalidated"  # body, recipient, policy or a source revision changed


class DraftStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    PACKAGED = "packaged"
    INVALIDATED = "invalidated"


class AlertKind(StrEnum):
    UNEXPLAINED_CREDIT = "unexplained_credit"
    UNUSUAL_INFLOW = "unusual_inflow"
    UNUSUAL_OUTFLOW = "unusual_outflow"
    AMOUNT_DISCREPANCY = "amount_discrepancy"
    SETTLEMENT_DISCREPANCY = "settlement_discrepancy"
    MERCHANT_UNVERIFIED = "merchant_unverified"


# --------------------------------------------------------------------------- shared pieces


class SignatureEnvelope(AlibiModel):
    """Detached signature: Ed25519 over ``domain`` + NUL + JCS(payload)."""

    alg: Literal["Ed25519"] = "Ed25519"
    kid: Ident
    domain: SigningDomain
    sig: SignatureHex


class EvidenceRef(AlibiModel):
    """A pinned pointer to one revision of a merchant record or bank transaction."""

    ref_type: Literal["record", "txn"]
    ref_id: Ident
    revision: PositiveInt

    def token(self) -> str:
        """Revision-free key, e.g. ``record:po_5001``."""
        return f"{self.ref_type}:{self.ref_id}"

    def key(self) -> str:
        """Revision-pinned key, e.g. ``record:po_5001@2``."""
        return f"{self.ref_type}:{self.ref_id}@{self.revision}"


class Period(AlibiModel):
    """Half-open UTC interval ``[from, to)``."""

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> "Period":
        if not self.from_ < self.to:
            raise ValueError("period 'from' must be strictly before 'to'")
        return self

    def contains(self, moment: datetime) -> bool:
        return self.from_ <= _require_utc(moment) < self.to


class ItemDetail(AlibiModel):
    """Optional line-item detail. Policy-scoped: item names are sensitive."""

    description: ShortText | None = None
    category: ShortText | None = None
    quantity: PositiveInt | None = None
    unit_amount_pence: NonNegativePence | None = None


# --------------------------------------------------------------------------- merchant records


class _RecordFields(AlibiModel):
    """Fields shared by an inbound merchant event and the stored evidence record."""

    event_id: Ident
    record_id: Ident
    revision: PositiveInt
    issuer_id: Ident
    subject_ref: Ident
    kind: RecordKind
    occurred_at: UtcDatetime
    effective_at: UtcDatetime | None = None   # e.g. when a settlement actually completed
    amount_pence: NonNegativePence | None = None
    currency: CurrencyCode | None = None
    payment_ref: Ref | None = None            # payout/transfer or buyer payment reference
    settlement_ref: Ref | None = None
    status: Status | None = None
    items: list[ItemDetail] | None = None
    text: LongText | None = None              # message or receipt text; always data, never instructions
    related_record_ids: list[Ident] = Field(default_factory=list)
    corrects_revision: PositiveInt | None = None
    correction_reason: ShortText | None = None

    @model_validator(mode="after")
    def _record_rules(self):
        if (self.amount_pence is None) != (self.currency is None):
            raise ValueError("amount_pence and currency must be given together")
        if self.kind in MONEY_KINDS and self.amount_pence is None:
            raise ValueError(f"{self.kind.value} records need amount_pence and currency")
        if self.corrects_revision is not None and self.corrects_revision >= self.revision:
            raise ValueError("corrects_revision must be lower than revision")
        if self.correction_reason is not None and self.corrects_revision is None:
            raise ValueError("correction_reason needs corrects_revision")
        return self


class MerchantEvent(_RecordFields):
    """What an issuer pushes (webhook) or returns (follow-up lookup). Signed separately."""

    schema_version: Literal["alibi.merchant_event.v1"] = SCHEMA_MERCHANT_EVENT


class SignedMerchantEvent(AlibiModel):
    """Wire wrapper: the event body plus a detached issuer signature envelope."""

    event: MerchantEvent
    signature: SignatureEnvelope | None = None


class EvidenceRecord(_RecordFields):
    """A merchant record as stored in the passport. ``source_trust`` is set by the backend."""

    schema_version: Literal["alibi.evidence.v1"] = SCHEMA_EVIDENCE
    source_trust: SourceTrust
    connector: Ident                       # merchant_webhook | merchant_lookup | self_supplied_import
    received_at: UtcDatetime
    payload_sha256: Sha256Hex              # sha256 of the canonical received payload
    issuer_signature: SignatureEnvelope | None = None
    signed_body: dict[str, Any] | None = None  # exact event JSON the issuer signed

    @model_validator(mode="after")
    def _trust_rules(self) -> "EvidenceRecord":
        if self.source_trust == SourceTrust.BANK_OBSERVED:
            raise ValueError("merchant evidence cannot be bank_observed")
        if self.source_trust == SourceTrust.ISSUER_VERIFIED:
            if self.issuer_signature is None or self.signed_body is None:
                raise ValueError("issuer_verified evidence needs issuer_signature and signed_body")
        if self.source_trust == SourceTrust.SELF_SUPPLIED:
            if self.issuer_signature is not None or self.signed_body is not None:
                raise ValueError("self_supplied evidence cannot carry an issuer signature")
        return self

    def ref(self) -> EvidenceRef:
        return EvidenceRef(ref_type="record", ref_id=self.record_id, revision=self.revision)

    def to_event(self) -> MerchantEvent:
        """The record's content as a MerchantEvent (not the raw signed bytes)."""
        names = set(_RecordFields.model_fields)
        return MerchantEvent.model_validate({n: getattr(self, n) for n in names})

    @classmethod
    def from_event(
        cls,
        event: MerchantEvent,
        *,
        source_trust: SourceTrust,
        connector: str,
        received_at: datetime,
        payload_sha256: str,
        issuer_signature: SignatureEnvelope | None = None,
        signed_body: dict[str, Any] | None = None,
    ) -> "EvidenceRecord":
        names = set(_RecordFields.model_fields)
        data = {n: getattr(event, n) for n in names}
        return cls.model_validate({
            **data,
            "source_trust": source_trust,
            "connector": connector,
            "received_at": received_at,
            "payload_sha256": payload_sha256,
            "issuer_signature": issuer_signature,
            "signed_body": signed_body,
        })


# --------------------------------------------------------------------------- bank feed


class Provenance(AlibiModel):
    connector: Ident          # e.g. bank_feed
    connection_id: Ident      # e.g. conn_bank_a_maya
    retrieved_at: UtcDatetime
    source_version: ShortText  # feed/API version or revision marker
    cursor: ShortText | None = None


class BankTxn(AlibiModel):
    """A booked bank transaction. ``amount_pence`` is signed from the holder's view."""

    schema_version: Literal["alibi.bank_txn.v1"] = SCHEMA_BANK_TXN
    txn_id: Ident
    revision: PositiveInt
    subject_ref: Ident        # account-scoped subject, set by the bank connector
    account_ref: Ident
    amount_pence: Pence
    currency: CurrencyCode
    booked_at: UtcDatetime
    description: ShortText
    refs: list[Ref] = Field(default_factory=list)
    provenance: Provenance
    source_trust: Literal[SourceTrust.BANK_OBSERVED] = SourceTrust.BANK_OBSERVED

    @model_validator(mode="after")
    def _non_zero(self) -> "BankTxn":
        if self.amount_pence == 0:
            raise ValueError("bank amount_pence cannot be zero")
        return self

    @property
    def is_credit(self) -> bool:
        return self.amount_pence > 0

    def ref(self) -> EvidenceRef:
        return EvidenceRef(ref_type="txn", ref_id=self.txn_id, revision=self.revision)


# --------------------------------------------------------------------------- requests


class Question(AlibiModel):
    question_id: Ident
    text: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class RequestForContext(AlibiModel):
    """A bank's Request for Context. The audience is bound to the authenticated issuer."""

    schema_version: Literal["alibi.rfc.v1"] = SCHEMA_RFC
    issuer: Ident
    request_id: Ident
    parent_request_id: Ident | None = None
    subject_ref: Ident
    audience: Ident
    period: Period | None = None
    txn_ids: list[Ident] | None = None
    questions: list[Question] = Field(min_length=1, max_length=20)
    issued_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _request_rules(self) -> "RequestForContext":
        if self.period is None and not self.txn_ids:
            raise ValueError("a request needs a period or txn_ids")
        if self.txn_ids is not None and len(set(self.txn_ids)) != len(self.txn_ids):
            raise ValueError("txn_ids must be unique")
        ids = [q.question_id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question_id values must be unique")
        if not self.issued_at < self.expires_at:
            raise ValueError("expires_at must be after issued_at")
        if self.parent_request_id is not None and self.parent_request_id == self.request_id:
            raise ValueError("a follow-up needs a new request_id")
        return self


class SignedRequest(AlibiModel):
    request: RequestForContext
    signature: SignatureEnvelope | None = None


# --------------------------------------------------------------------------- derived results


class Link(AlibiModel):
    """Link state for one bank transaction revision."""

    link_id: Ident
    txn_ref: EvidenceRef
    link_state: LinkState
    record_ref: EvidenceRef | None = None      # the linked source when linked
    candidates: list[EvidenceRef] = Field(default_factory=list)
    method: Ident | None = None                # reference | amount_date | agent | manual
    signals: list[ShortText] = Field(default_factory=list)
    run_id: Ident | None = None
    created_at: UtcDatetime
    state: DerivedState = DerivedState.ACTIVE
    invalidated_reason: ShortText | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Link":
        if self.txn_ref.ref_type != "txn":
            raise ValueError("txn_ref must point at a bank transaction")
        if self.link_state == LinkState.LINKED and self.record_ref is None:
            raise ValueError("a linked transaction needs record_ref")
        if self.link_state != LinkState.LINKED and self.record_ref is not None:
            raise ValueError("only a linked transaction has record_ref")
        if self.link_state == LinkState.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("an ambiguous link needs at least two candidates")
        return self


class CalcTerm(AlibiModel):
    ref: EvidenceRef
    kind: Ident               # order | refund | fee | payout | settlement | bank_credit ...
    sign: Literal[1, -1]
    amount_pence: NonNegativePence
    currency: CurrencyCode

    @property
    def signed_pence(self) -> int:
        return self.sign * self.amount_pence


class TimingCheck(AlibiModel):
    """Whether settlement evidence was effective by the bank credit's booking time."""

    settlement_ref: EvidenceRef
    credit_ref: EvidenceRef
    settlement_status: Status | None = None
    settlement_effective_at: UtcDatetime | None = None
    status_observed_at: UtcDatetime
    booked_at: UtcDatetime
    ok: bool
    reason: ShortText


class Calculation(AlibiModel):
    """Arithmetic done in code from cited amounts. The validator re-checks the sums."""

    method: Ident
    currency: CurrencyCode
    terms: list[CalcTerm] = Field(default_factory=list)
    total_pence: Pence
    target_ref: EvidenceRef | None = None
    target_pence: Pence | None = None
    difference_pence: Pence | None = None      # total_pence - target_pence
    balanced: bool | None = None
    timing: TimingCheck | None = None
    cited_refs: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _arithmetic(self) -> "Calculation":
        if any(t.currency != self.currency for t in self.terms):
            raise ValueError("calculation terms must share one currency")
        keys = [t.ref.key() for t in self.terms]
        if len(set(keys)) != len(keys):
            raise ValueError("a calculation cannot count the same evidence twice")
        if sum(t.signed_pence for t in self.terms) != self.total_pence:
            raise ValueError("total_pence does not equal the sum of the cited terms")
        if self.target_pence is None:
            if self.difference_pence is not None or self.balanced is not None:
                raise ValueError("difference_pence and balanced need target_pence")
        else:
            if self.difference_pence != self.total_pence - self.target_pence:
                raise ValueError("difference_pence must equal total_pence - target_pence")
            if self.balanced is not None and self.balanced != (self.difference_pence == 0):
                raise ValueError("balanced must reflect difference_pence == 0")
        return self


class Claim(AlibiModel):
    """One answer to one RFC question, with its evidence and status."""

    claim_id: Ident
    request_id: Ident
    question_id: Ident
    statement: LongText
    status: ClaimStatus
    rationale: LongText
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    calculation: Calculation | None = None
    gap: LongText | None = None
    run_id: Ident | None = None
    created_at: UtcDatetime
    state: DerivedState = DerivedState.ACTIVE
    invalidated_reason: ShortText | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> "Claim":
        if self.status in (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED) and not self.evidence_refs:
            raise ValueError(f"a {self.status.value} claim must cite evidence")
        if self.status == ClaimStatus.UNRESOLVED and not self.gap:
            raise ValueError("an unresolved claim must state the gap")
        return self


class Alert(AlibiModel):
    alert_id: Ident
    subject_ref: Ident
    kind: AlertKind
    severity: Literal["info", "warning"] = "info"
    title: ShortText
    explanation: LongText
    refs: list[EvidenceRef] = Field(default_factory=list)
    suggested_evidence: list[ShortText] = Field(default_factory=list)
    created_at: UtcDatetime
    state: Literal["open", "resolved", "invalidated"] = "open"


# --------------------------------------------------------------------------- policy


class _RuleBase(AlibiModel):
    rule_id: Ident
    source_text: LongText | None = None
    kinds: list[RecordKind] = Field(default_factory=list)  # empty = all kinds


class DenyRule(_RuleBase):
    """Never disclose these fields. ``kinds`` scopes the rule; with no ``fields``, whole
    records of those kinds are excluded. Deny wins over every other rule."""

    type: Literal["deny"] = "deny"
    fields: list[FieldPath] = Field(default_factory=list)

    @model_validator(mode="after")
    def _target(self) -> "DenyRule":
        if not self.fields and not self.kinds:
            raise ValueError("a deny rule needs fields or kinds")
        return self


class GeneraliseRule(_RuleBase):
    """Disclose a coarser form of these fields (item name -> category, time -> day/month)."""

    type: Literal["generalise"] = "generalise"
    fields: list[FieldPath] = Field(min_length=1)
    level: Literal["category", "day", "month", "redact"] = "category"


class AgeRule(_RuleBase):
    """Disclose nothing that occurred more than ``max_age_days`` before the build time."""

    type: Literal["age"] = "age"
    max_age_days: Annotated[int, Strict(), Field(ge=1, le=3650)]


class RecipientRule(_RuleBase):
    """Disclose only to these audiences."""

    type: Literal["recipient"] = "recipient"
    allow: list[Ident] = Field(min_length=1)


PolicyRule = Annotated[
    Union[DenyRule, GeneraliseRule, AgeRule, RecipientRule], Field(discriminator="type")
]


class Policy(AlibiModel):
    policy_id: Ident
    version: PositiveInt
    subject_ref: Ident
    rules: list[PolicyRule] = Field(default_factory=list)
    confirmed: bool = False      # holder confirmed the interpretation of every rule
    created_at: UtcDatetime

    def version_tag(self) -> str:
        return f"{self.policy_id}@v{self.version}"


# --------------------------------------------------------------------------- review, approval, package


class ReviewEntry(AlibiModel):
    ref: EvidenceRef
    field: FieldPath | None = None
    reason: ShortText
    rule_id: Ident | None = None


class Gap(AlibiModel):
    question_id: Ident
    text: LongText


class ClaimChange(AlibiModel):
    question_id: Ident
    from_status: ClaimStatus
    to_status: ClaimStatus
    reason: ShortText


class Review(AlibiModel):
    """Private holder-side review of a draft. Never sent to a recipient."""

    request_id: Ident
    audience: Ident
    included: list[ReviewEntry] = Field(default_factory=list)
    excluded: list[ReviewEntry] = Field(default_factory=list)
    generalised: list[ReviewEntry] = Field(default_factory=list)
    withheld_fields: list[ReviewEntry] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    claim_changes: list[ClaimChange] = Field(default_factory=list)


class Attachment(AlibiModel):
    """Evidence disclosed in a package.

    ``full_record``: the exact issuer-signed event body plus its issuer signature, used only
    when every field is approved. ``redacted_extract``: allowlisted fields, attested only by
    the package signature (the issuer signature cannot cover a partial record).
    """

    attachment_id: Ident
    ref: EvidenceRef
    form: Literal["full_record", "redacted_extract"]
    source_trust: SourceTrust
    issuer_id: Ident | None = None
    content: dict[str, Any]
    issuer_signature: SignatureEnvelope | None = None
    generalised_fields: list[FieldPath] = Field(default_factory=list)

    @model_validator(mode="after")
    def _form_rules(self) -> "Attachment":
        if self.form == "redacted_extract" and self.issuer_signature is not None:
            raise ValueError("a redacted extract cannot carry the issuer signature")
        if self.form == "full_record" and self.source_trust == SourceTrust.ISSUER_VERIFIED \
                and self.issuer_signature is None:
            raise ValueError("an issuer_verified full record needs its issuer signature")
        if self.form == "full_record" and self.generalised_fields:
            raise ValueError("a full record cannot have generalised fields")
        return self


class PackageClaim(AlibiModel):
    question_id: Ident
    statement: LongText
    status: ClaimStatus
    rationale: LongText
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    calculation: Calculation | None = None
    gap: LongText | None = None


class PackageScope(AlibiModel):
    period: Period | None = None
    txn_ids: list[Ident] | None = None


class PackageBody(AlibiModel):
    """The exact content the holder approves. Approval binds sha256(JCS(body))."""

    schema_version: Literal["alibi.package_body.v1"] = SCHEMA_PACKAGE_BODY
    request_id: Ident
    parent_request_id: Ident | None = None
    audience: Ident
    case_ref: Ident
    scope: PackageScope
    questions: list[Question]
    claims: list[PackageClaim]
    attachments: list[Attachment] = Field(default_factory=list)
    expires_at: UtcDatetime


class ContextPackage(AlibiModel):
    """Signed by the Alibi key (domain alibi:package:v1) over every field but ``signature``."""

    schema_version: Literal["alibi.package.v1"] = SCHEMA_PACKAGE
    package_id: Ident
    issued_at: UtcDatetime
    body: PackageBody
    body_sha256: Sha256Hex
    signature: SignatureEnvelope


class Draft(AlibiModel):
    draft_id: Ident
    request_id: Ident
    audience: Ident
    body: PackageBody
    body_sha256: Sha256Hex
    review: Review
    policy_version: ShortText
    source_revisions: list[EvidenceRef] = Field(default_factory=list)
    status: DraftStatus = DraftStatus.PENDING
    created_at: UtcDatetime
    invalidated_reason: ShortText | None = None


class Approval(AlibiModel):
    """A holder decision bound to one exact body, audience, request, policy and sources."""

    approval_id: Ident
    request_id: Ident
    audience: Ident
    body_sha256: Sha256Hex
    policy_version: ShortText
    source_revisions: list[EvidenceRef] = Field(default_factory=list)
    holder_id: Ident
    draft_id: Ident | None = None
    approved_at: UtcDatetime
    expires_at: UtcDatetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    package_id: Ident | None = None
    invalidated_reason: ShortText | None = None

    @model_validator(mode="after")
    def _window(self) -> "Approval":
        if not self.approved_at < self.expires_at:
            raise ValueError("approval expires_at must be after approved_at")
        return self


class Delivery(AlibiModel):
    delivery_id: Ident
    package_id: Ident
    request_id: Ident
    audience: Ident
    package_sha256: Sha256Hex
    delivered_at: UtcDatetime
    status: Literal["sent", "acknowledged", "failed"] = "sent"
    receipt_id: Ident | None = None


class Receipt(AlibiModel):
    """Recipient-side acknowledgement bound to a package ID and its digest."""

    receipt_id: Ident
    package_id: Ident
    package_sha256: Sha256Hex
    recipient: Ident
    received_at: UtcDatetime
    accepted: bool
    reasons: list[ShortText] = Field(default_factory=list)


# --------------------------------------------------------------------------- verification


class AuthResult(AlibiModel):
    """Package authentication only. Says nothing about whether claims are true."""

    valid: bool
    package_id: Ident | None = None
    audience: Ident | None = None
    kid: Ident | None = None
    reasons: list[ShortText] = Field(default_factory=list)


class SourceTrustResult(AlibiModel):
    attachment_id: Ident
    ref: EvidenceRef
    form: Literal["full_record", "redacted_extract"]
    claimed_trust: SourceTrust
    attestation: Literal["issuer_signature", "package_signer"]
    signature_valid: bool | None = None   # None when no issuer signature was claimed
    lower_trust: bool
    note: ShortText


class ClaimResult(AlibiModel):
    question_id: Ident
    status: ClaimStatus
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    calculation_valid: bool | None = None
    gap: LongText | None = None


class VerificationResult(AlibiModel):
    """Three separate answers: package authentication, per-source trust, claim statuses."""

    accepted: bool
    package_sha256: Sha256Hex | None = None
    authentication: AuthResult
    sources: list[SourceTrustResult] = Field(default_factory=list)
    claims: list[ClaimResult] = Field(default_factory=list)
    errors: list[ShortText] = Field(default_factory=list)


# --------------------------------------------------------------------------- trace and audit


class TraceEvent(AlibiModel):
    """One step of an investigation trace. Holder-side; never sent to a bank as-is."""

    trace_id: Ident
    run_id: Ident
    seq: NonNegativeInt
    at: UtcDatetime
    actor: Ident                 # rules | gateway | matcher | detective | challenger | curator
    step: Literal["tool_call", "validation", "calculation", "status_change", "refusal", "note"]
    tool: Ident | None = None
    request_id: Ident | None = None
    question_id: Ident | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    refs: list[EvidenceRef] = Field(default_factory=list)
    outcome: Literal["ok", "refused", "unavailable", "error", "budget_exceeded", "not_found"] = "ok"
    reason: ShortText | None = None


class AuditEvent(AlibiModel):
    audit_id: Ident
    at: UtcDatetime
    actor: Ident
    action: Ident
    target_id: Ident | None = None
    outcome: Literal["ok", "refused", "error", "duplicate"]
    detail: ShortText | None = None


class InvestigationResult(AlibiModel):
    run_id: Ident
    request_id: Ident
    mode: Literal["rules", "agents", "single_call"]
    links: list[Link] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    calls_used: NonNegativeInt = 0
    unavailable: list[ShortText] = Field(default_factory=list)
