"""Demo keys for the simulated services.

The core's ``DEMO_KEYS`` pin ``merchant_demo`` to the S1 fixture issuer ``threadline``. The
team's cases are issued by ``mock-marketplace`` (``agents.data.api``), and ``ingest_merchant_event``
refuses a signature whose pinned issuer differs from ``event.issuer_id``. So the connectors use
one more record-signing key, ``merchant_mock``, pinned to ``mock-marketplace``. Everything else
(``alibi_demo``, ``bank_a``, ``bank_b``) is the core's key, so a registry written here is a
superset of the core's and verifies S1 fixture packages too.

``CONNECTOR_KEYS`` is the one demo key spec for every party: the core's ``DEMO_KEYS`` plus the
app's two case-issuer keys (``mock_marketplace`` for ``mock-marketplace``, ``second_platform``
for ``second-platform``, which ``alibi/app/cases.py`` signs replayed case records with). The app
keystore (``alibi.app.config.APP_KEYS``) is this same dict, so the connector keystore, the app
keystore and the ``trust_registry.json`` either of them writes carry every kid, and a package or
record signed under one process's keystore verifies against the other's registry when both read
the same key directory (``data/alibi/keys`` in the demo).

``connector_keystore()`` creates any missing key under ``data/alibi/keys`` (git-ignored) and
rewrites ``trust_registry.json`` with all of them. Note: ``alibi.api.default_keystore()``
rewrites that file with the core's keys only, dropping the app's two; pass this keystore and
its registry explicitly to every passport call (see ``alibi/NEEDS.md``).
"""

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

from alibi.core.schemas import KeyPurpose, SigningDomain
from alibi.core.signing import DEFAULT_KEY_DIR, DEMO_KEYS, DemoKeystore, SignatureError, TrustRegistry

__all__ = [
    "APP_ISSUER_KIDS", "BANK_KIDS", "CASE_ISSUER", "CONNECTOR_KEYS", "MERCHANT_KID", "connector_keystore", "kid_for_issuer",
    "sign_merchant_event", "sign_request",
]

CASE_ISSUER = "mock-marketplace"        # issuer_id on every team case record
MERCHANT_KID = "merchant_mock"          # record-signing key pinned to CASE_ISSUER
BANK_KIDS = {"bank_a": "bank_a", "bank_b": "bank_b"}   # recipient -> request-signing kid

# The app keystore's record-signing keys for the two case issuers (``alibi/app/cases.py``).
APP_ISSUER_KIDS = {"mock_marketplace": CASE_ISSUER, "second_platform": "second-platform"}

CONNECTOR_KEYS: dict[str, tuple[KeyPurpose, str]] = {
    **DEMO_KEYS,
    MERCHANT_KID: (KeyPurpose.RECORD_SIGNING, CASE_ISSUER),
    **{kid: (KeyPurpose.RECORD_SIGNING, issuer) for kid, issuer in APP_ISSUER_KIDS.items()},
}


def connector_keystore(root: Path | str = DEFAULT_KEY_DIR, *, seed: str | None = None,
                       ensure: bool = True,
                       trusted_issuers: Mapping[str, str] | None = None) -> DemoKeystore:
    """The keystore the simulated services sign with: ``CONNECTOR_KEYS`` (the core's demo keys,
    ``merchant_mock`` and the app's two issuer keys). ``seed`` derives keys deterministically
    (tests and disposable synthetic harnesses only). A private high-entropy seed allows
    fresh isolated workers to share predeclared public-key pins. ``trusted_issuers`` adds
    explicit ``kid -> issuer_id`` pins for synthetic
    services. Only trusted harness setup may supply it; never derive it from case records.
    New keys are record-signing only, cannot replace existing pins, and use safe filenames.
    """
    specs = dict(CONNECTOR_KEYS)
    if trusted_issuers is not None:
        if not isinstance(trusted_issuers, Mapping):
            raise ValueError("trusted_issuers must map key ids to issuer ids")
        for kid, issuer in trusted_issuers.items():
            if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", kid):
                raise ValueError("configured issuer key id must be a safe filename token")
            if not isinstance(issuer, str) or not issuer or issuer != issuer.strip() \
                    or any(ord(char) < 32 or ord(char) == 127 for char in issuer):
                raise ValueError("configured issuer id must be a nonempty string without control characters")
            if kid in specs or any(pin == issuer for _, pin in specs.values()):
                raise ValueError("configured issuer key or issuer collides with an existing pin")
            specs[kid] = (KeyPurpose.RECORD_SIGNING, issuer)
    keystore = DemoKeystore(root, seed=seed, specs=specs)
    return keystore.ensure() if ensure else keystore


def kid_for_issuer(issuer_id: Any) -> str:
    """The record-signing key id for a case issuer: ``merchant_mock`` for the mock marketplace,
    the app's issuer key for the second platform (``second_platform``)."""
    for kid, issuer in APP_ISSUER_KIDS.items():
        if issuer == issuer_id and kid != "mock_marketplace":
            return kid
    return MERCHANT_KID


def sign_merchant_event(body: dict[str, Any], keystore: DemoKeystore,
                        kid: str | None = None) -> dict[str, Any]:
    """The wire shape the passport ingests: ``{"event": body, "signature": envelope}``, signed
    in the record domain. ``body`` is a core ``MerchantEvent`` as JSON (see case_source).
    An event selects among already configured pins; it never creates or changes a pin.
    Unknown issuers and explicitly mismatched keys fail closed before signing.
    """
    issuer = body.get("issuer_id") if isinstance(body, dict) else None
    if not isinstance(issuer, str) or not issuer:
        raise SignatureError("merchant issuer has no configured record-signing key")
    if kid is None:
        matches = sorted(key for key, spec in keystore.specs.items()
                         if spec == (KeyPurpose.RECORD_SIGNING, issuer))
        preferred = kid_for_issuer(issuer)
        kid = preferred if preferred in matches else next(iter(matches), None)
    if kid is None or keystore.specs.get(kid) != (KeyPurpose.RECORD_SIGNING, issuer):
        raise SignatureError("merchant issuer has no matching configured record-signing key")
    envelope = keystore.sign(body, kid=kid, domain=SigningDomain.RECORD)
    return {"event": body, "signature": envelope.to_json_dict()}


def sign_request(body: dict[str, Any], keystore: DemoKeystore, kid: str) -> dict[str, Any]:
    """A bank-signed Request for Context: ``{"request": body, "signature": envelope}``."""
    envelope = keystore.sign(body, kid=kid, domain=SigningDomain.REQUEST)
    return {"request": body, "signature": envelope.to_json_dict()}


def registry_of(keystore: DemoKeystore) -> TrustRegistry:
    return keystore.registry()
