"""Demo keys for the simulated services.

The core's ``DEMO_KEYS`` pin ``merchant_demo`` to the S1 fixture issuer ``threadline``. The
team's cases are issued by ``mock-marketplace`` (``agents.data.api``), and ``ingest_merchant_event``
refuses a signature whose pinned issuer differs from ``event.issuer_id``. So the connectors use
one more record-signing key, ``merchant_mock``, pinned to ``mock-marketplace``. Everything else
(``alibi_demo``, ``bank_a``, ``bank_b``) is the core's key, so a registry written here is a
superset of the core's and verifies S1 fixture packages too.

``connector_keystore()`` creates any missing key under ``data/alibi/keys`` (git-ignored) and
rewrites ``trust_registry.json`` with all five keys. Note: ``alibi.api.default_keystore()``
rewrites that file with the core's four keys, dropping ``merchant_mock``; pass this keystore and
its registry explicitly to every passport call (see ``alibi/NEEDS.md``).
"""

from pathlib import Path
from typing import Any

from alibi.core.schemas import KeyPurpose, SigningDomain
from alibi.core.signing import DEFAULT_KEY_DIR, DEMO_KEYS, DemoKeystore, TrustRegistry

__all__ = [
    "BANK_KIDS", "CASE_ISSUER", "CONNECTOR_KEYS", "MERCHANT_KID", "connector_keystore",
    "sign_merchant_event", "sign_request",
]

CASE_ISSUER = "mock-marketplace"        # issuer_id on every team case record
MERCHANT_KID = "merchant_mock"          # record-signing key pinned to CASE_ISSUER
BANK_KIDS = {"bank_a": "bank_a", "bank_b": "bank_b"}   # recipient -> request-signing kid

CONNECTOR_KEYS: dict[str, tuple[KeyPurpose, str]] = {
    **DEMO_KEYS,
    MERCHANT_KID: (KeyPurpose.RECORD_SIGNING, CASE_ISSUER),
}


def connector_keystore(root: Path | str = DEFAULT_KEY_DIR, *, seed: str | None = None,
                       ensure: bool = True) -> DemoKeystore:
    """The keystore the simulated services sign with: the core's demo keys plus
    ``merchant_mock``. ``seed`` derives keys deterministically (tests only)."""
    keystore = DemoKeystore(root, seed=seed, specs=CONNECTOR_KEYS)
    return keystore.ensure() if ensure else keystore


def sign_merchant_event(body: dict[str, Any], keystore: DemoKeystore,
                        kid: str = MERCHANT_KID) -> dict[str, Any]:
    """The wire shape the passport ingests: ``{"event": body, "signature": envelope}``, signed
    in the record domain. ``body`` is a core ``MerchantEvent`` as JSON (see case_source)."""
    envelope = keystore.sign(body, kid=kid, domain=SigningDomain.RECORD)
    return {"event": body, "signature": envelope.to_json_dict()}


def sign_request(body: dict[str, Any], keystore: DemoKeystore, kid: str) -> dict[str, Any]:
    """A bank-signed Request for Context: ``{"request": body, "signature": envelope}``."""
    envelope = keystore.sign(body, kid=kid, domain=SigningDomain.REQUEST)
    return {"request": body, "signature": envelope.to_json_dict()}


def registry_of(keystore: DemoKeystore) -> TrustRegistry:
    return keystore.registry()
