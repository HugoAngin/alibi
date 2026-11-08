"""Verify the published S1 package format without importing the app."""

from datetime import datetime, timedelta
from hashlib import sha256
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agents.eval.sharing_delivery import (
    AttachmentCheck, VerificationError, VerificationResult, _exact_keys, _id_list,
    _parse_time, _require_revision, _validate_period, bytes_digest, canonical,
)

SCHEMA = "alibi.package.v1"
BODY_SCHEMA = "alibi.package_body.v1"
PACKAGE_DOMAIN = "alibi:package:v1"
RECORD_DOMAIN = "alibi:record:v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
NO_FIELDS = frozenset()
SIGNED_FIELDS = frozenset({"schema_version", "package_id", "issued_at", "body", "body_sha256"})
BODY_FIELDS = frozenset({"schema_version", "request_id", "audience", "case_ref", "scope", "questions", "claims",
                         "attachments", "expires_at"})
REF_FIELDS = frozenset({"ref_type", "ref_id", "revision"})
ATTACHMENT_FIELDS = frozenset({"attachment_id", "ref", "form", "source_trust", "content"})
SIGNATURE_FIELDS = frozenset({"alg", "kid", "domain", "sig"})
PURPOSES = {"record_signing", "package_signing", "request_signing"}


def _ident(value, field):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise VerificationError("schema", f"invalid {field}")
    return value


def _hex(value, digits):
    return isinstance(value, str) and len(value) == digits and re.fullmatch(r"[0-9a-f]+", value) is not None


def _registry(entries):
    if not isinstance(entries, dict) or not entries:
        raise VerificationError("invalid_registry")
    result = {}
    for kid, entry in entries.items():
        if (not isinstance(kid, str) or not ID.fullmatch(kid) or not isinstance(entry, dict)
                or set(entry) != {"alg", "public_key", "purpose", "issuer_id"}
                or entry["alg"] != "Ed25519" or not _hex(entry["public_key"], 64)
                or not isinstance(entry["purpose"], str) or entry["purpose"] not in PURPOSES
                or not isinstance(entry["issuer_id"], str) or not ID.fullmatch(entry["issuer_id"])):
            raise VerificationError("invalid_registry")
        result[kid] = (Ed25519PublicKey.from_public_bytes(bytes.fromhex(entry["public_key"])),
                       entry["purpose"], entry["issuer_id"])
    return result


def _signature(payload, envelope, keys, *, domain, purpose, issuer):
    sig = _exact_keys(envelope, SIGNATURE_FIELDS, NO_FIELDS, "signature")
    kid = _ident(sig["kid"], "signature.kid")
    if sig["alg"] != "Ed25519" or sig["domain"] != domain or not _hex(sig["sig"], 128):
        raise VerificationError("bad_signature")
    if kid not in keys:
        raise VerificationError("unknown_key")
    key, actual_purpose, actual_issuer = keys[kid]
    if actual_purpose != purpose or actual_issuer != issuer:
        raise VerificationError("wrong_key_purpose")
    try:
        key.verify(bytes.fromhex(sig["sig"]), domain.encode("ascii") + b"\0" + canonical(payload))
    except InvalidSignature as exc:
        raise VerificationError("bad_signature") from exc
    return kid


def _ref(value):
    ref = _exact_keys(value, REF_FIELDS, NO_FIELDS, "evidence reference")
    if ref["ref_type"] not in ("record", "txn"):
        raise VerificationError("schema", "invalid reference type")
    return (ref["ref_type"], _ident(ref["ref_id"], "ref_id"), _require_revision(ref["revision"], "revision"))


def _attachment(value, keys, subject):
    row = _exact_keys(value, ATTACHMENT_FIELDS, {"issuer_id", "issuer_signature", "generalised_fields"}, "attachment")
    attachment_id = _ident(row["attachment_id"], "attachment_id")
    ref = kind, source_id, revision = _ref(row["ref"])
    form, trust, content = row["form"], row["source_trust"], row["content"]
    if form not in ("full_record", "redacted_extract") or not isinstance(content, dict):
        raise VerificationError("schema", "invalid attachment content or form")
    if (kind == "txn" and trust != "bank_observed") or (kind == "record" and trust not in ("issuer_verified", "self_supplied")):
        raise VerificationError("schema", "invalid origin trust for the evidence kind")

    id_field = "record_id" if kind == "record" else "txn_id"
    for field, expected in ((id_field, source_id), ("revision", revision)):
        if form == "full_record" or field in content:
            actual = content.get(field)
            if type(actual) is not type(expected) or actual != expected:
                raise VerificationError("source_reference_mismatch")
    issuer = row.get("issuer_id")
    if issuer is not None:
        _ident(issuer, "attachment.issuer_id")
    if kind == "record" and (form == "full_record" or "issuer_id" in content) and content.get("issuer_id") != issuer:
        raise VerificationError("source_reference_mismatch")
    if subject is not None and content.get("subject_ref") != subject:
        raise VerificationError("subject_unverifiable")

    sig = row.get("issuer_signature")
    if form == "redacted_extract":
        if sig is not None:
            raise VerificationError("source_signature_invalid")
        return ref, AttachmentCheck(attachment_id, trust, "absent", None, "package_signer", form, revision)
    if kind != "record" or trust != "issuer_verified" or sig is None or issuer is None:
        raise VerificationError("source_signature_missing")
    if row.get("generalised_fields"):
        raise VerificationError("schema", "full records cannot have generalised fields")
    try:
        kid = _signature(content, sig, keys, domain=RECORD_DOMAIN, purpose="record_signing", issuer=issuer)
    except VerificationError as exc:
        raise VerificationError("source_signature_invalid") from exc
    return ref, AttachmentCheck(attachment_id, trust, "valid", kid, "issuer", form, revision)


