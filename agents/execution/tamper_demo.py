"""Live tamper demo for the risk-controls moment: flip bytes in an exported package, verify again.

    uv run --frozen python -m agents.execution.tamper_demo                    # build, verify, tamper, verify
    uv run --frozen python -m agents.execution.tamper_demo --file PKG.json    # tamper an exported package
    uv run --frozen python -m agents.execution.tamper_demo --verify PKG.json  # just verify a file as bank_a

Without --file it builds Maya's supported package through the real alibi API (quick start in
alibi/CONTRACTS.md) in a throwaway store and keystore under data/alibi/tamper_demo/, writes the
exported JSON to data/alibi/tamper_demo/package.json, and then:

  1. verifies it as Bank A                       -> accepted
  2. changes the credit amount, the first `amount_pence` field equal to 216000 inside the signed
     body, to 261000, and prints its byte offset    -> rejected, with the verifier's own reasons
     (the edit is located as a JSON field, never as a bare byte match, so a digest or signature
     that happens to contain "216000" is never touched; the edited document is re-parsed to
     prove that this one field, and nothing else, changed)
  3. re-addresses the original to Bank B          -> rejected (signature and recipient binding)

Everything is synthetic: demo keys, demo master key, Maya's fixture. data/ is git-ignored.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "data" / "alibi" / "tamper_demo"
ORIGINAL, TAMPERED = b"216000", b"261000"
# The credit amount as a JSON number in the canonical encoding: `"amount_pence":216000` followed by
# `,` or `}`. Quoted strings (hex digests, base64 signatures, prose) can never match.
AMOUNT_FIELD = re.compile(rb'"amount_pence":(' + ORIGINAL + rb')(?=[,}])')
DEMO_MASTER_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"SYNTHETIC tamper demo master key").digest())


def build_package() -> tuple[bytes, object, object]:
    from agents.lead.api import (approve, build_review, default_keystore, load_fixture, open_store,
                                 run_investigation, sign_and_package)

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    store = open_store(WORK / "passport.db", master_key=DEMO_MASTER_KEY)
    keys = default_keystore(WORK / "keys")
    try:
        fx = load_fixture(store, "supported", keystore=keys)
        now = fx["now"]
        run_investigation(store, "rfc_a_001", settlement_lookup=fx["settlement_lookup"],
                          registry=keys.registry(), now=now)
        draft = build_review(store, "rfc_a_001", now=now)
        approval = approve(store, draft.draft_id, holder_id="holder_maya", now=now)
        package = sign_and_package(store, approval.approval_id, keystore=keys, now=now)
    finally:
        store.close()
    raw = json.dumps(package.to_json_dict(), sort_keys=True, separators=(",", ":")).encode()
    return raw, keys.registry(), now


def verify(raw: bytes, registry, now, audience: str = "bank_a") -> dict:
    from agents.lead.api import verify_package

    result = verify_package(raw, expected_audience=audience, registry=registry, now=now)
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    auth = data.get("authentication") or {}
    sources = data.get("sources") or []
    return {"accepted": data.get("accepted"), "authentication_valid": auth.get("valid"),
            "reasons": auth.get("reasons") or data.get("errors") or [],
            "sources_checked": len(sources),
            "sources_lower_trust": sum(1 for s in sources if s.get("lower_trust")),
            "claims": [(c.get("question_id"), c.get("status")) for c in data.get("claims") or []]}


def _leaves(node, path: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def changed_fields(before: bytes, after: bytes) -> list[tuple[str, object, object]]:
    """Every leaf whose value differs between two JSON documents of the same shape."""
    a, b = dict(_leaves(json.loads(before))), dict(_leaves(json.loads(after)))
    return [(p, a.get(p), b.get(p)) for p in sorted(set(a) | set(b)) if a.get(p) != b.get(p)]


def tamper(raw: bytes) -> tuple[bytes, int, str]:
    """Change the credit amount inside the signed body. Returns the bytes, the byte offset and the
    JSON path of the edited field, after checking that it is the only field that changed."""
    body_at = raw.find(b'"body":{')
    match = AMOUNT_FIELD.search(raw, body_at if body_at >= 0 else 0)
    if body_at < 0 or not match:
        raise SystemExit("the signed body has no amount_pence of 216000 to change")
    offset = match.start(1)
    bad = raw[:offset] + TAMPERED + raw[offset + len(ORIGINAL):]
    changes = changed_fields(raw, bad)
    if len(changes) != 1 or not changes[0][0].startswith(".body.") \
            or not changes[0][0].endswith(".amount_pence") \
            or (changes[0][1], changes[0][2]) != (int(ORIGINAL), int(TAMPERED)):
        raise SystemExit(f"tamper did not change exactly the credit amount: {changes}")
    return bad, offset, changes[0][0]


def readdress(raw: bytes) -> bytes:
    return raw.replace(b'"audience":"bank_a"', b'"audience":"bank_b"', 1)


def _show(title: str, result: dict) -> None:
    mark = "ACCEPTED" if result["accepted"] else "REJECTED"
    print(f"\n{title}: {mark}")
    print(f"  authentication valid: {result['authentication_valid']}")
    for r in result["reasons"]:
        print(f"  reason: {r}")
    if result["accepted"]:
        print(f"  sources checked: {result['sources_checked']} ({result['sources_lower_trust']} at lower trust)")
        print(f"  claims: {', '.join(f'{q} {s}' for q, s in result['claims'])}")


def run() -> dict:
    raw, registry, now = build_package()
    (WORK / "package.json").write_bytes(raw)
    print(f"package: {(WORK / 'package.json').relative_to(ROOT)}  sha256 {hashlib.sha256(raw).hexdigest()[:16]}  {len(raw)} bytes")
    before = verify(raw, registry, now)
    _show("1. Bank A verifies the exported package", before)

    bad, offset, field = tamper(raw)
    (WORK / "package_tampered.json").write_bytes(bad)
    ctx = raw[max(0, offset - 24): offset + 12].decode(errors="replace")
    print(f"\n2. Tamper: {field}, bytes {offset}..{offset + len(ORIGINAL) - 1}  {ORIGINAL.decode()} -> {TAMPERED.decode()}")
    print(f"   context: ...{ctx}...")
    after = verify(bad, registry, now)
    _show("   Bank A verifies the tampered package", after)

    moved = readdress(raw)
    redirected = verify(moved, registry, now, audience="bank_b")
    _show("3. Same package re-addressed and sent to Bank B", redirected)
    return {"before": before, "after": after, "readdressed": redirected, "offset": offset,
            "field": field, "changed": changed_fields(raw, bad)}


def main() -> None:
    if "--verify" in sys.argv or "--file" in sys.argv:
        flag = "--verify" if "--verify" in sys.argv else "--file"
        path = Path(sys.argv[sys.argv.index(flag) + 1])
        raw = path.read_bytes()
        from agents.lead.api import default_keystore

        keys_dir = WORK / "keys" if (WORK / "keys").exists() else None
        registry = default_keystore(keys_dir).registry() if keys_dir else None
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        _show(f"verify {path.name} as bank_a", verify(raw, registry, now))
        if flag == "--file":
            bad, offset, field = tamper(raw)
            out = path.with_name(path.stem + "_tampered.json")
            out.write_bytes(bad)
            print(f"\nTamper: {field}, bytes {offset}..{offset + len(ORIGINAL) - 1}  {ORIGINAL.decode()} -> {TAMPERED.decode()}  -> {out}")
            _show("verify tampered copy as bank_a", verify(bad, registry, now))
        return
    result = run()
    ok = result["before"]["accepted"] and not result["after"]["accepted"] and not result["readdressed"]["accepted"]
    print(json.dumps({"tamper_demo_ok": int(bool(ok)), "tamper_offset": result["offset"]}))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
