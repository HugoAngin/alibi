"""Ed25519 signatures over canonical JSON, with domain separation and a pinned registry.

Signed bytes are ``domain`` + NUL + JCS(payload), so a record signature can never be
replayed as a package or request signature. The envelope is
``{"alg": "Ed25519", "kid": ..., "domain": ..., "sig": <128 hex chars>}``.

Verification pins to a ``TrustRegistry`` (kid -> public key, purpose, issuer). A key only
verifies in the domain its purpose allows, and, when the caller names an issuer, only for
that issuer.

``DemoKeystore`` holds the five demo keys (merchant_demo, merchant_mock, alibi_demo, bank_a,
bank_b) as PKCS#8 PEM files with mode 0600 under ``data/alibi/keys/`` by default. Keys are generated
randomly on first use. A ``seed`` derives them deterministically instead: tests only, since
anyone who knows the seed can sign. On Windows, 0600 is best effort (only the read-only
bit is honoured by ``os.chmod``).
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from alibi.core.canon import canonical_bytes
from alibi.core.schemas import KeyPurpose, SignatureEnvelope, SigningDomain

DOMAIN_PURPOSE: dict[SigningDomain, KeyPurpose] = {
    SigningDomain.RECORD: KeyPurpose.RECORD_SIGNING,
    SigningDomain.PACKAGE: KeyPurpose.PACKAGE_SIGNING,
    SigningDomain.REQUEST: KeyPurpose.REQUEST_SIGNING,
}

# kid -> (purpose, issuer_id the key speaks for)
DEMO_KEYS: dict[str, tuple[KeyPurpose, str]] = {
    "merchant_demo": (KeyPurpose.RECORD_SIGNING, "threadline"),       # the S1 fixture issuer
    "merchant_mock": (KeyPurpose.RECORD_SIGNING, "mock-marketplace"),  # the team's case issuer
    "alibi_demo": (KeyPurpose.PACKAGE_SIGNING, "alibi"),
    "bank_a": (KeyPurpose.REQUEST_SIGNING, "bank_a"),
    "bank_b": (KeyPurpose.REQUEST_SIGNING, "bank_b"),
}

DEFAULT_KEY_DIR = Path("data/alibi/keys")
REGISTRY_FILE = "trust_registry.json"


class SignatureError(Exception):
    """A signature, key or envelope failed a check. ``reason`` is safe to show and log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def signing_input(domain: SigningDomain | str, payload: Any) -> bytes:
    """The exact bytes signed: domain, a NUL separator, then canonical JSON of the payload."""
    return SigningDomain(domain).value.encode("ascii") + b"\x00" + canonical_bytes(payload)


def public_key_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    if isinstance(key, Ed25519PrivateKey):
        key = key.public_key()
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class TrustEntry:
    kid: str
    public_key: str        # raw 32-byte Ed25519 public key, hex
    purpose: KeyPurpose
    issuer_id: str         # the identity this key's signatures speak for

    def key(self) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key))


