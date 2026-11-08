"""SP-47: evidence is encrypted at rest and the key is not stored with it.

    uv run --frozen python -m agents.execution.at_rest_probe              # app store + controls
    uv run --frozen python -m agents.execution.at_rest_probe --controls   # controls only

The probe, given a store:
  1. plants unique canaries in the free-text fields of public synthetic case 1 and persists it
     through the store's authorised (app) path;
  2. reads it back through that path with the key: the canaries must come back, which proves the
     evidence was really persisted and is readable by the app;
  3. walks the raw store directory, sidecars included (journals, WAL, indexes, temp and hidden
     files), WITHOUT the key, and searches every file for each canary and for the key material in
     raw, UTF-16, base64 (all three alignments), hex, URL-encoded and gzip-wrapped forms.

Outcomes, as in eval's HTTP runtime:
    blocked      persisted, readable through the app, no canary and no key material in the raw store
    not_blocked  a canary or the key was found at rest (the finding names the file and encoding)
    not_run      no store, or persistence could not be established through the app path

The report holds outcomes and redacted evidence only: relative file paths, the encoding that
matched and a short sha256 of the matched value, never the value itself.

A store adapter is any object with:
    root: pathlib.Path                  the directory the app persists to
    key_material: list[bytes]           every secret that must not sit beside the data
    persist(case: dict) -> None         write through the app path
    read_all() -> bytes | str           read back through the app path, decrypted
The app adapter is `app_store()`; it returns None until the passport store is published on main.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

BLOCKED, NOT_BLOCKED, NOT_RUN = "blocked", "not_blocked", "not_run"
MAX_FILE = 64 * 1024 * 1024


# --------------------------------------------------------------------------- canaries

def planted_case(run_id: str | None = None) -> tuple[dict, list[bytes]]:
    """Case 1 with a unique canary appended to every free-text merchant field."""
    from agents.data.api import canonical_case

    run_id = run_id or uuid.uuid4().hex[:12]
    case = copy.deepcopy(canonical_case())
    canaries = []
    for i, d in enumerate(case["merchant_deliveries"]):
        e = d["event"]
        for field in ("item", "message"):
            if isinstance(e.get(field), str):
                token = f"ALIBI-CANARY-{run_id}-{i}-{field}"
                e[field] = f"{e[field]} {token}"
                canaries.append(token.encode())
    return case, canaries


def _forms(secret: bytes) -> dict[str, list[bytes]]:
    forms = {
        "raw": [secret],
        "utf16le": [secret.decode("latin-1").encode("utf-16-le")],
        "utf16be": [secret.decode("latin-1").encode("utf-16-be")],
        "hex": [secret.hex().encode(), secret.hex().upper().encode()],
        "url": [quote(secret.decode("latin-1"), safe="").encode()],
    }
    # base64 of a substring is only findable by alignment; take the stable middle of each.
    b64 = []
    for shift in range(3):
        chunk = secret[shift:]
        enc = base64.b64encode(chunk)
        stable = enc[: (len(chunk) // 3) * 4]
        if len(stable) >= 8:
            b64.append(stable)
            b64.append(stable.replace(b"+", b"-").replace(b"/", b"_"))
    forms["base64"] = b64
    return forms


def _short(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()[:12]


def _views(data: bytes) -> list[tuple[str, bytes]]:
    views = [("file", data)]
    if data[:2] == b"\x1f\x8b":
        try:
            views.append(("gunzip", gzip.decompress(data)))
        except OSError:
            pass
    return views


# --------------------------------------------------------------------------- inspection

def inspect_store(root: Path, canaries: list[bytes], key_material: list[bytes]) -> dict:
    """Search every file under root, without any key, for canaries and key material."""
    root = Path(root)
    files, findings = [], []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = str(path.relative_to(root))
        size = path.stat().st_size
        files.append({"path": rel, "bytes": size})
        if size > MAX_FILE:
            findings.append({"kind": "unscanned", "path": rel, "why": "file over 64 MiB"})
            continue
        data = path.read_bytes()
        for view, blob in _views(data):
            for kind, secrets in (("canary", canaries), ("key", key_material)):
                for secret in secrets:
                    for enc, needles in _forms(secret).items():
                        if any(n and n in blob for n in needles):
                            findings.append({"kind": kind, "path": rel, "view": view, "encoding": enc,
                                             "value": _short(secret)})
                            break
    hits = [f for f in findings if f["kind"] in ("canary", "key")]
    return {"files": files, "findings": hits, "unscanned": [f for f in findings if f["kind"] == "unscanned"]}


def probe(store, *, run_id: str | None = None) -> dict:
    """Run SP-47 against one store adapter and return a redacted report."""
    report = {"probe_id": "SP-47"}
    if store is None:
        return {**report, "outcome": NOT_RUN, "reason": "no store adapter: the passport store is not published"}
    root = Path(store.root)
    case, canaries = planted_case(run_id)
    try:
        store.persist(case)
        back = store.read_all()
    except Exception as exc:
        return {**report, "outcome": NOT_RUN, "reason": f"app path failed: {type(exc).__name__}"}
    back = back.encode() if isinstance(back, str) else bytes(back)
    missing = [_short(c) for c in canaries if c not in back]
    if missing:
        return {**report, "outcome": NOT_RUN, "reason": "persistence not established: canaries not readable through the app path",
                "missing": missing}
    if not root.is_dir() or not any(p.is_file() for p in root.rglob("*")):
        return {**report, "outcome": NOT_RUN, "reason": "store directory missing or empty after persist"}
    found = inspect_store(root, canaries, [k for k in store.key_material if k])
    key_files = [f["path"] for f in found["files"]
                 if any(w in Path(f["path"]).name.lower() for w in ("key", ".env", "secret"))]
    outcome = NOT_BLOCKED if found["findings"] or key_files else BLOCKED
    if found["unscanned"] and outcome == BLOCKED:
        outcome = NOT_RUN
    return {**report, "outcome": outcome,
            "canaries": len(canaries), "files_scanned": len(found["files"]),
            "stored_bytes": sum(f["bytes"] for f in found["files"]),
            "findings": found["findings"], "key_named_files": key_files, "unscanned": found["unscanned"],
            "files": found["files"]}


# --------------------------------------------------------------------------- stores

class PlaintextStore:
    """Control: writes evidence as JSON, plus a plaintext journal sidecar. Must be not_blocked."""

    def __init__(self, root: Path):
        self.root, self.key_material = Path(root), [b"k" * 32]

    def persist(self, case):
        (self.root / "evidence.json").write_text(json.dumps(case))
        (self.root / ".journal").write_text(json.dumps(case["merchant_deliveries"][-1]))

    def read_all(self):
        return (self.root / "evidence.json").read_bytes()


class EncryptedStore:
    """Control: AES-GCM per record, key held outside the store directory. Must be blocked."""

    def __init__(self, root: Path, key: bytes, *, key_beside_data: bool = False, plaintext_sidecar: bool = False):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.root, self.key_material = Path(root), [key]
        self._aead, self._beside, self._sidecar = AESGCM(key), key_beside_data, plaintext_sidecar

    def persist(self, case):
        blob = json.dumps(case).encode()
        nonce = os.urandom(12)
        (self.root / "evidence.bin").write_bytes(nonce + self._aead.encrypt(nonce, blob, b"alibi-evidence"))
        (self.root / "index.json").write_text(json.dumps({"case_number": case["case_number"], "records": len(case["merchant_deliveries"])}))
        if self._beside:
            (self.root / "store.key").write_bytes(base64.b64encode(self.key_material[0]))
        if self._sidecar:
            (self.root / "evidence.wal").write_text(json.dumps(case["merchant_deliveries"][0]["event"]))

    def read_all(self):
        raw = (self.root / "evidence.bin").read_bytes()
        return self._aead.decrypt(raw[:12], raw[12:], b"alibi-evidence")


class Base64Store(PlaintextStore):
    """Control: encoding is not encryption. Must be not_blocked."""

    def persist(self, case):
        (self.root / "evidence.b64").write_bytes(base64.b64encode(json.dumps(case).encode()))

    def read_all(self):
        return base64.b64decode((self.root / "evidence.b64").read_bytes())


def app_store():
    """The published passport store, once it lands on main. None until then."""
    try:
        from alibi.passport.store import probe_adapter  # expected hook; name to confirm with lead/S1
    except ImportError:
        return None
    return probe_adapter()


def controls(run_id: str = "control") -> dict:
    key = hashlib.sha256(b"SYNTHETIC at-rest control key").digest()
    out = {}
    with tempfile.TemporaryDirectory() as base:
        def fresh(name):
            d = Path(base) / name
            d.mkdir()
            return d
        out["plaintext"] = probe(PlaintextStore(fresh("plain")), run_id=run_id)["outcome"]
        out["base64"] = probe(Base64Store(fresh("b64")), run_id=run_id)["outcome"]
        out["ciphertext"] = probe(EncryptedStore(fresh("enc"), key), run_id=run_id)["outcome"]
        out["key_beside_data"] = probe(EncryptedStore(fresh("enc_key"), key, key_beside_data=True), run_id=run_id)["outcome"]
        out["plaintext_sidecar"] = probe(EncryptedStore(fresh("enc_wal"), key, plaintext_sidecar=True), run_id=run_id)["outcome"]
        missing = EncryptedStore(Path(base) / "absent", key)
        out["missing_store"] = probe(missing, run_id=run_id)["outcome"]
        out["no_adapter"] = probe(None)["outcome"]
    return out


EXPECTED_CONTROLS = {"plaintext": NOT_BLOCKED, "base64": NOT_BLOCKED, "ciphertext": BLOCKED,
                     "key_beside_data": NOT_BLOCKED, "plaintext_sidecar": NOT_BLOCKED,
                     "missing_store": NOT_RUN, "no_adapter": NOT_RUN}


def main() -> None:
    ctrl = controls()
    ok = ctrl == EXPECTED_CONTROLS
    for name, outcome in ctrl.items():
        print(f"control {name:18} {outcome:12} {'ok' if outcome == EXPECTED_CONTROLS[name] else 'UNEXPECTED'}")
    summary = {"sp47_controls_passed": sum(ctrl[k] == v for k, v in EXPECTED_CONTROLS.items()),
               "sp47_controls": len(EXPECTED_CONTROLS)}
    if "--controls" not in sys.argv:
        app = probe(app_store())
        print(f"app store          {app['outcome']:12} {app.get('reason', '')}")
        for f in app.get("findings", []):
            print(f"  finding {f['kind']} in {f['path']} ({f['view']}, {f['encoding']}, {f['value']})")
        summary["sp47_app_outcome"] = app["outcome"]
    print(json.dumps(summary))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
