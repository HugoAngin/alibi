"""Verify an exported Alibi context package offline against separately pinned public keys."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.eval.sharing_delivery import (
    MAX_PACKAGE_BYTES,
    TrustRegistry,
    VerificationError,
    verify_package,
)


def _read_bytes(path: Path) -> bytes:
    with path.open("rb") as source:
        return source.read(MAX_PACKAGE_BYTES + 1)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _read_object(path: Path, error_code: str) -> dict:
    raw = _read_bytes(path)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise VerificationError("too_large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise VerificationError(error_code) from exc
    if not isinstance(value, dict):
        raise VerificationError(error_code)
    return value


def _load_registry(path: Path) -> TrustRegistry:
    data = _read_object(path, "invalid_registry")
    keys = data.get("keys")
    fields = {"key_id", "purpose", "owner", "public_key"}
    if set(data) != {"keys"} or not isinstance(keys, list):
        raise VerificationError("invalid_registry")
    for entry in keys:
        if not isinstance(entry, dict) or set(entry) != fields or any(
            not isinstance(value, str) for value in entry.values()
        ):
            raise VerificationError("invalid_registry")
    try:
        return TrustRegistry.from_json(data)
    except (ValueError, VerificationError) as exc:
        raise VerificationError("invalid_registry") from exc


def _evaluation_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VerificationError("invalid_time") from exc
    if timestamp.utcoffset() != timedelta(0):
        raise VerificationError("invalid_time")
    return timestamp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="original signed JSON envelope, as exported")
    parser.add_argument("--registry", type=Path, required=True,
                        help="trusted JSON public key registry in TrustRegistry.to_json() format")
    parser.add_argument("--recipient", required=True, help="expected audience, e.g. bank_a")
    parser.add_argument("--at", metavar="UTC_TIME",
                        help="evaluation time, e.g. 2026-11-08T10:00:00Z (default: current UTC time)")
    parser.add_argument("--issuer-proof", type=Path, action="append", default=[],
                        help="issuer-signed record envelope for a full attachment (repeatable)")
    args = parser.parse_args(argv)
    try:
        now = _evaluation_time(args.at)
        registry = _load_registry(args.registry)
        proofs = [_read_object(path, "invalid_issuer_proof") for path in args.issuer_proof]
        result = verify_package(_read_bytes(args.package), recipient=args.recipient,
                                registry=registry, now=now, issuer_records=proofs)
    except VerificationError as exc:
        code = exc.code
    except OSError:
        code = "file_error"
    except (RecursionError, ValueError):
        code = "malformed"
    else:
        print(json.dumps({"status": "verified", **result.to_dict()}, sort_keys=True))
        return 0
    print(json.dumps({"status": "refused", "code": code}, sort_keys=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
