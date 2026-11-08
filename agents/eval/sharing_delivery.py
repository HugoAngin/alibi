"""Signed Context Package delivery: Ed25519 over JCS, recipient-scoped verification, bank inbox.

Contract sources: docs/SPEC.md "Technical contracts" and "Consent and signing", and
shared/CONTRACTS.md (Signing, Recipients, Trust, Claim status, Idempotency).

Envelope (both packages and issuer records):

    {"body": {...}, "signature": {"algorithm": "Ed25519", "key_id": "...", "signature": "<b64url>"}}

The signed message is ``domain || 0x00 || JCS(body)``. Packages and issuer records use different
domains, so a signature made for one can never verify as the other, even under the same key.

Two body shapes share the same ten top-level keys:

- ``schema_version == "1"`` (``V1_SCHEMA_VERSION``): the public wire shape the privacy worker emits.
  Scope ``{txn_ids, period, question_ids}``, claim ``evidence_refs`` as ``{ref, revision}`` and
  attachments ``{ref, revision, kind, issuer_id, trust, presentation, fields}``. It carries no issuer
  signatures. A ``redacted_extract`` is attested by the package signer only, whatever its origin
  trust. A ``full_record`` claiming ``issuer_verified`` fails closed unless the verifier is handed a
  matching issuer-signed record envelope out of band (``issuer_records``).
- ``schema_version == SCHEMA_VERSION``: the signed-record shape, whose attachments embed issuer
  record envelopes and are checked inline.

Approval is owned by the coordinator. ``sign_package`` only refuses to sign a body whose digest
differs from the digest the caller says was approved (``sha256:<hex>`` or bare hex); it does not
decide whether approval is valid, and it signs the body as given without adding or renaming keys.
Verification never upgrades trust or claim status: package authentication, per-source trust and
claim status are reported as separate fields.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALGORITHM = "Ed25519"
SCHEMA_VERSION = "alibi.context_package.v1"
PACKAGE_DOMAIN = b"alibi.context_package.v1"
ISSUER_RECORD_DOMAIN = b"alibi.issuer_record.v1"

PURPOSE_PACKAGE = "context_package"
PURPOSE_ISSUER_RECORD = "issuer_record"
PURPOSES = frozenset({PURPOSE_PACKAGE, PURPOSE_ISSUER_RECORD})

TRUST_LEVELS = frozenset({"issuer_verified", "bank_observed", "self_supplied"})
CLAIM_STATUSES = frozenset({"supported", "contradicted", "unresolved"})
RECIPIENTS = ("bank_a", "bank_b")

BODY_FIELDS = frozenset(
    {
        "schema_version",
        "package_id",
        "request_id",
        "audience",
        "case_id",
        "scope",
        "issued_at",
        "expires_at",
        "claims",
        "attachments",
    }
)
ENVELOPE_FIELDS = frozenset({"body", "signature"})
SIGNATURE_FIELDS = frozenset({"algorithm", "key_id", "signature"})
ATTACHMENT_REQUIRED = frozenset({"attachment_id", "trust", "record"})
ATTACHMENT_OPTIONAL = frozenset({"provenance"})
CLAIM_REQUIRED = frozenset({"question_id", "status", "rationale", "evidence_refs"})
CLAIM_OPTIONAL = frozenset({"calculation", "gap"})

V1_SCHEMA_VERSION = "1"
V1_SCOPE_FIELDS = frozenset({"txn_ids", "period", "question_ids"})
V1_CLAIM_FIELDS = frozenset({"question_id", "status", "rationale", "evidence_refs", "calculation", "gap"})
V1_ATTACHMENT_FIELDS = frozenset({"ref", "revision", "kind", "issuer_id", "trust", "presentation", "fields"})
PERIOD_FIELDS = frozenset({"from", "to"})
REF_FIELDS = frozenset({"ref", "revision"})
CALC_FIELDS = frozenset({"terms", "total_minor", "currency"})
TERM_FIELDS = frozenset({"ref", "amount_minor"})
PRESENTATIONS = frozenset({"full_record", "redacted_extract"})
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --------------------------------------------------------------------------- errors


class DeliveryError(Exception):
    """Base error. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class VerificationError(DeliveryError):
    """The package or one of its claimed source signatures failed verification."""


class ConflictError(DeliveryError):
    """A package ID was reused with different bytes."""


class AuthError(DeliveryError):
    """Unknown credential or recipient."""


