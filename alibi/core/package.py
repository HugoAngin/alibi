"""Context packages: build the filtered body, wrap it as a draft, sign it, verify it.

``build_body``
    Default-minimum and policy-filtered. Only evidence some claim cites (evidence refs or
    calculation refs) is considered; anything else goes to the private review as "not needed
    to answer the questions". Each considered source is filtered twice, first to find what is
    left out and then again with ``excluded_ids`` so references to records that are not in
    the package are withheld. A source goes out as a ``full_record`` (the exact signed body
    with its issuer signature) only when ``FilterResult.full_record_ok``; otherwise as a
    ``redacted_extract`` of allowlisted fields, attested by the package signer. Claims keep
    only disclosed refs, plus the request's own transactions, which the recipient already
    holds. A claim that lost cited evidence, or whose calculation cannot be shown from what is
    disclosed, is answered as unresolved with generic text and the review records the change.
    Last, a forbidden-content scan replaces withheld text found in any recipient-facing string
    (statements, rationales, gaps, calculation text, extract content) with ``[withheld]``.
    Withheld identifiers match only as whole tokens (``ord_100`` never inside ``ord_1001``),
    an extract's own identity fields are never rewritten, and the words for every category
    the policy keeps from this recipient are withheld whether or not a claim cites one.
``sign_package``
    Revalidates the approval, then signs ``{schema_version, package_id, issued_at, body,
    body_sha256}`` with the Alibi key in domain ``alibi:package:v1``.
``verify_package``
    Recipient-side checks with three separate results: package authentication, per-source
    trust and claim statuses. It never raises on bad input. A source or answer that appears
    twice, or full records about different subjects, reject the package.
``case_binding``
    The body's ``case_ref`` is a recipient-scoped subject binding: sha256 over
    (``alibi:case:v1``, audience, root request id, subject_ref). Every package about one case
    for one recipient shares it, it names the subject without disclosing it, and a verifier
    that knows its own request's subject recomputes it, so a package whose attachments are
    all redacted extracts (subject withheld) still cannot verify for another subject.
"""

import copy
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from alibi.core import canon
from alibi.core.approval import (
    ApprovalError,
    body_refs,
    calculation_refs,
    check_approval,
    merge_refs,
)
from alibi.core.policy import REASON_EXCLUDED_REF, CompiledPolicy, FilterResult, compile_policy
from alibi.core.reconcile import NET_METHODS, SETTLED_STATUSES, allowed_signs
from alibi.core.schemas import (
    SCHEMA_PACKAGE,
    Attachment,
    AuthResult,
    BankTxn,
    CalcTerm,
    Calculation,
    Claim,
    ClaimChange,
    ClaimResult,
    ClaimStatus,
    ContextPackage,
    DenyRule,
    DerivedState,
    Draft,
    EvidenceRecord,
    EvidenceRef,
    Gap,
    PackageBody,
    PackageClaim,
    PackageScope,
    RecipientRule,
    RecordKind,
    RequestForContext,
    Review,
    ReviewEntry,
    SigningDomain,
    SourceTrust,
    SourceTrustResult,
    TimingCheck,
    VerificationResult,
    format_utc,
    parse_utc,
    utc_now,
)
from alibi.core.signing import SignatureError, verify

__all__ = [
    "DEFAULT_PACKAGE_TTL",
    "GAP_NOT_SHARED",
    "GAP_NO_ANSWER",
    "GAP_WITHHELD",
    "REASON_NOT_NEEDED",
    "REASON_NOT_PROVIDED",
    "REASON_REMOVED",
    "REASON_UNUSED",
    "WITHHELD",
    "PackageError",
    "build_body",
    "case_binding",
    "cited_refs",
    "make_draft",
    "package_signing_payload",
    "sign_package",
    "verify_package",
]

DEFAULT_PACKAGE_TTL = timedelta(days=7)
CLOCK_SKEW = timedelta(minutes=5)   # tolerated when checking that issued_at is not in the future
WITHHELD = "[withheld]"
MIN_FORBIDDEN_LEN = 4               # shorter withheld values are too common to scan for

REASON_NOT_NEEDED = "not needed to answer the questions"
REASON_REMOVED = "removed by the account holder"
REASON_NOT_PROVIDED = "cited by a claim but not provided to the package"
REASON_UNUSED = "not needed once the answer citing it could not be shared"
REASON_OTHER_REVISION = "the claims cite a different revision of this source"
REASON_OTHER_SUBJECT = "belongs to a different subject than the request"

# Recipient-facing generic text. It never names a withheld category, merchant or record.
GAP_WITHHELD = "Some supporting evidence was withheld at the account holder's request."
GAP_NOT_SHARED = "No evidence is shared with this recipient at the account holder's request."
GAP_NO_ANSWER = "No answer to this question was prepared from the available evidence."
STATEMENT_UNANSWERED = "This question is not answered in this package."
RATIONALE_WITHHELD = ("The evidence needed to support an answer was withheld at the account "
                      "holder's request, so the answer is left unresolved.")
RATIONALE_NOT_SHARED = "The account holder does not share evidence with this recipient."
RATIONALE_NO_ANSWER = "No claim was prepared for this question."

_SIGNED_FIELDS = ("schema_version", "package_id", "issued_at", "body", "body_sha256")
CASE_BINDING_DOMAIN = "alibi:case:v1"   # separates case_ref from every other digest
# String fields of a source left out of the package that must not resurface in any text.
_EXCLUDED_RECORD_FIELDS = ("record_id", "event_id", "issuer_id", "payment_ref", "settlement_ref",
                           "text", "correction_reason", "items")
_EXCLUDED_TXN_FIELDS = ("txn_id", "description", "refs", "account_ref")
# Fields holding identifiers. Their withheld values match only as whole tokens, so a withheld
# "ord_100" or "PO-500" never matches inside a shared "ord_1001" or "PO-5001".
_ID_FIELDS = frozenset({"record_id", "event_id", "issuer_id", "payment_ref", "settlement_ref",
                        "related_record_ids", "txn_id", "refs", "account_ref"})
# An extract's own identity repeats its attachment's ref and issuer, which the body shows
# anyway, so the scan never rewrites these keys (the verifier checks them against the ref).
_IDENTITY_KEYS = frozenset({"record_id", "txn_id", "revision", "issuer_id"})
# Other words that name a record kind ("sales" names the order category).
_KIND_SYNONYMS: dict[RecordKind, tuple[str, ...]] = {RecordKind.ORDER: ("sale",)}

Evidence = EvidenceRecord | BankTxn


class PackageError(ValueError):
    """Invalid input to build, draft or sign a package. The message is safe to show."""