class TrustRegistry:
    """Pinned public keys: ``{kid: {"public_key", "purpose", "issuer_id", "alg"}}``."""

    def __init__(self, entries: dict[str, TrustEntry] | None = None):
        self._entries: dict[str, TrustEntry] = dict(entries or {})

    def add(self, kid: str, public_key: str, purpose: KeyPurpose | str, issuer_id: str) -> None:
        entry = _entry_from_json(kid, {"public_key": public_key, "purpose": str(purpose),
                                       "issuer_id": issuer_id})
        existing = self._entries.get(kid)
        if existing is not None and existing != entry:
            raise SignatureError(f"key id {kid!r} is already pinned to a different key")
        self._entries[kid] = entry

    def get(self, kid: str) -> TrustEntry:
        entry = self._entries.get(kid)
        if entry is None:
            raise SignatureError(f"key id {kid!r} is not in the trust registry")
        return entry

    def __contains__(self, kid: object) -> bool:
        return kid in self._entries

    def kids(self) -> list[str]:
        return sorted(self._entries)

    def merged(self, other: "TrustRegistry") -> "TrustRegistry":
        out = TrustRegistry(self._entries)
        for kid in other.kids():
            entry = other.get(kid)
            out.add(kid, entry.public_key, entry.purpose, entry.issuer_id)
        return out

    def to_json(self) -> dict[str, dict[str, str]]:
        return {
            kid: {"alg": "Ed25519", "public_key": e.public_key, "purpose": e.purpose.value,
                  "issuer_id": e.issuer_id}
            for kid, e in sorted(self._entries.items())
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TrustRegistry":
        if not isinstance(data, dict):
            raise SignatureError("trust registry must be a JSON object of kid -> entry")
        return cls({kid: _entry_from_json(kid, value) for kid, value in data.items()})

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "TrustRegistry":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def _entry_from_json(kid: str, value: Any) -> TrustEntry:
    if not isinstance(value, dict):
        raise SignatureError(f"registry entry for {kid!r} must be an object")
    if value.get("alg", "Ed25519") != "Ed25519":
        raise SignatureError(f"registry entry for {kid!r} is not an Ed25519 key")
    public_key = value.get("public_key")
    if not isinstance(public_key, str) or len(public_key) != 64:
        raise SignatureError(f"registry entry for {kid!r} needs a 64-hex-char public_key")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        purpose = KeyPurpose(value.get("purpose"))
    except ValueError as exc:
        raise SignatureError(f"registry entry for {kid!r} is invalid: {exc}") from exc
    issuer_id = value.get("issuer_id")
    if not isinstance(issuer_id, str) or not issuer_id:
        raise SignatureError(f"registry entry for {kid!r} needs issuer_id")
    return TrustEntry(kid=kid, public_key=public_key.lower(), purpose=purpose, issuer_id=issuer_id)


# --------------------------------------------------------------------------- sign / verify


def sign(payload: Any, *, private_key: Ed25519PrivateKey, kid: str,
         domain: SigningDomain | str) -> SignatureEnvelope:
    """Sign ``payload`` (JSON value or model) in ``domain``. No purpose check: see DemoKeystore.sign."""
    domain = SigningDomain(domain)
    sig = private_key.sign(signing_input(domain, payload))
    return SignatureEnvelope(kid=kid, domain=domain, sig=sig.hex())


def verify(payload: Any, envelope: SignatureEnvelope | dict[str, Any], registry: TrustRegistry, *,
           domain: SigningDomain | str, issuer_id: str | None = None) -> TrustEntry:
    """Return the pinned entry that signed ``payload``; raise ``SignatureError`` otherwise.

    Fails if the envelope is malformed, the domain differs from ``domain``, the kid is not
    pinned, the pinned purpose does not match the domain, the key belongs to a different
    issuer than ``issuer_id``, or the signature does not verify over the payload.
    Pass the raw received JSON object as ``payload``, not a re-serialised model.
    """
    expected = SigningDomain(domain)
    if not isinstance(envelope, SignatureEnvelope):
        try:
            envelope = SignatureEnvelope.model_validate(envelope)
        except ValueError as exc:
            raise SignatureError("malformed signature envelope") from exc
    if envelope.domain != expected:
        raise SignatureError(
            f"signature domain {envelope.domain.value} does not match expected {expected.value}")
    entry = registry.get(envelope.kid)
    wanted = DOMAIN_PURPOSE[expected]
    if entry.purpose != wanted:
        raise SignatureError(
            f"key {entry.kid!r} is pinned for {entry.purpose.value}, not {wanted.value}")
    if issuer_id is not None and entry.issuer_id != issuer_id:
        raise SignatureError(f"key {entry.kid!r} does not belong to issuer {issuer_id!r}")
    try:
        entry.key().verify(bytes.fromhex(envelope.sig), signing_input(expected, payload))
    except InvalidSignature as exc:
        raise SignatureError("signature does not verify") from exc
    return entry


# --------------------------------------------------------------------------- demo keystore


class DemoKeystore:
    """The demo signing keys for every simulated party, stored as 0600 PEM files."""

    def __init__(self, root: Path | str = DEFAULT_KEY_DIR, *, seed: str | None = None,
                 specs: dict[str, tuple[KeyPurpose, str]] | None = None):
        self.root = Path(root)
        self.seed = seed
        self.specs = dict(DEMO_KEYS if specs is None else specs)
        self._cache: dict[str, Ed25519PrivateKey] = {}

    def ensure(self) -> "DemoKeystore":
        """Create any missing keys and (re)write ``trust_registry.json`` beside them."""
        for kid in self.specs:
            self.private_key(kid)
        self.registry().save(self.root / REGISTRY_FILE)
        return self

    @property
    def registry_path(self) -> Path:
        return self.root / REGISTRY_FILE

    def private_key(self, kid: str) -> Ed25519PrivateKey:
        if kid not in self.specs:
            raise SignatureError(f"unknown demo key {kid!r}")
        if kid not in self._cache:
            self._cache[kid] = self._load_or_create(kid)
        return self._cache[kid]

    def public_key_hex(self, kid: str) -> str:
        return public_key_hex(self.private_key(kid))

    def registry(self) -> TrustRegistry:
        reg = TrustRegistry()
        for kid, (purpose, issuer_id) in self.specs.items():
            reg.add(kid, self.public_key_hex(kid), purpose, issuer_id)
        return reg

    def sign(self, payload: Any, *, kid: str, domain: SigningDomain | str) -> SignatureEnvelope:
        """Sign with a demo key, refusing a domain the key's purpose does not allow."""
        domain = SigningDomain(domain)
        purpose = self.specs.get(kid, (None, None))[0]
        if purpose != DOMAIN_PURPOSE[domain]:
            raise SignatureError(f"key {kid!r} may not sign in domain {domain.value}")
        return sign(payload, private_key=self.private_key(kid), kid=kid, domain=domain)

    def _path(self, kid: str) -> Path:
        return self.root / f"{kid}.pem"

    def _derive(self, kid: str) -> Ed25519PrivateKey:
        material = hashlib.sha256(f"alibi-demo-key|{self.seed}|{kid}".encode("utf-8")).digest()
        return Ed25519PrivateKey.from_private_bytes(material)

    def _load_or_create(self, kid: str) -> Ed25519PrivateKey:
        path = self._path(kid)
        if path.exists():
            return _load_pem(path)
        key = self._derive(kid) if self.seed is not None else Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        self.root.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:  # another process created it first
            return _load_pem(path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        os.chmod(path, 0o600)
        return key


def _load_pem(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError(f"{path.name} is not an Ed25519 private key")
    return key


def load_registry(path: Path | str = DEFAULT_KEY_DIR / REGISTRY_FILE) -> TrustRegistry:
    return TrustRegistry.load(path)