# --------------------------------------------------------------------------- primitives


def canonical(obj: Any) -> bytes:
    """JCS (RFC 8785) canonical bytes."""
    try:
        return rfc8785.dumps(obj)
    except rfc8785.CanonicalizationError as exc:
        raise VerificationError("non_canonicalizable", str(exc)) from exc


def body_digest(body: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical body. The coordinator binds approval to this value."""
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def bytes_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: Any, expected_len: int) -> bytes:
    if not isinstance(text, str) or not _B64URL_RE.fullmatch(text):
        raise VerificationError("bad_encoding", "expected unpadded base64url")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:
        raise VerificationError("bad_encoding", "invalid base64url length") from exc
    if len(data) != expected_len or _b64e(data) != text:
        raise VerificationError("bad_encoding", "non-canonical base64url or wrong length")
    return data


def _message(domain: bytes, body: Mapping[str, Any]) -> bytes:
    return domain + b"\x00" + canonical(body)


def sign_detached(domain: bytes, body: Mapping[str, Any], private_key: Ed25519PrivateKey, key_id: str) -> dict:
    """Signature object over ``domain || 0x00 || JCS(body)``."""
    return {
        "algorithm": ALGORITHM,
        "key_id": key_id,
        "signature": _b64e(private_key.sign(_message(domain, body))),
    }


def verify_detached(domain: bytes, body: Mapping[str, Any], signature: Mapping[str, Any], public_key: Ed25519PublicKey) -> bool:
    if signature.get("algorithm") != ALGORITHM:
        return False
    try:
        public_key.verify(_b64d(signature.get("signature"), 64), _message(domain, body))
    except (InvalidSignature, VerificationError):
        return False
    return True


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return _b64e(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def package_signing_key_from_master(master_key: bytes) -> Ed25519PrivateKey:
    """Derive the Alibi package-signing key from ``ALIBI_MASTER_KEY`` bytes (HKDF-SHA256).

    The caller reads the environment; this module never touches secrets or prints keys.
    """
    if len(master_key) < 32:
        raise ValueError("master key must be at least 32 bytes")
    seed = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=PACKAGE_DOMAIN + b"/signing-key").derive(master_key)
    return Ed25519PrivateKey.from_private_bytes(seed)


# --------------------------------------------------------------------------- trust registry


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    purpose: str  # PURPOSE_PACKAGE or PURPOSE_ISSUER_RECORD
    owner: str  # "alibi" for package keys, the issuer_id for record keys
    public_key: Ed25519PublicKey = field(repr=False, compare=False)


class TrustRegistry:
    """Pinned, purpose-scoped public keys. One key serves exactly one purpose and owner."""

    def __init__(self) -> None:
        self._keys: dict[str, TrustedKey] = {}

    def add(self, key_id: str, purpose: str, owner: str, public_key: Ed25519PublicKey | Ed25519PrivateKey) -> TrustedKey:
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}")
        if not _ID_RE.match(key_id) or not owner:
            raise ValueError("bad key_id or owner")
        if isinstance(public_key, Ed25519PrivateKey):
            public_key = public_key.public_key()
        raw = public_key_b64(public_key)
        for existing in self._keys.values():
            if public_key_b64(existing.public_key) == raw and (existing.purpose, existing.owner) != (purpose, owner):
                raise ValueError("a key may serve only one purpose and owner")
        if key_id in self._keys:
            old = self._keys[key_id]
            if public_key_b64(old.public_key) != raw or (old.purpose, old.owner) != (purpose, owner):
                raise ValueError(f"key_id {key_id!r} already pinned to different material")
            return old
        entry = TrustedKey(key_id, purpose, owner, public_key)
        self._keys[key_id] = entry
        return entry

    def resolve(self, key_id: Any, purpose: str) -> TrustedKey:
        entry = self._keys.get(key_id) if isinstance(key_id, str) else None
        if entry is None:
            raise VerificationError("unknown_key", f"key {key_id!r} is not pinned")
        if entry.purpose != purpose:
            raise VerificationError("wrong_key_purpose", f"key {key_id!r} is pinned for {entry.purpose}")
        return entry

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "TrustRegistry":
        """``{"keys": [{"key_id", "purpose", "owner", "public_key": <b64url raw 32 bytes>}]}``."""
        reg = cls()
        for k in data["keys"]:
            pub = Ed25519PublicKey.from_public_bytes(_b64d(k["public_key"], 32))
            reg.add(k["key_id"], k["purpose"], k["owner"], pub)
        return reg

    def to_json(self) -> dict:
        return {
            "keys": [
                {"key_id": k.key_id, "purpose": k.purpose, "owner": k.owner, "public_key": public_key_b64(k.public_key)}
                for k in sorted(self._keys.values(), key=lambda k: k.key_id)
            ]
        }


# --------------------------------------------------------------------------- schema


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise VerificationError("schema", f"{name} must be a UTC ISO 8601 string ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise VerificationError("schema", f"{name}: {exc}") from exc
    return parsed


def _require_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise VerificationError("schema", f"{name} must match {_ID_RE.pattern}")
    return value


def _exact_keys(obj: Any, required: frozenset, optional: frozenset, name: str) -> dict:
    if not isinstance(obj, dict):
        raise VerificationError("schema", f"{name} must be an object")
    keys = set(obj)
    if missing := required - keys:
        raise VerificationError("schema", f"{name} missing {sorted(missing)}")
    if extra := keys - required - optional:
        raise VerificationError("schema", f"{name} has unexpected fields {sorted(extra)}")
    return obj


def _check_signature_object(sig: Any, name: str) -> dict:
    _exact_keys(sig, SIGNATURE_FIELDS, frozenset(), name)
    if sig["algorithm"] != ALGORITHM:
        raise VerificationError("unsupported_algorithm", f"{name}.algorithm must be {ALGORITHM}")
    _require_id(sig["key_id"], f"{name}.key_id")
    _b64d(sig["signature"], 64)
    return sig


def _enum(value: Any, allowed: frozenset, name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise VerificationError("schema", f"{name} must be one of {sorted(allowed)}")
    return value


def _require_revision(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VerificationError("schema", f"{name} must be an integer >= 1")
    return value


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationError("schema", f"{name} must be an integer (minor units)")
    return value


def _id_list(value: Any, name: str, *, allow_empty: bool) -> list:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise VerificationError("schema", f"{name} must be a list" + ("" if allow_empty else " with at least one item"))
    for item in value:
        _require_id(item, f"{name}[]")
    if len(set(value)) != len(value):
        raise VerificationError("schema", f"{name} has duplicates")
    return value


def _validate_period(period: Any, name: str) -> None:
    _exact_keys(period, PERIOD_FIELDS, frozenset(), name)
    if not _parse_time(period["from"], f"{name}.from") < _parse_time(period["to"], f"{name}.to"):
        raise VerificationError("schema", f"{name} must be half-open [from, to) with from < to")


def validate_body(body: Any) -> dict:
    """Allowlist schema check for the public package body. Raises VerificationError.

    Dispatches on ``schema_version``: ``"1"`` is the wire v1 shape, ``SCHEMA_VERSION`` the
    signed-record shape. Both reject any key outside their allowlist.
    """
    _exact_keys(body, BODY_FIELDS, frozenset(), "body")
    version = body["schema_version"]
    if version not in (V1_SCHEMA_VERSION, SCHEMA_VERSION):
        raise VerificationError("schema", f"schema_version must be {V1_SCHEMA_VERSION!r} or {SCHEMA_VERSION!r}")
    for name in ("package_id", "request_id", "audience", "case_id"):
        _require_id(body[name], name)
    issued = _parse_time(body["issued_at"], "issued_at")
    expires = _parse_time(body["expires_at"], "expires_at")
    if not issued < expires:
        raise VerificationError("schema", "issued_at must be before expires_at")
    if not isinstance(body["attachments"], list) or not isinstance(body["claims"], list):
        raise VerificationError("schema", "claims and attachments must be lists")
    if version == V1_SCHEMA_VERSION:
        _validate_v1(body)
    else:
        _validate_signed_record_body(body)
    return body


def _validate_v1(body: dict) -> None:
    scope = _exact_keys(body["scope"], V1_SCOPE_FIELDS, frozenset(), "scope")
    txn_ids = _id_list(scope["txn_ids"], "scope.txn_ids", allow_empty=True)
    if scope["period"] is not None:
        _validate_period(scope["period"], "scope.period")
    elif not txn_ids:
        raise VerificationError("schema", "scope needs a period or at least one txn_id")
    questions = set(_id_list(scope["question_ids"], "scope.question_ids", allow_empty=False))

    attached: dict[str, int] = {}  # ref -> revision
    for i, att in enumerate(body["attachments"]):
        where = f"attachments[{i}]"
        _exact_keys(att, V1_ATTACHMENT_FIELDS, frozenset(), where)
        ref = _require_id(att["ref"], f"{where}.ref")
        if ref in attached:
            raise VerificationError("schema", f"duplicate attachment ref {ref!r}")
        attached[ref] = _require_revision(att["revision"], f"{where}.revision")
        _require_id(att["kind"], f"{where}.kind")
        if att["issuer_id"] is not None:
            _require_id(att["issuer_id"], f"{where}.issuer_id")
        _enum(att["trust"], TRUST_LEVELS, f"{where}.trust")
        _enum(att["presentation"], PRESENTATIONS, f"{where}.presentation")
        if att["trust"] == "issuer_verified" and att["issuer_id"] is None:
            raise VerificationError("schema", f"{where}: an issuer_verified origin needs an issuer_id")
        if not isinstance(att["fields"], dict):
            raise VerificationError("schema", f"{where}.fields must be an object")

    answered: set[str] = set()
    for i, claim in enumerate(body["claims"]):
        where = f"claims[{i}]"
        _exact_keys(claim, V1_CLAIM_FIELDS, frozenset(), where)
        qid = _require_id(claim["question_id"], f"{where}.question_id")
        if qid in answered:
            raise VerificationError("schema", f"duplicate question_id {qid!r}")
        if qid not in questions:
            raise VerificationError("schema", f"{where}.question_id {qid!r} is not in scope.question_ids")
        answered.add(qid)
        status = _enum(claim["status"], CLAIM_STATUSES, f"{where}.status")
        if not isinstance(claim["rationale"], str):
            raise VerificationError("schema", f"{where}.rationale must be a string")
        if claim["gap"] is not None and not isinstance(claim["gap"], str):
            raise VerificationError("schema", f"{where}.gap must be a string or null")
        if not isinstance(claim["evidence_refs"], list):
            raise VerificationError("schema", f"{where}.evidence_refs must be a list")
        cited: set[str] = set()
        for j, ref_obj in enumerate(claim["evidence_refs"]):
            name = f"{where}.evidence_refs[{j}]"
            _exact_keys(ref_obj, REF_FIELDS, frozenset(), name)
            ref = _require_id(ref_obj["ref"], f"{name}.ref")
            revision = _require_revision(ref_obj["revision"], f"{name}.revision")
            if ref in cited:
                raise VerificationError("schema", f"{name} repeats {ref!r}")
            if ref not in attached:
                raise VerificationError("reference", f"{name} cites {ref!r}, which is not attached")
            if attached[ref] != revision:
                raise VerificationError("reference", f"{name} cites {ref!r} revision {revision}; attached is {attached[ref]}")
            cited.add(ref)
        if status != "unresolved" and not cited:
            raise VerificationError("reference", f"{where} is {status} without evidence")
        _validate_calculation(claim["calculation"], cited, where)


def _validate_calculation(calc: Any, cited: set[str], where: str) -> None:
    if calc is None:
        return
    _exact_keys(calc, CALC_FIELDS, frozenset(), f"{where}.calculation")
    terms = calc["terms"]
    if not isinstance(terms, list) or not terms:
        raise VerificationError("schema", f"{where}.calculation.terms must be a non-empty list")
    total = 0
    for k, term in enumerate(terms):
        name = f"{where}.calculation.terms[{k}]"
        _exact_keys(term, TERM_FIELDS, frozenset(), name)
        if _require_id(term["ref"], f"{name}.ref") not in cited:
            raise VerificationError("reference", f"{name} uses evidence the claim does not cite")
        total += _require_int(term["amount_minor"], f"{name}.amount_minor")
    if _require_int(calc["total_minor"], f"{where}.calculation.total_minor") != total:
        raise VerificationError("schema", f"{where}.calculation.total_minor must equal the sum of its terms")
    if not isinstance(calc["currency"], str) or not _CURRENCY_RE.match(calc["currency"]):
        raise VerificationError("schema", f"{where}.calculation.currency must be an ISO 4217 code")


def _validate_signed_record_body(body: dict) -> None:
    scope = body["scope"]
    if not isinstance(scope, dict) or not scope:
        raise VerificationError("schema", "scope must be a non-empty object")
    if "from" in scope or "to" in scope:
        if not _parse_time(scope.get("from"), "scope.from") < _parse_time(scope.get("to"), "scope.to"):
            raise VerificationError("schema", "scope period must be half-open [from, to) with from < to")
    if "txn_ids" in scope:
        if not isinstance(scope["txn_ids"], list) or not scope["txn_ids"]:
            raise VerificationError("schema", "scope.txn_ids must be a non-empty list")
        for t in scope["txn_ids"]:
            _require_id(t, "scope.txn_ids[]")

    attachments = body["attachments"]
    ids: set[str] = set()
    for i, att in enumerate(attachments):
        _exact_keys(att, ATTACHMENT_REQUIRED, ATTACHMENT_OPTIONAL, f"attachments[{i}]")
        aid = _require_id(att["attachment_id"], f"attachments[{i}].attachment_id")
        if aid in ids:
            raise VerificationError("schema", f"duplicate attachment_id {aid!r}")
        ids.add(aid)
        _enum(att["trust"], TRUST_LEVELS, f"attachments[{i}].trust")
        record = _exact_keys(att["record"], frozenset({"body"}), frozenset({"signature"}), f"attachments[{i}].record")
        if not isinstance(record["body"], dict):
            raise VerificationError("schema", f"attachments[{i}].record.body must be an object")
        if "signature" in record:
            _check_signature_object(record["signature"], f"attachments[{i}].record.signature")
        if "provenance" in att and not isinstance(att["provenance"], dict):
            raise VerificationError("schema", f"attachments[{i}].provenance must be an object")

    qids: set[str] = set()
    for i, claim in enumerate(body["claims"]):
        _exact_keys(claim, CLAIM_REQUIRED, CLAIM_OPTIONAL, f"claims[{i}]")
        qid = _require_id(claim["question_id"], f"claims[{i}].question_id")
        if qid in qids:
            raise VerificationError("schema", f"duplicate question_id {qid!r}")
        qids.add(qid)
        _enum(claim["status"], CLAIM_STATUSES, f"claims[{i}].status")
        if not isinstance(claim["rationale"], str):
            raise VerificationError("schema", f"claims[{i}].rationale must be a string")
        refs = claim["evidence_refs"]
        if not isinstance(refs, list) or any(not isinstance(r, str) or r not in ids for r in refs):
            raise VerificationError("schema", f"claims[{i}].evidence_refs must reference attachment_ids in this package")


# --------------------------------------------------------------------------- signing


def sign_record(body: Mapping[str, Any], private_key: Ed25519PrivateKey, key_id: str) -> dict:
    """Issuer (mock merchant) record envelope, signed in the issuer-record domain."""
    if not isinstance(body, Mapping) or not isinstance(body.get("issuer_id"), str):
        raise ValueError("record body needs a string issuer_id")
    return {"body": dict(body), "signature": sign_detached(ISSUER_RECORD_DOMAIN, body, private_key, key_id)}


def sign_package(
    body: Mapping[str, Any],
    private_key: Ed25519PrivateKey,
    key_id: str,
    *,
    approved_body_digest: str,
) -> bytes:
    """Sign the exact approved body and return the canonical envelope bytes to deliver.

    The coordinator validates approval (holder, audience, request, policy, source revisions,
    window) before calling. This refuses if the body changed since that digest was taken.
    ``approved_body_digest`` may be ``sha256:<hex>`` or bare lowercase hex of the JCS body.
    The envelope's body is the given body: same keys, same JCS bytes, nothing added.
    Resend these exact bytes on retry; do not re-sign.
    """
    body_bytes = canonical(body)
    plain = validate_body(json.loads(body_bytes))  # plain-JSON view; its JCS bytes equal body_bytes
    if not _digest_matches(approved_body_digest, body_bytes):
        raise VerificationError("approval_mismatch", "body differs from the approved digest")
    envelope = {"body": plain, "signature": sign_detached(PACKAGE_DOMAIN, plain, private_key, key_id)}
    return canonical(envelope)


def _digest_matches(approved: Any, body_bytes: bytes) -> bool:
    if not isinstance(approved, str):
        return False
    hex_digest = approved.removeprefix("sha256:")
    return hmac.compare_digest(hex_digest.encode("utf-8"), hashlib.sha256(body_bytes).hexdigest().encode("ascii"))


# --------------------------------------------------------------------------- verification


@dataclass(frozen=True)
class AttachmentCheck:
    attachment_id: str  # v1: the attachment ``ref``
    trust: str  # the stated (origin) trust, never upgraded
    source_signature: str  # "valid" or "absent"
    source_key_id: str | None
    attested_by: str  # "issuer" when a source signature verified, else "package_signer"
    presentation: str | None = None  # v1: "full_record" or "redacted_extract"
    revision: int | None = None  # v1: the attached revision


def _with_source_attestation(attachment: dict) -> dict:
    redacted = attachment.get("presentation") == "redacted_extract"
    if redacted:
        attestation = "package_signer"
    elif attachment.get("source_signature") == "valid":
        attestation = "issuer_signature"
    else:
        attestation = "none"
    return {**attachment, "attestation": attestation, "redacted": redacted}


@dataclass(frozen=True)
class VerificationResult:
    schema_version: str
    package_id: str
    request_id: str
    case_id: str
    recipient: str
    digest: str  # SHA-256 of the exact received bytes
    expires_at: str
    package_signature: str  # "valid": the package is authentic, not that its claims are true
    package_key_id: str
    attachments: tuple[AttachmentCheck, ...]
    claims: tuple[dict, ...]  # question_id and status, passed through unchanged

    def to_dict(self) -> dict:
        result = asdict(self)
        result["attachments"] = tuple(_with_source_attestation(item) for item in result["attachments"])
        return result


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise VerificationError("malformed", f"duplicate key {k!r}")
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise VerificationError("malformed", f"non-finite number {name}")


def parse_envelope(raw: bytes) -> dict:
    """Strict parse: size cap, UTF-8 JSON, no duplicate keys, canonical JCS bytes, exact envelope keys."""
    if not isinstance(raw, (bytes, bytearray)):
        raise VerificationError("malformed", "package must be bytes")
    raw = bytes(raw)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise VerificationError("too_large", f"package exceeds {MAX_PACKAGE_BYTES} bytes")
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("malformed", str(exc)) from exc
    if canonical(obj) != raw:
        raise VerificationError("non_canonical", "package bytes must be JCS canonical")
    _exact_keys(obj, ENVELOPE_FIELDS, frozenset(), "envelope")
    _check_signature_object(obj["signature"], "signature")
    return obj


def _check_signed_record_attachment(att: dict, registry: TrustRegistry) -> AttachmentCheck:
    aid, trust, record = att["attachment_id"], att["trust"], att["record"]
    rsig = record.get("signature")
    if rsig is None:
        if trust == "issuer_verified":
            raise VerificationError("source_signature_missing", f"{aid} claims issuer_verified without a signature")
        return AttachmentCheck(aid, trust, "absent", None, "package_signer")
    issuer_key = registry.resolve(rsig["key_id"], PURPOSE_ISSUER_RECORD)
    if record["body"].get("issuer_id") != issuer_key.owner:
        raise VerificationError("source_signature_invalid", f"{aid} issuer_id does not own key {rsig['key_id']!r}")
    if not verify_detached(ISSUER_RECORD_DOMAIN, record["body"], rsig, issuer_key.public_key):
        raise VerificationError("source_signature_invalid", f"{aid} source signature does not verify")
    return AttachmentCheck(aid, trust, "valid", rsig["key_id"], "issuer")


def _index_issuer_records(issuer_records: Iterable[Mapping[str, Any]] | None) -> dict[tuple[str, int], Mapping]:
    """Out-of-band issuer proofs keyed by (record_id or txn_id, revision)."""
    index: dict[tuple[str, int], Mapping] = {}
    for env in issuer_records or ():
        if not isinstance(env, Mapping) or set(env) != ENVELOPE_FIELDS or not isinstance(env["body"], Mapping):
            raise VerificationError("schema", "an issuer proof must be a {body, signature} record envelope")
        rbody = env["body"]
        key = (
            _require_id(rbody.get("record_id") or rbody.get("txn_id"), "issuer proof record_id"),
            _require_revision(rbody.get("revision"), "issuer proof revision"),
        )
        if key in index and canonical(index[key]) != canonical(env):
            raise VerificationError("source_signature_invalid", f"conflicting issuer proofs for {key}")
        index[key] = env
    return index


def _check_v1_attachment(att: dict, registry: TrustRegistry, proofs: Mapping[tuple[str, int], Mapping]) -> AttachmentCheck:
    ref, revision, trust, presentation = att["ref"], att["revision"], att["trust"], att["presentation"]
    if presentation == "redacted_extract" or trust != "issuer_verified":
        # Origin trust is reported as stated; only the package signer attests these bytes.
        return AttachmentCheck(ref, trust, "absent", None, "package_signer", presentation, revision)
    proof = proofs.get((ref, revision))
    if proof is None:
        raise VerificationError("source_signature_missing", f"{ref} is a full_record claiming issuer_verified without issuer proof")
    sig = _check_signature_object(proof["signature"], f"issuer proof for {ref}")
    issuer_key = registry.resolve(sig["key_id"], PURPOSE_ISSUER_RECORD)
    rbody = proof["body"]
    bound = issuer_key.owner == att["issuer_id"] == rbody.get("issuer_id") and rbody.get("kind") == att["kind"]
    same_fields = all(k in rbody and canonical(rbody[k]) == canonical(v) for k, v in att["fields"].items())
    if not (bound and same_fields and verify_detached(ISSUER_RECORD_DOMAIN, rbody, sig, issuer_key.public_key)):
        raise VerificationError("source_signature_invalid", f"{ref} does not match a valid issuer-signed record")
    return AttachmentCheck(ref, trust, "valid", sig["key_id"], "issuer", presentation, revision)


def verify_package(
    raw: bytes,
    *,
    recipient: str,
    registry: TrustRegistry,
    now: datetime,
    issuer_records: Iterable[Mapping[str, Any]] | None = None,
) -> VerificationResult:
    """Verify received bytes for an authenticated recipient.

    Checks: canonical encoding, schema allowlist, pinned package-purpose key, package signature
    in the package domain, audience == recipient, issued_at <= now < expires_at, and every claimed
    source signature with a pinned issuer-record key owned by the record's issuer_id. Unsigned
    attachments are accepted only at an explicit non-issuer trust and are attested by the package
    signer alone.

    v1 bodies: a ``redacted_extract`` (any origin trust) is reported ``source_signature="absent"``
    and ``attested_by="package_signer"``. A ``full_record`` claiming ``issuer_verified`` needs a
    matching envelope from ``sign_record`` in ``issuer_records`` (same ref, revision, issuer, kind
    and disclosed field values), or the package is rejected.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    env = parse_envelope(raw)
    body = validate_body(env["body"])
    sig = env["signature"]

    key = registry.resolve(sig["key_id"], PURPOSE_PACKAGE)
    if not verify_detached(PACKAGE_DOMAIN, body, sig, key.public_key):
        raise VerificationError("bad_signature", "package signature does not verify")

    if body["audience"] != recipient:
        raise VerificationError("wrong_recipient", f"package is addressed to {body['audience']!r}")
    now_utc = now.astimezone(timezone.utc)
    if now_utc < _parse_time(body["issued_at"], "issued_at"):
        raise VerificationError("not_yet_valid", "issued_at is in the future")
    if now_utc >= _parse_time(body["expires_at"], "expires_at"):
        raise VerificationError("expired", "package has expired")

    if body["schema_version"] == V1_SCHEMA_VERSION:
        proofs = _index_issuer_records(issuer_records)
        checks = [_check_v1_attachment(att, registry, proofs) for att in body["attachments"]]
    else:
        checks = [_check_signed_record_attachment(att, registry) for att in body["attachments"]]

    return VerificationResult(
        schema_version=body["schema_version"],
        package_id=body["package_id"],
        request_id=body["request_id"],
        case_id=body["case_id"],
        recipient=recipient,
        digest=bytes_digest(raw),
        expires_at=body["expires_at"],
        package_signature="valid",
        package_key_id=sig["key_id"],
        attachments=tuple(checks),
        claims=tuple({"question_id": c["question_id"], "status": c["status"]} for c in body["claims"]),
    )


# --------------------------------------------------------------------------- bank receipt and inbox


@dataclass(frozen=True)
class Receipt:
    package_id: str
    recipient: str
    request_id: str
    digest: str
    received_at: str
    status: str = "accepted"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Delivery:
    receipt: Receipt
    created: bool  # False when identical bytes were already received


@dataclass(frozen=True)
class StoredPackage:
    raw: bytes
    receipt: Receipt
    verification: dict


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BankInbox:
    """One local mock bank service with one inbox per recipient.

    Delivery is addressed to a recipient inbox and verified against that recipient. Reads need a
    credential, which maps to exactly one recipient; one recipient cannot see another's packages.
    Each accepted package is published with an atomic no-clobber link, so concurrent or repeated
    deliveries of identical bytes yield one receipt, and a reused package ID with different bytes
    is rejected. Rejected packages are not stored.
    """

    def __init__(self, root: Path | str, registry: TrustRegistry, credentials: Mapping[str, str]) -> None:
        self.root = Path(root)
        self.registry = registry
        self._credentials = dict(credentials)  # credential -> recipient
        self.recipients = frozenset(self._credentials.values())
        self._lock = threading.Lock()
        for r in self.recipients:
            _require_id(r, "recipient")
            (self.root / r).mkdir(parents=True, exist_ok=True)

    def _path(self, recipient: str, package_id: str) -> Path:
        return self.root / recipient / (hashlib.sha256(package_id.encode()).hexdigest() + ".json")

    def _load(self, recipient: str, package_id: str) -> StoredPackage | None:
        try:
            data = json.loads(self._path(recipient, package_id).read_bytes())
        except FileNotFoundError:
            return None
        verification = data["verification"]
        verification["attachments"] = [_with_source_attestation(item) for item in verification["attachments"]]
        return StoredPackage(base64.b64decode(data["raw"]), Receipt(**data["receipt"]), verification)

    def _publish(self, recipient: str, package_id: str, stored: StoredPackage) -> None:
        final = self._path(recipient, package_id)
        payload = json.dumps(
            {"raw": base64.b64encode(stored.raw).decode(), "receipt": stored.receipt.to_dict(), "verification": stored.verification},
            sort_keys=True,
        ).encode()
        fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.link(tmp, final)  # atomic; raises FileExistsError instead of overwriting
        finally:
            os.unlink(tmp)

    @staticmethod
    def _same_or_conflict(existing: StoredPackage, digest: str) -> Delivery:
        if existing.receipt.digest != digest:
            raise ConflictError("package_id_conflict", f"{existing.receipt.package_id} was already received with different bytes")
        return Delivery(existing.receipt, created=False)

    def deliver(
        self,
        recipient: str,
        raw: bytes,
        *,
        now: datetime,
        issuer_records: Iterable[Mapping[str, Any]] | None = None,
    ) -> Delivery:
        """Receive a package into ``recipient``'s inbox and return the receipt (the bank acknowledgment).

        ``issuer_records`` is passed to ``verify_package`` for v1 full-record issuer proofs.
        """
        if recipient not in self.recipients:
            raise AuthError("unknown_recipient", repr(recipient))
        raw = bytes(raw)
        digest = bytes_digest(raw)
        body = parse_envelope(raw)["body"]
        package_id = _require_id(body.get("package_id") if isinstance(body, dict) else None, "package_id")
        with self._lock:
            existing = self._load(recipient, package_id)
            if existing is not None and existing.receipt.digest == digest:
                return Delivery(existing.receipt, created=False)  # exact retry, even after expiry
            # Verify before reporting a conflict, so unauthenticated bytes cannot probe which IDs exist.
            result = verify_package(raw, recipient=recipient, registry=self.registry, now=now, issuer_records=issuer_records)
            if existing is not None:
                raise ConflictError("package_id_conflict", f"{package_id} was already received with different bytes")
            receipt = Receipt(result.package_id, recipient, result.request_id, digest, _iso(now))
            try:
                self._publish(recipient, package_id, StoredPackage(raw, receipt, result.to_dict()))
            except FileExistsError:  # another process won the race
                return self._same_or_conflict(self._load(recipient, package_id), digest)
            return Delivery(receipt, created=True)

    def _recipient_for(self, credential: str) -> str:
        recipient = self._credentials.get(credential)
        if recipient is None:
            raise AuthError("unauthenticated", "unknown credential")
        return recipient

    def get(self, credential: str, package_id: str) -> StoredPackage:
        recipient = self._recipient_for(credential)
        stored = self._load(recipient, _require_id(package_id, "package_id"))
        if stored is None:  # same answer whether absent or another recipient's
            raise KeyError(package_id)
        return stored

    def list_receipts(self, credential: str) -> list[Receipt]:
        recipient = self._recipient_for(credential)
        out = []
        for p in sorted((self.root / recipient).glob("*.json")):
            out.append(Receipt(**json.loads(p.read_bytes())["receipt"]))
        return sorted(out, key=lambda r: (r.received_at, r.package_id))
