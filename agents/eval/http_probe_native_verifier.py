"""Verifier checks against the native bank API, using a public approved package."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.eval.http_probe_runtime import BLOCKED, NOT_BLOCKED, ProbeUnavailable
from alibi.core.canon import canonical_bytes, digest
from alibi.core.package import package_signing_payload
from alibi.core.schemas import SigningDomain, format_utc, parse_utc
from alibi.core.signing import sign, signing_input


class Bank:
    def __init__(self, ctx):
        self.ctx = ctx
        self.service = ctx.fixtures.get("native_verifier_service", "bank")
        origin = ctx.base_urls.get(self.service, "")
        if not isinstance(origin, str) or urlsplit(origin).hostname not in {
            "127.0.0.1", "::1", "localhost"
        }:
            raise ProbeUnavailable("native verifier probes need a disposable loopback bank")
        self.pid = ctx.require("native_verifier_package_id")
        response = self.get(self.pid, suffix="/raw")
        ctx.require_success(response)
        self.original = ctx.payload(response)
        if (self.original.get("package_id") != self.pid
                or self.original.get("body", {}).get("audience") != "bank_a"):
            raise ProbeUnavailable("the verifier control must be a native Bank A package")

    def get(self, pid, actor="bank_a", suffix=""):
        return self.ctx.http("GET", self.service, self.ctx.route(
            "native_verifier_package", "/packages/{package_id}", package_id=pid
        ) + suffix, actor)

    def inbox(self):
        response = self.ctx.http("GET", self.service, self.ctx.route(
            "native_verifier_inbox", "/inbox"), "bank_a")
        self.ctx.require_success(response)
        return self.ctx.payload(response)

    def deliver(self, envelope, actor="bank_a"):
        return self.ctx.http("POST", self.service, self.ctx.route(
            "native_verifier_deliver", "/deliver"), actor, content=canonical_bytes(envelope),
            headers={"Content-Type": "application/json"})

    def variant(self):
        envelope = deepcopy(self.original)
        envelope["package_id"] = f"pkg-probe-{uuid4().hex}"
        return envelope

    def signed(self, envelope, *, key=None, kid=None, domain=SigningDomain.PACKAGE):
        if key is None:
            key = self.key("native_verifier_package_pem")
        envelope["body_sha256"] = digest(envelope["body"])
        envelope["signature"] = sign(
            package_signing_payload(envelope), private_key=key,
            kid=kid or self.original["signature"]["kid"], domain=domain,
        ).to_json_dict()
        return envelope

    def key(self, fixture):
        try:
            value = serialization.load_pem_private_key(
                Path(self.ctx.require(fixture)).read_bytes(), password=None)
        except (OSError, TypeError, ValueError):
            raise ProbeUnavailable(f"cannot load disposable Ed25519 key: {fixture}") from None
        if not isinstance(value, Ed25519PrivateKey):
            raise ProbeUnavailable(f"fixture must be an Ed25519 key: {fixture}")
        return value

    def acceptance(self, response, envelope):
        payload = self.ctx.payload(response)
        if response.status_code != 200:
            if self.ctx.code(response) in {"package_rejected", "audience_mismatch", "package_id_reused"}:
                return None
            raise ProbeUnavailable(f"verifier control returned HTTP {response.status_code}")
        receipt = payload.get("receipt", {})
        if (payload.get("status") != "verified" or receipt.get("accepted") is not True
                or not receipt.get("receipt_id")
                or receipt.get("package_id") != envelope["package_id"]
                or receipt.get("package_sha256") != digest(envelope)
                or receipt.get("recipient") != envelope["body"]["audience"]):
            return None
        return payload

    def baseline(self):
        baseline = self.signed(self.variant())
        if self.acceptance(self.deliver(baseline), baseline) is None:
            raise ProbeUnavailable("native verifier did not accept a freshly signed control")

    def refused(self, envelope, reason, *, actor="bank_a", code="package_rejected", status=422):
        response = self.deliver(envelope, actor)
        if response.is_success:
            return False
        receipt = self.ctx.payload(response).get("receipt", {})
        if (response.status_code != status or self.ctx.code(response) != code
                or receipt.get("accepted") is not False
                or not any(reason in item.lower() for item in receipt.get("reasons", []))):
            raise ProbeUnavailable("native verifier returned an unplanned refusal")
        pid = envelope["package_id"]
        view = self.get(pid, actor)
        self.ctx.require_success(view)
        payload = self.ctx.payload(view)
        raw = self.get(pid, actor, "/raw")
        attachments = self.get(pid, actor, "/attachments")
        return (payload.get("status") == "rejected"
                and not {"package", "claims", "sources", "attachments"} & payload.keys()
                and all(r.status_code == 409 and self.ctx.code(r) == "package_rejected"
                        for r in (raw, attachments)))

    def issuer_attachment(self, envelope):
        for attachment in envelope["body"]["attachments"]:
            if attachment.get("form") == "full_record" and attachment.get("issuer_signature"):
                return attachment
        raise ProbeUnavailable("the public control has no full issuer-signed attachment")


def _flip(signature):
    value = signature["sig"]
    signature["sig"] = ("0" if value[0] != "0" else "1") + value[1:]


def _sp25(bank):
    tampered = bank.signed(bank.variant())
    tampered["body"]["claims"][0]["statement"] += "."
    flipped = bank.signed(bank.variant())
    _flip(flipped["signature"])
    return all([bank.refused(tampered, "body_sha256"), bank.refused(flipped, "signature does not verify")])


def _sp26(bank):
    return bank.refused(bank.signed(bank.variant()), "addressed to bank_a",
                        actor="bank_b", code="audience_mismatch", status=409)


def _sp27(bank):
    envelope = bank.variant()
    try:
        now = parse_utc(bank.inbox()["as_of"])
    except (KeyError, TypeError, ValueError):
        raise ProbeUnavailable("bank inbox does not report a valid fixture clock") from None
    envelope["issued_at"] = format_utc(now - timedelta(minutes=2))
    envelope["body"]["expires_at"] = format_utc(now - timedelta(minutes=1))
    refused = bank.refused(bank.signed(envelope), "expired")
    original = bank.get(bank.pid, suffix="/raw")
    bank.ctx.require_success(original)
    return refused and bank.ctx.payload(original) == bank.original


def _sp28(bank):
    attachment = bank.issuer_attachment(bank.original)
    issuer = attachment["issuer_signature"]
    key = bank.key("native_verifier_record_pem")
    try:
        key.public_key().verify(bytes.fromhex(issuer["sig"]),
                                signing_input(SigningDomain.RECORD, attachment["content"]))
    except (InvalidSignature, ValueError):
        raise ProbeUnavailable("record key does not match the public control's issuer signature") from None
    wrong_domain = bank.signed(bank.variant(), key=key, kid=issuer["kid"],
                               domain=SigningDomain.RECORD)
    wrong_purpose = bank.signed(bank.variant(), key=key, kid=issuer["kid"])
    unknown = bank.signed(bank.variant(), key=Ed25519PrivateKey.generate(),
                          kid=f"rogue-{uuid4().hex}")
    return all([bank.refused(unknown, "not in the trust registry"),
                bank.refused(wrong_domain, "does not match expected"),
                bank.refused(wrong_purpose, "pinned for record_signing")])


def _sp29(bank):
    invalid = bank.variant()
    _flip(bank.issuer_attachment(invalid)["issuer_signature"])
    missing = bank.variant()
    del bank.issuer_attachment(missing)["issuer_signature"]
    return all([bank.refused(bank.signed(invalid), "issuer signature: signature does not verify"),
                bank.refused(bank.signed(missing), "needs its issuer signature")])


def _sp30(bank):
    envelope = bank.variant()
    ref = f"self-probe-{uuid4().hex}"
    envelope["body"]["attachments"].append({
        "attachment_id": ref, "form": "full_record", "source_trust": "self_supplied",
        "ref": {"ref_type": "record", "ref_id": ref, "revision": 1},
        "content": {"record_id": ref, "revision": 1, "label": "Synthetic holder receipt"},
    })
    bank.signed(envelope)
    accepted = bank.acceptance(bank.deliver(envelope), envelope) or {}
    sources = accepted.get("sources", [])
    return (any(s.get("attachment_id") == ref and s.get("claimed_trust") == "self_supplied"
                and s.get("lower_trust") is True and s.get("attestation") == "package_signer"
                and s.get("signature_valid") is None for s in sources)
            and any(s.get("claimed_trust") == "issuer_verified" and s.get("lower_trust") is False
                    for s in sources))


def _sp31(bank):
    envelope = bank.signed(bank.variant())
    first = bank.acceptance(bank.deliver(envelope), envelope)
    if first is None:
        raise ProbeUnavailable("initial delivery for retry was not accepted")
    again = bank.acceptance(bank.deliver(envelope), envelope)
    modified = deepcopy(envelope)
    modified["body"]["claims"][0]["statement"] += "."
    conflict = bank.deliver(bank.signed(modified))
    listed = [p for p in bank.inbox().get("packages", [])
              if p.get("package_id") == envelope["package_id"]]
    if not conflict.is_success and (conflict.status_code != 409
                                     or bank.ctx.code(conflict) != "package_id_reused"):
        raise ProbeUnavailable("native verifier returned an unplanned retry refusal")
    original = bank.get(envelope["package_id"], suffix="/raw")
    bank.ctx.require_success(original)
    return (again is not None and first["receipt"] == again["receipt"]
            and len(listed) == 1 and listed[0].get("receipt_id") == first["receipt"]["receipt_id"]
            and bank.ctx.payload(original) == envelope
            and conflict.status_code == 409)


def _sp32(bank):
    result = bank.acceptance(bank.get(bank.pid), bank.original) or {}
    sources, claims = result.get("sources", []), result.get("claims", [])
    return (result.get("authentication", {}).get("valid") is True
            and bool(sources) and all({"attestation", "lower_trust"} <= row.keys() for row in sources)
            and bool(claims) and all({"question_id", "status"} <= row.keys() for row in claims))


def _handler(check, *, baseline):
    def run(ctx):
        bank = Bank(ctx)
        if baseline:
            bank.baseline()
        return BLOCKED if check(bank) else NOT_BLOCKED
    return run


HANDLERS = {f"SP-{i}": _handler(check, baseline=i < 30)
            for i, check in enumerate(
                (_sp25, _sp26, _sp27, _sp28, _sp29, _sp30, _sp31, _sp32), 25)}
