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

Result on the real store (agents.lead.api.open_store, 2026-11-08): with the master key supplied
(ALIBI_MASTER_KEY or the master_key argument) -> blocked; in the default layout the store writes
master.key beside passport.db -> not_blocked (key co-located), though no canary is readable at rest.

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
    if hasattr(store, "close"):
        store.close()
        found_after = inspect_store(root, canaries, [k for k in store.key_material if k])
        seen = {(f["path"], f["kind"], f["encoding"]) for f in found["findings"]}
        found["findings"] += [f for f in found_after["findings"] if (f["path"], f["kind"], f["encoding"]) not in seen]
        found["files"] = found_after["files"] if len(found_after["files"]) >= len(found["files"]) else found["files"]
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


class AlibiStore:
    """The real passport store (agents.lead.api.open_store), in one of two layouts.

    default     open_store(<root>/passport.db) with ALIBI_MASTER_KEY unset, keystore at the default
                sibling <root>/keys: mirrors data/alibi/ out of the box
    configured  master key passed in (as from ALIBI_MASTER_KEY in a secret store), keystore in a
                separate directory outside the data root
    Canaries go in through the authorised path (load_fixture, then import_self_supplied) and are
    read back through it (Store.get_raw_payload, decrypted).
    """

    def __init__(self, root: Path, layout: str = "default", outside: Path | None = None):
        self.root, self.layout = Path(root), layout
        self.outside = Path(outside) if outside else None
        self.master = None
        self.key_material: list[bytes] = []
        self._store = self._record = None

    def persist(self, case):
        from agents.lead.api import default_keystore, import_self_supplied, load_fixture, open_store

        self.root.mkdir(parents=True, exist_ok=True)
        saved = os.environ.pop("ALIBI_MASTER_KEY", None)
        try:
            if self.layout == "configured":
                self.master = base64.urlsafe_b64encode(hashlib.sha256(b"SYNTHETIC SP-47 master key").digest())
                store = open_store(self.root / "passport.db", master_key=self.master)
                keys = default_keystore(self.outside / "keys")
            else:
                store = open_store(self.root / "passport.db")
                keys = default_keystore(self.root / "keys")
                self.master = (self.root / "master.key").read_bytes().strip()
        finally:
            if saved is not None:
                os.environ["ALIBI_MASTER_KEY"] = saved
        self._store = store
        self.key_material = [self.master, base64.urlsafe_b64decode(self.master)]
        fx = load_fixture(store, "supported", keystore=keys)
        texts = [d["event"][f] for d in case["merchant_deliveries"] for f in ("item", "message")
                 if isinstance(d["event"].get(f), str)]
        result = import_self_supplied(store, {
            "kind": "receipt", "amount_pence": 4500, "currency": "GBP", "occurred_at": "2026-10-14T10:00:00Z",
            "status": "paid", "schema_version": "alibi.merchant_event.v1", "subject_ref": fx["subject_ref"],
            "text": " | ".join(texts),
            "items": [{"category": "synthetic", "description": t, "quantity": 1, "unit_amount_pence": 1} for t in texts],
        }, subject_ref=fx["subject_ref"], now=fx["now"])
        self._record = (result.record.record_id, result.record.revision)

    def read_all(self):
        return json.dumps(self._store.get_raw_payload(*self._record)).encode()

    def close(self):
        if self._store is not None:
            self._store.close()


def app_store(layout: str = "default", base: Path | None = None):
    """The real passport store adapter, or None if alibi's API is not importable."""
    try:
        import agents.lead.api  # noqa: F401
    except ImportError:
        return None
    base = Path(base or tempfile.mkdtemp(prefix="sp47-"))
    return AlibiStore(base / "data_root", layout, outside=base / "secrets")


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
        for layout in ("default", "configured"):
            app = probe(app_store(layout))
            print(f"alibi store {layout:10} {app['outcome']:12} {app.get('reason', '')}")
            for f in app.get("findings", []):
                print(f"  finding {f['kind']} in {f['path']} ({f['view']}, {f['encoding']}, {f['value']})")
            if app.get("key_named_files"):
                print(f"  key-named files beside the data: {app['key_named_files']}")
            if app.get("files"):
                print(f"  scanned {app['files_scanned']} files, {app['stored_bytes']} bytes, {app['canaries']} canaries")
            code = {BLOCKED: 1, NOT_BLOCKED: 0, NOT_RUN: -1}[app["outcome"]]
            summary[f"sp47_{layout}_blocked"] = code
    print(json.dumps(summary))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