# --------------------------------------------------------------------------- small helpers


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise PackageError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _summary(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "value"
    return f"{where}: {first.get('msg', 'invalid value')}"


def _strings(value: Any) -> list[str]:
    """Every string leaf inside a JSON value (dict values, list items)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item)]
    return []


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _same(value: Any, expected: Any) -> bool:
    return type(value) is type(expected) and value == expected


def _exact_time(value: Any) -> datetime | None:
    """A full UTC timestamp from JSON text; None for a date, a month or anything else."""
    if not isinstance(value, str):
        return None
    try:
        return parse_utc(value)
    except ValueError:
        return None


def _entry(ref: EvidenceRef, reason: str, *, field: str | None = None,
           rule_id: str | None = None) -> ReviewEntry:
    try:
        return ReviewEntry(ref=ref, field=field, reason=_short(reason, 300), rule_id=rule_id)
    except ValidationError:  # a field path the schema cannot carry: name it in the reason
        label = f"{field}: {reason}" if field else reason
        return ReviewEntry(ref=ref, reason=_short(label, 300))


def _claim_refs(claim: Claim) -> list[EvidenceRef]:
    return merge_refs(claim.evidence_refs, calculation_refs(claim.calculation))


def cited_refs(claims: Iterable[Claim], *, request_id: str | None = None,
               audience: str | None = None) -> list[EvidenceRef]:
    """Every ref the active claims cite (evidence and calculation refs), sorted by key.

    ``request_id`` and ``audience`` (both, since request ids are per bank) keep only one
    request's claims. This is the disclosure set, not the complete dependency set: a draft
    must also retain each claim's private ``examined_refs`` through ``store.dependency_refs``.
    """
    refs: list[EvidenceRef] = []
    for claim in claims:
        if request_id is not None and claim.request_id != request_id:
            continue
        if audience is not None and claim.audience != audience:
            continue
        if DerivedState(claim.state) == DerivedState.ACTIVE:
            refs += _claim_refs(claim)
    return merge_refs(refs)


# --------------------------------------------------------------------------- building blocks


def _unique_evidence(evidence: Iterable[Evidence]) -> dict[str, Evidence]:
    """Evidence by revision-pinned key, sorted by key; the first copy of a key wins."""
    found: dict[str, Evidence] = {}
    for item in evidence:
        if not isinstance(item, (EvidenceRecord, BankTxn)):
            raise PackageError("evidence must be EvidenceRecord or BankTxn, not "
                               f"{type(item).__name__}")
        found.setdefault(item.ref().key(), item)
    return {key: found[key] for key in sorted(found)}


def _pick_claims(request: RequestForContext, claims: Iterable[Claim]) -> dict[str, Claim]:
    """The newest active claim per question of this request."""
    questions = {q.question_id for q in request.questions}
    best: dict[str, Claim] = {}
    for claim in claims:
        if not isinstance(claim, Claim):
            try:
                claim = Claim.model_validate(claim)
            except ValidationError as exc:
                raise PackageError(f"invalid claim ({_summary(exc)})") from exc
        if (claim.request_id != request.request_id or claim.audience != request.audience
                or claim.question_id not in questions
                or DerivedState(claim.state) != DerivedState.ACTIVE):
            continue
        current = best.get(claim.question_id)
        if current is None or (claim.created_at, claim.claim_id) > (current.created_at,
                                                                     current.claim_id):
            best[claim.question_id] = claim
    return best


def _needed(chosen: dict[str, Claim]) -> dict[str, list[str]]:
    """Ref key -> ids of the questions whose claim cites it."""
    needed: dict[str, set[str]] = {}
    for question_id, claim in chosen.items():
        for ref in _claim_refs(claim):
            needed.setdefault(ref.key(), set()).add(question_id)
    return {key: sorted(ids) for key, ids in needed.items()}


def _filter(policy: CompiledPolicy, item: Evidence, audience: str, now: datetime,
            excluded_ids: Collection[str] = ()) -> FilterResult:
    if isinstance(item, BankTxn):
        return policy.filter_txn(item, audience=audience, now=now)
    return policy.filter_record(item, audience=audience, now=now, excluded_ids=excluded_ids)


def _attachment_id(request_id: str, ref: EvidenceRef) -> str:
    return canon.make_id("att", request_id, ref.key())


def _full_record(request_id: str, record: EvidenceRecord) -> Attachment:
    return Attachment(
        attachment_id=_attachment_id(request_id, record.ref()), ref=record.ref(),
        form="full_record", source_trust=record.source_trust, issuer_id=record.issuer_id,
        content=copy.deepcopy(record.signed_body), issuer_signature=record.issuer_signature)


def _extract(request_id: str, item: Evidence, result: FilterResult) -> Attachment:
    issuer = (item.issuer_id if isinstance(item, EvidenceRecord) and "issuer_id" in result.fields
              else None)  # an extract that withholds issuer_id names no issuer either
    return Attachment(
        attachment_id=_attachment_id(request_id, item.ref()), ref=item.ref(),
        form="redacted_extract", source_trust=item.source_trust, issuer_id=issuer,
        content=copy.deepcopy(result.fields),
        generalised_fields=sorted({path for path, _, _ in result.generalised}))


def _attach(request_id: str, item: Evidence, result: FilterResult) -> Attachment:
    """The full signed record when the policy allows every field, else an extract."""
    if isinstance(item, EvidenceRecord) and result.full_record_ok:
        return _full_record(request_id, item)
    return _extract(request_id, item, result)


def case_binding(audience: str, root_request_id: str, subject_ref: str) -> str:
    """The ``case_ref`` of every package about ``root_request_id`` (a request, or the parent
    of a follow-up) for ``audience``: ``case_`` + sha256 over the domain, audience, request
    and subject. A recipient recomputes it from its own request to check the subject; it
    cannot be inverted to the subject by anyone who does not already know it."""
    return "case_" + canon.digest([CASE_BINDING_DOMAIN, audience, root_request_id, subject_ref])


def _body(request: RequestForContext, claims: list[PackageClaim], attachments: list[Attachment],
          expires_at: datetime) -> PackageBody:
    return PackageBody(
        request_id=request.request_id, parent_request_id=request.parent_request_id,
        audience=request.audience,
        case_ref=case_binding(request.audience, request.parent_request_id or request.request_id,
                              request.subject_ref),
        scope=PackageScope(period=request.period, txn_ids=request.txn_ids),
        questions=list(request.questions), claims=claims, attachments=attachments,
        expires_at=expires_at)


def _generic(question_id: str, rationale: str, gap: str,
             evidence_refs: Iterable[EvidenceRef] = ()) -> PackageClaim:
    return PackageClaim(question_id=question_id, statement=STATEMENT_UNANSWERED,
                        status=ClaimStatus.UNRESOLVED, rationale=rationale,
                        evidence_refs=list(evidence_refs), gap=gap)


def _generalised_reason(path: str, how: str) -> str:
    if how == "category":
        return "item name replaced by its category"
    if how in ("day", "month"):
        return f"shown to the {how} only"
    return f"generalised ({how})"


# --------------------------------------------------------------------------- forbidden content


def _field_strings(data: dict[str, Any], path: str, reason: str,
                   excluded_ids: Collection[str]) -> list[str]:
    """String values a withheld or generalised field held in the source (not the extract)."""
    top, _, sub = path.partition(".")
    if top == "items":
        items = data.get("items") or []
        return [s for item in items if isinstance(item, dict)
                for s in _strings(item.get(sub) if sub else item)]
    if top == "related_record_ids" and reason == REASON_EXCLUDED_REF:
        return [rid for rid in data.get("related_record_ids") or [] if rid in excluded_ids]
    return _strings(data.get(top))


def _forbidden_values(attached: list[tuple[Evidence, FilterResult]], excluded: list[Evidence],
                      excluded_ids: Collection[str],
                      held: Collection[str]) -> dict[str, tuple[str, EvidenceRef, str | None]]:
    """Lower-cased withheld string -> (value, source ref, field), first source wins.

    Withheld and generalised fields of attached sources, read from the source itself, and
    the identifying text (ids, issuer, references, free text, item detail) of sources left
    out. Transactions the recipient already holds contribute nothing.
    """
    out: dict[str, tuple[str, EvidenceRef, str | None]] = {}

    def add(values: Iterable[str], ref: EvidenceRef, field: str | None) -> None:
        for value in values:
            out.setdefault(value.lower(), (value, ref, field))

    for item, result in attached:
        if item.ref().token() in held:
            continue
        data = item.to_json_dict()
        for path, reason, _ in [*result.withheld, *result.generalised]:
            add(_field_strings(data, path, reason, excluded_ids), item.ref(), path)
    for item in excluded:
        if item.ref().token() in held:
            continue
        data = item.to_json_dict()
        names = _EXCLUDED_TXN_FIELDS if isinstance(item, BankTxn) else _EXCLUDED_RECORD_FIELDS
        for name in names:
            add(_strings(data.get(name)), item.ref(), name)
    return out


def _disclosed_strings(attachments: Iterable[Attachment], request: RequestForContext) -> set[str]:
    """Lower-cased strings the package discloses anyway, or the recipient already knows."""
    values: set[str] = set()
    for attachment in attachments:
        values.update(s.lower() for s in _strings(attachment.content))
        if attachment.issuer_id:
            values.add(attachment.issuer_id.lower())
    for value in (request.request_id, request.parent_request_id, request.audience,
                  request.subject_ref, request.issuer, *(request.txn_ids or [])):
        if value:
            values.add(value.lower())
    return values


def _withheld_kinds(
        policy: CompiledPolicy, request: RequestForContext,
        items: Mapping[str, Evidence]) -> dict[str, tuple[str, EvidenceRef | None, str]]:
    """Words for every category the policy keeps from this recipient, whatever was cited.

    A deny rule without fields, or a kind-scoped recipient rule that blocks this audience,
    keeps a whole kind out; naming it ("refunds", or "sales" for orders) would identify the
    withheld category, so its words are withheld too. Each word is reported against a record
    of that kind when the evidence holds one (else the text is still scrubbed, unreported).
    """
    kinds: set[RecordKind] = set()
    for rule in policy.rules:
        if isinstance(rule, DenyRule) and not rule.fields:
            kinds.update(rule.kinds)
        elif isinstance(rule, RecipientRule) and rule.kinds and request.audience not in rule.allow:
            kinds.update(rule.kinds)
    out: dict[str, tuple[str, EvidenceRef | None, str]] = {}
    for kind in sorted(kinds):
        source = next((item.ref() for item in items.values()
                       if isinstance(item, EvidenceRecord) and item.kind == kind
                       and item.subject_ref == request.subject_ref), None)
        for word in (kind.value, *_KIND_SYNONYMS.get(kind, ())):
            out.setdefault(word, (word, source, "kind"))
    return out


def _alternative(value: str, field: str | None) -> str:
    """The pattern for one withheld value: a kind word with any suffix, an identifier as a
    whole token (no word character either side), anything else as a substring."""
    escaped = re.escape(value)
    if field == "kind":
        return rf"\b{escaped}\w*"
    if field in _ID_FIELDS:
        start = r"(?<!\w)" if re.match(r"\w", value[0]) else ""
        end = r"(?!\w)" if re.match(r"\w", value[-1]) else ""
        return f"{start}{escaped}{end}"
    return escaped


class _Scrubber:
    """Replaces withheld strings (case-insensitive) with ``[withheld]``.

    Entries whose field is ``kind`` are words: they match at a word start with any suffix,
    so "refund" also removes "Refunds" and "refunded". Identifiers match as whole tokens;
    other text matches anywhere.
    """

    def __init__(self, forbidden: dict[str, tuple[str, EvidenceRef | None, str | None]]):
        self.forbidden = forbidden
        self.words = {low for low, (_, _, field) in forbidden.items() if field == "kind"}
        values = sorted(forbidden.values(), key=lambda src: (-len(src[0]), src[0]))
        alternatives = [_alternative(v, field) for v, _, field in values]
        self.pattern = re.compile("|".join(alternatives), re.IGNORECASE) if values else None
        self.hits: dict[tuple[str, str, str], ReviewEntry] = {}

    def _source(self, matched: str) -> tuple[str, EvidenceRef | None, str | None] | None:
        low = matched.lower()
        if low in self.forbidden:
            return self.forbidden[low]
        word = next((w for w in sorted(self.words, key=len, reverse=True) if low.startswith(w)),
                    None)
        return self.forbidden[word] if word else None

    def text(self, value: str, where: str, limit: int | None = None) -> str:
        if self.pattern is None or not isinstance(value, str):
            return value

        def replace(match: re.Match) -> str:
            source = self._source(match.group(0))
            if source is not None and source[1] is not None:
                _, ref, field = source
                self.hits.setdefault((ref.key(), field or "", where), _entry(
                    ref, f"withheld text removed from {where}", field=field))
            return WITHHELD

        out = self.pattern.sub(replace, value)
        return _short(out, limit) if limit else out

    def matches(self, value: Any) -> bool:
        return self.pattern is not None and any(self.pattern.search(s) for s in _strings(value))

    def tree(self, value: Any, where: str) -> Any:
        if isinstance(value, str):
            return self.text(value, where)
        if isinstance(value, dict):
            return {key: self.tree(item, where) for key, item in value.items()}
        if isinstance(value, list):
            return [self.tree(item, where) for item in value]
        return value

    def entries(self) -> list[ReviewEntry]:
        return [self.hits[key] for key in sorted(self.hits)]


def _scrub_calculation(calc: Calculation | None, scrubber: _Scrubber,
                       question_id: str) -> Calculation | None:
    if calc is None:
        return None
    data = calc.to_json_dict()
    where = f"{question_id} calculation"
    if scrubber.text(data["method"], where) != data["method"]:
        data["method"] = "withheld"  # the method is an identifier, so it cannot be partly masked
    if "timing" in data:
        data["timing"]["reason"] = scrubber.text(data["timing"]["reason"], where, limit=300)
    return Calculation.model_validate(data)


# --------------------------------------------------------------------------- calculation checks


def _term_check(term: CalcTerm, contents: Mapping[str, Any],
                held: Collection[str], *, method: str) -> bool | None:
    content = contents.get(term.ref.key())
    if not isinstance(content, Mapping):
        return True if term.ref.token() in held else None
    amount, currency = content.get("amount_pence"), content.get("currency")
    if not _is_int(amount) or not isinstance(currency, str):
        return None
    if term.ref.ref_type == "txn":
        direction = 1 if amount > 0 else -1
        return abs(amount) == term.amount_pence and direction == term.sign \
            and currency == term.currency
    kind = content.get("kind")
    if not isinstance(kind, str):
        return None
    return (amount == term.amount_pence and currency == term.currency and kind == term.kind
            and term.sign in allowed_signs(method, kind))


def _target_check(calc: Calculation, contents: Mapping[str, Any],
                  held: Collection[str]) -> bool | None:
    ref = calc.target_ref
    content = contents.get(ref.key())
    if not isinstance(content, Mapping):
        return True if ref.token() in held else None
    amount, currency = content.get("amount_pence"), content.get("currency")
    if not _is_int(amount) or not isinstance(currency, str):
        return None
    return amount == calc.target_pence and currency == calc.currency


def _timing_check(timing: TimingCheck, contents: Mapping[str, Any],
                  held: Collection[str]) -> bool | None:
    """Recompute settlement timing from the disclosed settlement and credit fields."""
    settlement = contents.get(timing.settlement_ref.key())
    if not isinstance(settlement, Mapping):
        return None
    kind, status = settlement.get("kind"), settlement.get("status")
    observed = _exact_time(settlement.get("occurred_at"))
    raw_effective = settlement.get("effective_at")
    stated = _exact_time(raw_effective) if raw_effective is not None else None
    if not isinstance(kind, str) or observed is None:
        return None
    if raw_effective is not None and stated is None:
        return None  # generalised or malformed: the exact time is not disclosed
    if status is None and timing.settlement_status is not None:
        return None
    # settlement evidence is a settlement record, or a payout that carries its own settlement
    # status (the team's payouts fold the settlement state in; reconcile.is_settlement_evidence)
    settles = kind == "settlement" or (kind == "payout" and status is not None)
    effective = (stated or observed) if settles and status in SETTLED_STATUSES else None
    credit = contents.get(timing.credit_ref.key())
    if isinstance(credit, Mapping):
        booked = _exact_time(credit.get("booked_at"))
        if booked is None:
            return None
    elif timing.credit_ref.token() in held:
        booked = timing.booked_at  # the recipient holds its own transaction
    else:
        return None
    ok = effective is not None and effective <= booked
    return (timing.settlement_status == status and timing.status_observed_at == observed
            and timing.settlement_effective_at == effective and timing.booked_at == booked
            and timing.ok == ok)


def _calc_check(calc: Calculation, contents: Mapping[str, Any],
                held: Collection[str] = ()) -> bool | None:
    """True when every value the calculation shows matches the disclosed contents, False on
    a mismatch, None when something cannot be checked. Refs in ``held`` count as known."""
    if calc.method not in NET_METHODS and len(
            {(term.kind, term.sign) for term in calc.terms if term.ref.ref_type == "record"}) > 1:
        return False  # a one-kind total (refunds_total...) neither mixes kinds nor nets records
    verdicts = [_term_check(term, contents, held, method=calc.method) for term in calc.terms]
    if calc.target_ref is not None:
        verdicts.append(_target_check(calc, contents, held))
    if calc.timing is not None:
        verdicts.append(_timing_check(calc.timing, contents, held))
    for ref in calc.cited_refs:
        if not isinstance(contents.get(ref.key()), Mapping):
            verdicts.append(True if ref.token() in held else None)
    if any(v is False for v in verdicts):
        return False
    if any(v is None for v in verdicts):
        return None
    return True


def _calc_hides_fields(calc: Calculation,
                       results: Mapping[str, tuple[Evidence, FilterResult]]) -> bool:
    """True when the calculation would show a field the policy withholds or generalises."""
    shown: dict[str, set[str]] = {}
    for term in calc.terms:
        fields = {"amount_pence", "currency"}
        if term.ref.ref_type == "record":
            fields.add("kind")
        shown.setdefault(term.ref.key(), set()).update(fields)
    if calc.target_ref is not None:
        shown.setdefault(calc.target_ref.key(), set()).update({"amount_pence", "currency"})
    if calc.timing is not None:
        shown.setdefault(calc.timing.settlement_ref.key(), set()).update(
            {"kind", "status", "occurred_at", "effective_at"})
        shown.setdefault(calc.timing.credit_ref.key(), set()).add("booked_at")
    for key, fields in shown.items():
        if key not in results:
            continue
        result = results[key][1]
        hidden = {path for path, _, _ in [*result.withheld, *result.generalised]}
        if fields & hidden:
            return True
    return False


def _calc_problem(calc: Calculation, contents: Mapping[str, Any],
                  results: Mapping[str, tuple[Evidence, FilterResult]],
                  held: Collection[str]) -> str | None:
    if _calc_hides_fields(calc, results):
        return "the calculation would show fields the policy withholds"
    verdict = _calc_check(calc, contents, held)
    if verdict is False:
        return "the calculation does not match the disclosed evidence"
    if verdict is None:
        return "the calculation cannot be shown from the disclosed evidence"
    return None


# --------------------------------------------------------------------------- build_body


def _pre_exclusion(item: Evidence, request: RequestForContext, needed: Mapping[str, list[str]],
                   needed_tokens: set[str], removed: set[str]) -> str | None:
    ref = item.ref()
    if item.subject_ref != request.subject_ref:
        return REASON_OTHER_SUBJECT
    if removed & {ref.ref_id, ref.token(), ref.key()}:
        return REASON_REMOVED
    if ref.key() not in needed:
        return REASON_OTHER_REVISION if ref.token() in needed_tokens else REASON_NOT_NEEDED
    return None


def _blocked(request: RequestForContext, chosen: dict[str, Claim], items: dict[str, Evidence],
             rule_id: str | None, expires_at: datetime) -> tuple[PackageBody, Review]:
    """The recipient is not allowed: no attachments, every question unresolved."""
    reason = f"recipient {request.audience} is not allowed by the policy"
    claims, changes = [], []
    for question in request.questions:
        claims.append(_generic(question.question_id, RATIONALE_NOT_SHARED, GAP_NOT_SHARED))
        claim = chosen.get(question.question_id)
        if claim is not None:
            changes.append(ClaimChange(question_id=question.question_id, from_status=claim.status,
                                       to_status=ClaimStatus.UNRESOLVED,
                                       reason=_short(reason, 300)))
    review = Review(
        request_id=request.request_id, audience=request.audience,
        excluded=[_entry(item.ref(), reason, rule_id=rule_id) for item in items.values()],
        gaps=[Gap(question_id=c.question_id, text=c.gap) for c in claims],
        claim_changes=changes)
    return _body(request, claims, [], expires_at), review


def _select(items: dict[str, Evidence], request: RequestForContext,
            needed: Mapping[str, list[str]], removed: set[str], policy: CompiledPolicy,
            now: datetime) -> tuple[dict, dict[str, ReviewEntry], list[Evidence]]:
    """Pass 1: keep cited, policy-included sources.

    Returns candidates (key -> (item, FilterResult)), review entries for what is left out,
    and the left-out sources themselves.
    """
    needed_tokens = {key.rsplit("@", 1)[0] for key in needed}
    candidates: dict[str, tuple[Evidence, FilterResult]] = {}
    excluded: dict[str, ReviewEntry] = {}
    left_out: list[Evidence] = []
    for key, item in items.items():
        reason, rule = _pre_exclusion(item, request, needed, needed_tokens, removed), None
        if reason is None:
            result = _filter(policy, item, request.audience, now)
            if result.included:
                candidates[key] = (item, result)
                continue
            reason, rule = result.excluded_reason or "excluded by the policy", result.excluded_rule
        excluded[key] = _entry(item.ref(), reason, rule_id=rule)
        left_out.append(item)
    return candidates, excluded, left_out


def _outcome(claim: Claim, sources: Mapping[str, tuple[Evidence, FilterResult]],
             contents: Mapping[str, Any], held: Collection[str],
             excluded: dict[str, ReviewEntry]) -> tuple[list[EvidenceRef], str | None]:
    """The evidence refs a claim keeps and, when it cannot stand as stated, why."""
    kept = [ref for ref in claim.evidence_refs if ref.key() in sources or ref.token() in held]
    lost = [ref for ref in _claim_refs(claim)
            if ref.key() not in sources and ref.token() not in held]
    if lost:
        for ref in lost:
            excluded.setdefault(ref.key(), _entry(ref, REASON_NOT_PROVIDED))
        details = [f"{ref.key()} ({excluded[ref.key()].reason})" for ref in lost]
        return kept, "cited evidence withheld: " + "; ".join(details)
    if claim.calculation is not None:
        return kept, _calc_problem(claim.calculation, contents, sources, held)
    return kept, None


def _refilter(candidates: dict[str, tuple[Evidence, FilterResult]], items: dict[str, Evidence],
              policy: CompiledPolicy, audience: str,
              now: datetime) -> tuple[dict[str, tuple[Evidence, FilterResult]], frozenset[str]]:
    """Pass 2: filter records again with the ids of every record outside the package."""
    records = [item for item, _ in candidates.values() if isinstance(item, EvidenceRecord)]
    attached = {record.record_id for record in records}
    mentioned = {rid for record in records for rid in record.related_record_ids}
    known = {item.record_id for item in items.values() if isinstance(item, EvidenceRecord)}
    excluded_ids = frozenset((known | mentioned) - attached)
    results = {}
    for key, (item, result) in candidates.items():
        if isinstance(item, EvidenceRecord):
            result = _filter(policy, item, audience, now, excluded_ids)
        results[key] = (item, result)
    return results, excluded_ids


def build_body(request: RequestForContext, claims: Iterable[Claim],
               evidence: Iterable[Evidence], policy: Any, *, now: datetime | None = None,
               expires_at: datetime | None = None,
               removed: Collection[str] = ()) -> tuple[PackageBody, Review]:
    """Build the recipient-facing body and the holder's private review.

    ``policy`` is a ``Policy``, a rule list, a ``CompiledPolicy`` or None (allowlist only).
    ``removed`` names sources the holder took out (record or txn ids, tokens or keys).
    The same inputs and ``now`` give a byte-identical body.
    """
    if not isinstance(request, RequestForContext):
        raise PackageError("build_body expects a RequestForContext")
    now = _resolve_now(now)
    if expires_at is None:
        expires_at = now + DEFAULT_PACKAGE_TTL
    elif not isinstance(expires_at, datetime) or expires_at.utcoffset() is None:
        raise PackageError("expires_at must be a timezone-aware datetime")
    expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= now:
        raise PackageError("expires_at must be after now")
    if isinstance(removed, (str, bytes)):
        raise PackageError("removed must be a collection of source ids")
    removed_ids = set(removed)
    if not all(isinstance(value, str) for value in removed_ids):
        raise PackageError("removed must be a collection of source ids")
    compiled = policy if isinstance(policy, CompiledPolicy) else compile_policy(policy)
    items = _unique_evidence(evidence)
    chosen = _pick_claims(request, claims)
    audience = request.audience
    held = frozenset(f"txn:{txn_id}" for txn_id in request.txn_ids or [])

    allowed, rule_id = compiled.recipient_allowed(audience)
    if not allowed:
        return _blocked(request, chosen, items, rule_id, expires_at)

    # Pass 1: default-minimum selection and policy exclusion.
    needed = _needed(chosen)
    candidates, excluded, left_out = _select(items, request, needed, removed_ids, compiled, now)

    # Decide each claim against what pass 1 would disclose, then keep only the sources the
    # surviving answers still cite: a dropped calculation no longer needs its terms.
    provisional = {key: _attach(request.request_id, item, result).content
                   for key, (item, result) in candidates.items()}
    outcomes = {qid: _outcome(claim, candidates, provisional, held, excluded)
                for qid, claim in chosen.items()}
    used: set[str] = set()
    for qid, claim in chosen.items():
        kept, problem = outcomes[qid]
        used.update(ref.key() for ref in (kept if problem else _claim_refs(claim)))
    for key in sorted(set(candidates) - used):
        item = candidates.pop(key)[0]
        excluded[key] = _entry(item.ref(), REASON_UNUSED)
        left_out.append(item)

    # Pass 2: withhold references to every record that is not in the package.
    results, excluded_ids = _refilter(candidates, items, compiled, audience, now)
    attachments = {key: _attach(request.request_id, item, result)
                   for key, (item, result) in results.items()}

    # Forbidden-content scan over attachment content; a signed record cannot be edited, so one
    # holding withheld text goes out as an extract instead.
    forbidden = _forbidden_values(list(results.values()), left_out, excluded_ids, held)
    forbidden = {low: src for low, src in forbidden.items() if len(src[0]) >= MIN_FORBIDDEN_LEN}
    forbidden.update(_withheld_kinds(compiled, request, items))
    disclosed = _disclosed_strings(attachments.values(), request)
    scrubber = _Scrubber({low: src for low, src in forbidden.items() if low not in disclosed})
    converted: set[str] = set()
    for key, attachment in list(attachments.items()):
        item, result = results[key]
        if attachment.form == "full_record" and scrubber.matches(attachment.content):
            attachment = _extract(request.request_id, item, result)
            converted.add(key)
        if attachment.form == "redacted_extract":
            where = f"attachment {attachment.attachment_id}"
            content = {name: value if name in _IDENTITY_KEYS else scrubber.tree(value, where)
                       for name, value in attachment.content.items()}
            attachment = Attachment.model_validate({**attachment.model_dump(), "content": content})
        attachments[key] = attachment

    # Claims: keep disclosed refs; downgrade what lost support; scrub what remains.
    contents = {key: attachment.content for key, attachment in attachments.items()}
    package_claims: list[PackageClaim] = []
    changes: list[ClaimChange] = []
    for question in request.questions:
        qid = question.question_id
        claim = chosen.get(qid)
        if claim is None:
            package_claims.append(_generic(qid, RATIONALE_NO_ANSWER, GAP_NO_ANSWER))
            continue
        kept, problem = outcomes[qid]
        if problem is None and claim.calculation is not None:  # recheck the final content
            problem = _calc_problem(claim.calculation, contents, results, held)
        if problem:
            if claim.status == ClaimStatus.UNRESOLVED:
                problem = "answer text replaced; " + problem
            changes.append(ClaimChange(question_id=qid, from_status=claim.status,
                                       to_status=ClaimStatus.UNRESOLVED,
                                       reason=_short(problem, 300)))
            package_claims.append(_generic(qid, RATIONALE_WITHHELD, GAP_WITHHELD,
                                           merge_refs(kept)))
            continue
        package_claims.append(PackageClaim(
            question_id=qid,
            statement=scrubber.text(claim.statement, f"{qid} statement", limit=4000),
            status=claim.status,
            rationale=scrubber.text(claim.rationale, f"{qid} rationale", limit=4000),
            evidence_refs=merge_refs(kept),
            calculation=_scrub_calculation(claim.calculation, scrubber, qid),
            gap=scrubber.text(claim.gap, f"{qid} gap", limit=4000) if claim.gap else None))

    included, generalised, withheld = [], [], []
    for key in sorted(attachments):
        attachment, (item, result) = attachments[key], results[key]
        label = ("full issuer-signed record" if attachment.form == "full_record"
                 else "redacted extract (the signed record held withheld text)" if key in converted
                 else "redacted extract")
        included.append(_entry(attachment.ref, f"{label}; cited by {', '.join(needed[key])}"))
        for path, how, rule in result.generalised:
            generalised.append(_entry(attachment.ref, _generalised_reason(path, how), field=path,
                                      rule_id=rule))
        if attachment.form == "redacted_extract":
            for path, why, rule in result.withheld:
                withheld.append(_entry(attachment.ref, why, field=path, rule_id=rule))
    review = Review(
        request_id=request.request_id, audience=audience, included=included,
        excluded=[excluded[key] for key in sorted(excluded)], generalised=generalised,
        withheld_fields=withheld + scrubber.entries(),
        gaps=[Gap(question_id=c.question_id, text=c.gap) for c in package_claims if c.gap],
        claim_changes=changes)
    body = _body(request, package_claims, [attachments[key] for key in sorted(attachments)],
                 expires_at)
    return body, review


# --------------------------------------------------------------------------- drafts and signing


def make_draft(request: RequestForContext, body: PackageBody, review: Review, *,
               policy_version: str, source_revisions: Iterable[EvidenceRef],
               now: datetime | None = None, sequence: int = 0) -> Draft:
    """A pending draft of ``body`` for the holder to review.

    ``source_revisions`` is required: merge ``store.dependency_refs(claim)`` for all claims,
    including private examined refs, so a correction to any
    source the answers depend on, shared or withheld, invalidates the draft and its approval.
    It is merged with every ref the body cites. The draft id is deterministic in the request,
    body digest, policy and time; ``sequence`` (0 by default) distinguishes a rebuilt draft of
    the same body from an earlier, since-invalidated one at the same clock.
    """
    if not isinstance(request, RequestForContext) or not isinstance(body, PackageBody) \
            or not isinstance(review, Review):
        raise PackageError("make_draft expects a RequestForContext, a PackageBody and a Review")
    if source_revisions is None or isinstance(source_revisions, (str, bytes)):
        raise PackageError("source_revisions must list the refs the claims cite "
                           "(e.g. cited_refs(claims))")
    now = _resolve_now(now)
    if body.request_id != request.request_id or body.audience != request.audience:
        raise PackageError("the body does not answer this request for this recipient")
    if review.request_id != body.request_id or review.audience != body.audience:
        raise PackageError("the review belongs to a different request or recipient")
    body_sha256 = canon.digest(body)
    try:
        return Draft(
            draft_id=canon.make_id("drf", request.request_id, request.audience, body_sha256,
                                   policy_version, format_utc(now),
                                   *([sequence] if sequence else [])),
            request_id=request.request_id, audience=request.audience, body=body,
            body_sha256=body_sha256, review=review, policy_version=policy_version,
            source_revisions=merge_refs(source_revisions, body_refs(body)), created_at=now)
    except ValidationError as exc:
        raise PackageError(f"invalid draft ({_summary(exc)})") from exc


def package_signing_payload(package: ContextPackage | dict) -> dict:
    """The signed part of a package: every field but ``signature``, as received."""
    data = package.to_json_dict() if isinstance(package, ContextPackage) else package
    if not isinstance(data, Mapping):
        raise PackageError("a package must be a JSON object")
    missing = [name for name in _SIGNED_FIELDS if name not in data]
    if missing:
        raise PackageError(f"the package is missing {', '.join(missing)}")
    return {name: data[name] for name in _SIGNED_FIELDS}


def sign_package(body: PackageBody, approval, keystore, *, policy_version: str,
                 revisions: Mapping[str, int | None], now: datetime | None = None,
                 kid: str = "alibi_demo") -> ContextPackage:
    """Revalidate ``approval`` for ``body`` now, then sign it with the Alibi key.

    Raises ``ApprovalError`` listing every problem before anything is signed. Does not touch
    the store: the caller saves the package and marks the approval consumed.
    """
    now = _resolve_now(now)
    parsed = body
    if not isinstance(parsed, PackageBody):
        try:
            parsed = PackageBody.model_validate(body)
        except ValidationError as exc:
            raise ApprovalError(["the package body is not a valid package body"]) from exc
    problems = check_approval(approval, body=parsed, audience=parsed.audience,
                              request_id=parsed.request_id, policy_version=policy_version,
                              revisions=revisions, now=now)
    if now >= parsed.expires_at:
        problems.append("the package body has already expired")
    if problems:
        raise ApprovalError(problems)
    body_json = parsed.to_json_dict()
    payload = {
        "schema_version": SCHEMA_PACKAGE,
        "package_id": canon.make_id("pkg", approval.approval_id, approval.body_sha256),
        "issued_at": format_utc(now),
        "body": body_json,
        "body_sha256": canon.digest(body_json),
    }
    signature = keystore.sign(payload, kid=kid, domain=SigningDomain.PACKAGE)
    package = ContextPackage.model_validate({**payload, "signature": signature.to_json_dict()})
    if canon.digest(package_signing_payload(package)) != canon.digest(payload):
        raise PackageError("the signed package does not serialise to the signed bytes")
    return package


# --------------------------------------------------------------------------- verification


def _rejected(reasons: list[str], package_sha256: str | None = None) -> VerificationResult:
    reasons = [_short(reason, 300) for reason in reasons]
    return VerificationResult(accepted=False, package_sha256=package_sha256,
                              authentication=AuthResult(valid=False, reasons=reasons),
                              errors=reasons)


def _raw_package(package: Any) -> tuple[dict | None, str | None]:
    if isinstance(package, ContextPackage):
        return package.to_json_dict(), None
    if isinstance(package, (bytes, bytearray, str)):
        try:
            raw = canon.parse_json(bytes(package) if isinstance(package, bytearray) else package)
        except canon.CanonError:
            return None, "the package is not valid JSON"
    elif isinstance(package, Mapping):
        raw = copy.deepcopy(dict(package))
    else:
        return None, "the package must be a JSON object"
    if not isinstance(raw, dict):
        return None, "the package must be a JSON object"
    return raw, None


def _consistency_problems(attachment: Attachment, content: Any,
                          expected_subject: str | None = None) -> list[str]:
    """Content and trust claims that contradict the attachment's own reference, or that put
    a signed (or subject-naming) source under a subject other than ``expected_subject``."""
    if not isinstance(content, Mapping):
        return ["the content is not a JSON object"]
    problems = []
    is_record = attachment.ref.ref_type == "record"
    trust = SourceTrust(attachment.source_trust)
    if is_record and trust == SourceTrust.BANK_OBSERVED:
        problems.append("a merchant record cannot be bank_observed")
    if not is_record and trust != SourceTrust.BANK_OBSERVED:
        problems.append("a bank transaction must be bank_observed")
    if not is_record and attachment.form == "full_record":
        problems.append("only a merchant record can be attached as a full record")
    full = attachment.form == "full_record"
    id_field = "record_id" if is_record else "txn_id"
    expected_values = ((id_field, attachment.ref.ref_id), ("revision", attachment.ref.revision))
    for name, expected in expected_values:
        if (full or name in content) and not _same(content.get(name), expected):
            problems.append(f"the content {name} does not match the attachment reference")
    if is_record and attachment.issuer_id is not None and (full or "issuer_id" in content) \
            and not _same(content.get("issuer_id"), attachment.issuer_id):
        problems.append("the content issuer_id does not match the attachment issuer")
    if expected_subject is not None \
            and ("subject_ref" in content or attachment.issuer_signature is not None) \
            and not _same(content.get("subject_ref"), expected_subject):
        problems.append("the content subject_ref does not match the expected subject")
    return problems


def _unsigned_note(attachment: Attachment, trust: SourceTrust) -> str:
    if trust == SourceTrust.SELF_SUPPLIED:
        return ("Self-supplied by the account holder: no issuer signature, attested only by the "
                "package signer (lower trust).")
    if trust == SourceTrust.BANK_OBSERVED:
        return ("Bank transaction from the holder's bank feed, attested only by the package "
                "signer (lower trust).")
    if attachment.form == "redacted_extract":
        return ("Redacted extract of an issuer-signed record: the issuer signature cannot cover a "
                "partial record, so only the package signer attests it (lower trust).")
    return "No issuer signature: attested only by the package signer (lower trust)."


def _check_source(raw_attachment: Mapping[str, Any], attachment: Attachment, registry,
                  expected_subject: str | None = None) -> tuple[SourceTrustResult, list[str]]:
    """Trust result for one attachment and the problems that reject the package."""
    content = raw_attachment.get("content")
    problems = _consistency_problems(attachment, content, expected_subject)
    trust = SourceTrust(attachment.source_trust)
    common = {"attachment_id": attachment.attachment_id, "ref": attachment.ref,
              "form": attachment.form, "claimed_trust": trust}
    if attachment.issuer_signature is None:
        note = _unsigned_note(attachment, trust)
        if problems:
            note += " Problems: " + "; ".join(problems)
        return SourceTrustResult(**common, attestation="package_signer", signature_valid=None,
                                 lower_trust=True, note=_short(note, 300)), problems

    signature_valid = False
    if attachment.ref.ref_type != "record":
        problems.append("only a merchant record can carry an issuer signature")
    elif not attachment.issuer_id:
        problems.append("the issuer signature names no issuer")
    else:
        try:
            verify(content, raw_attachment.get("issuer_signature"), registry,
                   domain=SigningDomain.RECORD, issuer_id=attachment.issuer_id)
            signature_valid = True
        except SignatureError as exc:
            problems.append(f"issuer signature: {exc.reason}")
    if signature_valid and not problems:
        note = (f"Issuer signature verified with pinned key {attachment.issuer_signature.kid} "
                f"for issuer {attachment.issuer_id}.")
    elif signature_valid:
        note = "Issuer signature verified, but the source is rejected: " + "; ".join(problems)
    else:
        note = "Claimed issuer signature failed: " + "; ".join(problems)
    lower = not (signature_valid and not problems and trust == SourceTrust.ISSUER_VERIFIED)
    return SourceTrustResult(**common, attestation="issuer_signature",
                             signature_valid=signature_valid, lower_trust=lower,
                             note=_short(note, 300)), problems


def _repeated(values: Iterable[str]) -> list[str]:
    """Values that occur more than once, in first-repeat order."""
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def _verify(package: Any, registry, expected_audience: str, now: datetime | None,
            expected_subject: str | None = None,
            expected_request_id: str | None = None) -> VerificationResult:
    try:
        now = _resolve_now(now)
    except PackageError as exc:
        return _rejected([str(exc)])
    raw, problem = _raw_package(package)
    if problem:
        return _rejected([problem])
    try:
        package_sha256 = canon.digest(raw)
    except canon.CanonError:
        return _rejected(["the package cannot be canonicalised"])
    try:
        parsed = ContextPackage.model_validate(raw)
    except ValidationError as exc:
        return _rejected([f"the package does not match the {SCHEMA_PACKAGE} schema "
                          f"({_summary(exc)})"], package_sha256)
    body = parsed.body

    # 1. Package authentication.
    reasons: list[str] = []
    if canon.digest(raw["body"]) != parsed.body_sha256:
        reasons.append("body_sha256 does not match the package body")
    if body.audience != expected_audience:
        reasons.append(f"the package is addressed to {body.audience}, not {expected_audience}")
    if expected_request_id is not None and body.request_id != expected_request_id:
        reasons.append(f"the package answers request {body.request_id}, not "
                       f"{expected_request_id}")
    if expected_subject is not None and body.case_ref != case_binding(
            body.audience, body.parent_request_id or body.request_id, expected_subject):
        reasons.append("the package is not bound to the expected subject: case_ref does not "
                       "match this recipient, request and subject")
    if now >= body.expires_at:
        reasons.append(f"the package expired at {format_utc(body.expires_at)}")
    if parsed.issued_at > now + CLOCK_SKEW:
        reasons.append("the package was issued in the future")
    if parsed.issued_at >= body.expires_at:
        reasons.append("the package expires before it was issued")
    try:
        verify(package_signing_payload(raw), raw["signature"], registry,
               domain=SigningDomain.PACKAGE)
    except SignatureError as exc:
        reasons.append(f"Alibi package signature: {exc.reason}")
    reasons = [_short(reason, 300) for reason in reasons]
    authentication = AuthResult(valid=not reasons, package_id=parsed.package_id,
                                audience=body.audience, kid=parsed.signature.kid, reasons=reasons)

    # 2. Per-source trust. A failed claimed issuer signature rejects the package, and so does
    # a source or an answer that appears twice: one copy could hide behind the other.
    raw_attachments = raw["body"].get("attachments") or []
    sources, failures = [], []
    for raw_attachment, attachment in zip(raw_attachments, body.attachments):
        result, problems = _check_source(raw_attachment, attachment, registry, expected_subject)
        sources.append(result)
        failures += [_short(f"attachment {attachment.attachment_id}: {p}", 300) for p in problems]
    failures += [_short(f"attachment {attachment_id} appears more than once", 300)
                 for attachment_id in _repeated(a.attachment_id for a in body.attachments)]
    failures += [_short(f"{key} is attached more than once", 300)
                 for key in _repeated(a.ref.key() for a in body.attachments)]
    failures += [_short(f"question {question_id} is answered more than once", 300)
                 for question_id in _repeated(c.question_id for c in body.claims)]
    subjects = {raw_attachment["content"]["subject_ref"] for raw_attachment in raw_attachments
                if isinstance(raw_attachment.get("content"), Mapping)
                and isinstance(raw_attachment["content"].get("subject_ref"), str)}
    if expected_subject is None and len(subjects) > 1:
        failures.append("the attachments are about more than one subject")

    # 3. Claim statuses, with the arithmetic rechecked against disclosed amounts. A ref whose
    # copies disagree cannot back a calculation.
    contents: dict[str, Any] = {}
    conflicting: set[str] = set()
    for raw_attachment, attachment in zip(raw_attachments, body.attachments):
        key, content = attachment.ref.key(), raw_attachment.get("content")
        if key in contents and contents[key] != content:
            conflicting.add(key)
        contents.setdefault(key, content)

    def calculation_valid(calc: Calculation | None) -> bool | None:
        if calc is None:
            return None
        if any(ref.key() in conflicting for ref in calculation_refs(calc)):
            return False
        return _calc_check(calc, contents)

    claims = [ClaimResult(question_id=claim.question_id, status=claim.status,
                          evidence_refs=claim.evidence_refs, gap=claim.gap,
                          calculation_valid=calculation_valid(claim.calculation))
              for claim in body.claims]

    return VerificationResult(accepted=authentication.valid and not failures,
                              package_sha256=package_sha256, authentication=authentication,
                              sources=sources, claims=claims, errors=reasons + failures)


def verify_package(package: dict | str | bytes | ContextPackage, registry, *,
                   expected_audience: str, now: datetime | None = None,
                   expected_subject: str | None = None,
                   expected_request_id: str | None = None) -> VerificationResult:
    """Verify a received package for ``expected_audience`` against the pinned ``registry``.

    ``authentication`` covers schema, body digest, recipient binding (and, when given, the
    request it must answer and the subject it must be about), expiry and the Alibi signature.
    ``sources`` reports each attachment's trust: a claimed issuer signature is checked over
    the raw content (domain alibi:record:v1, the attachment's issuer) and must match the
    attachment's reference; unsigned self-supplied, bank and redacted items are reported at
    lower trust. With ``expected_subject`` (the recipient's own request subject) the body's
    ``case_ref`` must equal ``case_binding(audience, root request, expected_subject)``, so even
    a package of redacted extracts is bound to the subject, and every signed source must name
    that subject; without it, sources naming different subjects reject the package. A source
    or an answer that appears twice rejects it too. ``claims`` lists each status with
    ``calculation_valid``. ``accepted`` is true only when authentication is valid and no
    source check failed. Never raises: unusable input comes back as ``accepted=False``.
    """
    try:
        return _verify(package, registry, expected_audience, now, expected_subject,
                       expected_request_id)
    except Exception as exc:  # noqa: BLE001 - verification reports, it never raises
        return _rejected([f"verification failed unexpectedly ({type(exc).__name__})"])
