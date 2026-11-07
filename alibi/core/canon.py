"""Canonical JSON (RFC 8785, JCS) and SHA-256 digests.

Everything Alibi hashes or signs goes through ``canonical_bytes``: keys sorted, no
insignificant whitespace, UTF-8, I-JSON number rules. Pydantic models are converted with
``to_json_dict`` semantics (JSON mode, aliases, ``None`` fields omitted) first.
"""

import hashlib
import json
import uuid
from typing import Any

import rfc8785
from pydantic import BaseModel


class CanonError(ValueError):
    """The value cannot be canonicalised, or raw JSON is malformed or ambiguous."""


def to_plain(obj: Any) -> Any:
    """Recursively turn pydantic models into JSON-ready dicts; leave plain JSON as is."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(obj, dict):
        return {key: to_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(value) for value in obj]
    return obj


def canonical_bytes(obj: Any) -> bytes:
    """RFC 8785 canonical UTF-8 bytes of a JSON value or pydantic model."""
    try:
        return rfc8785.dumps(to_plain(obj))
    except rfc8785.CanonicalizationError as exc:
        raise CanonError(f"cannot canonicalise value: {exc}") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """sha256 hex of the canonical bytes."""
    return sha256_hex(canonical_bytes(obj))


def make_id(prefix: str, *parts: Any) -> str:
    """Deterministic identifier from JSON-able parts, e.g. ``make_id("clm", run_id, "q1")``."""
    return f"{prefix}_{digest(list(parts))[:24]}"


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonError(f"duplicate JSON key: {key!r}")
        out[key] = value
    return out


def _no_constants(name: str) -> Any:
    raise CanonError(f"non-standard JSON constant: {name}")


def parse_json(raw: bytes | str) -> Any:
    """Strict JSON parsing for inbound payloads: rejects duplicate keys, NaN and Infinity."""
    try:
        return json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CanonError(f"invalid JSON: {exc}") from exc