def verify_native(package: dict, raw: bytes, registry: dict, *, recipient: str, now: datetime,
                  request_id: str | None = None, subject: str | None = None) -> VerificationResult:
    envelope = _exact_keys(package, SIGNED_FIELDS | {"signature"}, NO_FIELDS, "package")
    if envelope["schema_version"] != SCHEMA:
        raise VerificationError("schema", "unknown native package version")
    body = _exact_keys(envelope["body"], BODY_FIELDS, {"parent_request_id"}, "body")
    if body["schema_version"] != BODY_SCHEMA:
        raise VerificationError("schema", "unknown native body version")
    keys = _registry(registry)
    if envelope["body_sha256"] != sha256(canonical(body)).hexdigest():
        raise VerificationError("bad_signature", "body digest differs from signed envelope")
    kid = _signature({key: envelope[key] for key in SIGNED_FIELDS}, envelope["signature"], keys,
                     domain=PACKAGE_DOMAIN, purpose="package_signing", issuer="alibi")
    package_id = _ident(envelope["package_id"], "package_id")
    body_request = _ident(body["request_id"], "request_id")
    case_ref = _ident(body["case_ref"], "case_ref")
    if _ident(body["audience"], "audience") != recipient:
        raise VerificationError("wrong_recipient")
    if request_id is not None and request_id != body_request:
        raise VerificationError("wrong_request")
    issued = _parse_time(envelope["issued_at"], "issued_at")
    expires = _parse_time(body["expires_at"], "expires_at")
    if issued >= expires:
        raise VerificationError("schema", "invalid validity window")
    if now >= expires:
        raise VerificationError("expired")
    if issued > now + timedelta(minutes=5):
        raise VerificationError("not_yet_valid")

    scope = _exact_keys(body["scope"], NO_FIELDS, {"period", "txn_ids"}, "scope")
    period, txns = scope.get("period"), scope.get("txn_ids")
    if (period is not None) == bool(txns):
        raise VerificationError("schema", "expected exactly one RFC scope")
    if period is not None:
        _validate_period(period, "scope.period")
    if txns is not None:
        _id_list(txns, "scope.txn_ids", allow_empty=period is not None)

    if not all(isinstance(body[key], list) for key in ("questions", "claims", "attachments")):
        raise VerificationError("schema", "questions, claims and attachments must be arrays")
    if subject is not None and not body["attachments"]:
        raise VerificationError("subject_unverifiable")
    attachments = [_attachment(row, keys, subject) for row in body["attachments"]]
    refs = {ref for ref, _ in attachments}
    if len(refs) != len(attachments) or len({check.attachment_id for _, check in attachments}) != len(attachments):
        raise VerificationError("schema", "duplicate attachments")

    questions = set()
    for row in body["questions"]:
        question = _exact_keys(row, {"question_id", "text"}, NO_FIELDS, "question")
        questions.add(_ident(question["question_id"], "question_id"))
    claims = {}
    for row in body["claims"]:
        claim = _exact_keys(row, {"question_id", "status", "statement", "rationale", "evidence_refs"},
                            {"gap", "calculation"}, "claim")
        question_id = _ident(claim["question_id"], "question_id")
        status = claim["status"]
        if question_id in claims or status not in ("supported", "contradicted", "unresolved"):
            raise VerificationError("schema", "invalid or duplicate claim")
        if not isinstance(claim["evidence_refs"], list) or any(_ref(ref) not in refs for ref in claim["evidence_refs"]):
            raise VerificationError("source_reference_mismatch")
        claims[question_id] = {"question_id": question_id, "status": status}
    if len(questions) != len(body["questions"]) or questions != claims.keys():
        raise VerificationError("schema", "claims do not match the RFC questions")

    return VerificationResult(SCHEMA, package_id, body_request, case_ref, recipient, bytes_digest(raw),
                              body["expires_at"], "valid", kid, tuple(check for _, check in attachments),
                              tuple(claims.values()))
