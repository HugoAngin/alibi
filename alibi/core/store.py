"""Encrypted SQLite store for the passport.

One SQLite file (default ``data/alibi/passport.db``) holds merchant evidence ("sources"),
the bank feed, derived results (links, claims, alerts), policies, requests, drafts,
approvals, packages, deliveries, audit events and traces.

* Concurrency: one connection shared across threads behind an ``RLock``. Writes run in
  ``BEGIN IMMEDIATE`` transactions, so revision checks, invalidation and dispatch
  authorisation are serialised against updates. ``transaction()`` is re-entrant: a nested
  block is a savepoint, so a failure inside it undoes only its own writes.
* Encryption at rest: every object is stored as Fernet(canonical JSON) and model-validated
  on read. Clear columns hold only ids, revision numbers, active/status/state flags,
  dependency tokens and keyed event fingerprints (HMAC-SHA256 under a key derived from the
  master key). Amounts, references, descriptions, items and text exist only in ciphertext.
* Integrity: a decrypted object must match the clear columns of its row (ids, revision,
  subject, state), and the active revision must be the highest stored one. A mismatch means
  the file was edited without the key and raises ``StoreError``.
* Master key: the ``master_key`` argument, else ``ALIBI_MASTER_KEY``, else a key file beside
  the database, created atomically with mode 0600 on first use. It never enters the
  database; a stored key-check value makes a wrong key fail when the store opens.
* Sources are append-only revisions with exactly one active revision per record.
  Idempotency is by ``(connector, issuer_id, event_id)`` plus the payload digest. A higher
  revision is a correction: it becomes active and invalidates the links, claims, pending
  drafts and pending approvals that depend on the record. A lower revision is kept as stale
  history. Every revision keeps the record's issuer, subject, kind and source trust.
  Self-supplied evidence has its own namespace: its record ids start with ``self_``, which
  issuer evidence may not use, so an import can never occupy an issuer's record id.
* Derived results are saved against current evidence: a link, claim, draft or approval that
  cites a revision which is no longer active is stored already invalidated. Terminal states
  (invalidated, packaged drafts, consumed approvals) are never reverted.
* Requests are keyed by ``(issuer, request_id)``: a bank's externally chosen request id is its
  own namespace, so two banks may use the same id. ``get_request(request_id)`` without an
  issuer resolves a shared id to the request received first under it (storage order), so
  every object that names a bare ``request_id`` (claims, drafts, approvals, packages) keeps
  its meaning; pass ``issuer`` to reach a later bank's request under the same id.
"""

import base64
import functools
import hashlib
import hmac
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, TypeVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, TypeAdapter, ValidationError

from alibi.core import canon
from alibi.core.schemas import (
    Alert,
    Approval,
    AuditEvent,
    BankTxn,
    Calculation,
    Claim,
    ContextPackage,
    Delivery,
    Draft,
    DraftStatus,
    EvidenceRecord,
    EvidenceRef,
    Ident,
    Link,
    PackageBody,
    Policy,
    RecordKind,
    RequestForContext,
    ShortText,
    SourceTrust,
    TraceEvent,
    utc_now,
)

__all__ = [
    "DEFAULT_DB_PATH", "MASTER_KEY_ENV", "MAX_REVISION", "SELF_SUPPLIED_PREFIX",
    "AmbiguousRequest", "IdempotencyConflict", "NamespaceError", "NotFound", "PutResult",
    "RevisionConflict", "Store", "StoreError", "dependency_refs", "dependency_tokens",
    "load_master_key",
]

DEFAULT_DB_PATH = Path("data/alibi/passport.db")
MASTER_KEY_ENV = "ALIBI_MASTER_KEY"
KEY_FILE_NAME = "master.key"
SCHEMA_VERSION = "3"   # 2 -> 3: requests keyed by (issuer, request_id), the bank; migrated on open
SELF_SUPPLIED_PREFIX = "self_"
MAX_REVISION = 2**53 - 1
_KEY_CHECK = b"alibi.store.key-check.v1"
_FINGERPRINT_CONTEXT = b"alibi.store.event-fingerprint.v1"

M = TypeVar("M", bound=BaseModel)


# --------------------------------------------------------------------------- errors


class StoreError(Exception):
    """Base store error. Messages name ids and fields only, never payload contents."""


class IdempotencyConflict(StoreError):
    """The same event key or idempotency key arrived with different bytes."""


class RevisionConflict(StoreError):
    """The same (record_id, revision) arrived with different content, or a revision tried to
    change a record's issuer, subject, kind or source trust."""


class NamespaceError(StoreError):
    """A record id is in the wrong namespace: self-supplied ids start with ``self_`` and
    issuer evidence may not use that prefix."""


class NotFound(StoreError):
    """No stored object has that id or revision."""


class AmbiguousRequest(StoreError):
    """A bare ``request_id`` names requests from more than one bank; pass ``audience``.

    Request ids are the bank's own, so a request is identified by ``(audience, request_id)``.
    Only holder-side lookups can be ambiguous: bank-facing code always passes the bank."""


def _no_invalidations() -> dict[str, list[str]]:
    return {"links": [], "claims": [], "drafts": [], "approvals": []}


@dataclass(frozen=True)
class PutResult:
    """Outcome of storing one source record or bank transaction revision."""

    status: Literal["inserted", "duplicate", "corrected", "stale"]
    record: EvidenceRecord | BankTxn   # the stored row (the existing one on duplicate)
    active: bool                        # whether this row is the active revision now
    invalidated: dict[str, list[str]] = field(default_factory=_no_invalidations)


# --------------------------------------------------------------------------- master key


def _check_key(key: bytes | str, source: str) -> bytes:
    """Return ``key`` as bytes if it is a valid Fernet key. Never echoes the value."""
    try:
        raw = key.encode("ascii") if isinstance(key, str) else bytes(key)
        raw = raw.strip()
        Fernet(raw)
    except (TypeError, ValueError):
        raise StoreError(f"{source} is not a valid Fernet key "
                         "(expected 32 url-safe base64-encoded bytes)") from None
    return raw


def load_master_key(key_path: Path | str, *, create: bool = True) -> bytes:
    """The Fernet master key: ``ALIBI_MASTER_KEY`` if set, else the key file at ``key_path``.

    A missing key file is created (see ``_create_key_file``) when ``create`` is true;
    otherwise ``StoreError``. An empty key file holds no key, so nothing can have been
    encrypted with it: with ``create`` it is replaced, otherwise it is an error.
    """
    env = os.environ.get(MASTER_KEY_ENV, "").strip()
    if env:
        return _check_key(env, f"environment variable {MASTER_KEY_ENV}")
    path = Path(key_path)
    try:
        if path.exists():
            data = path.read_bytes()
            if data.strip():
                return _check_key(data, f"master key file {path}")
            if not create:
                raise StoreError(f"master key file {path} is empty: restore it or set "
                                 f"{MASTER_KEY_ENV}")
            path.unlink()
            return _create_key_file(path)
        if not create:
            raise StoreError(
                f"no master key for this database: set {MASTER_KEY_ENV} or restore {path}")
        return _create_key_file(path)
    except OSError as exc:
        raise StoreError(f"cannot use master key file {path}: {type(exc).__name__}") from None


def _create_key_file(path: Path) -> bytes:
    """Create ``path`` holding a new Fernet key, atomically and without overwriting.

    The key goes to a temporary file (mode 0600) that is flushed and fsynced, then
    published with a hard link, or where links are unsupported on Windows with a rename
    (which refuses to overwrite there). A crash never leaves a partial key file behind. If
    another opener published first, its complete key is returned instead.
    """
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass  # another opener won the race: use its key
        except OSError:
            if os.name != "nt":
                raise
            try:
                os.rename(tmp, path)
            except FileExistsError:
                pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return _check_key(path.read_bytes(), f"master key file {path}")


# --------------------------------------------------------------------------- helpers


def _calc_refs(calc: Calculation | None) -> list[EvidenceRef]:
    if calc is None:
        return []
    refs = [*calc.cited_refs, *(term.ref for term in calc.terms)]
    if calc.target_ref is not None:
        refs.append(calc.target_ref)
    if calc.timing is not None:
        refs += [calc.timing.settlement_ref, calc.timing.credit_ref]
    return refs


def _body_refs(body: PackageBody) -> list[EvidenceRef]:
    refs = [attachment.ref for attachment in body.attachments]
    for claim in body.claims:
        refs += [*claim.evidence_refs, *_calc_refs(claim.calculation)]
    return refs


def dependency_refs(obj: Link | Claim | Draft | Approval | ContextPackage) -> list[EvidenceRef]:
    """Revision-pinned refs a derived object depends on, sorted and de-duplicated.

    Link: txn_ref, record_ref, candidates. Claim: evidence_refs plus every calculation ref.
    Draft: source_revisions plus the body's refs. Approval: source_revisions. Package: the
    body's attachment refs and claim evidence/calculation refs.
    """
    if isinstance(obj, Link):
        refs = [obj.txn_ref, obj.record_ref, *obj.candidates]
    elif isinstance(obj, Claim):
        refs = [*obj.evidence_refs, *_calc_refs(obj.calculation)]
    elif isinstance(obj, Draft):
        refs = [*obj.source_revisions, *_body_refs(obj.body)]
    elif isinstance(obj, Approval):
        refs = list(obj.source_revisions)
    elif isinstance(obj, ContextPackage):
        refs = _body_refs(obj.body)
    else:
        raise TypeError(f"no dependency refs for {type(obj).__name__}")
    unique = {ref.key(): ref for ref in refs if ref is not None}
    return [unique[key] for key in sorted(unique)]


def dependency_tokens(obj: Link | Claim | Draft | Approval | ContextPackage) -> list[str]:
    """Revision-free tokens (``record:<id>``, ``txn:<id>``) a derived object depends on."""
    return sorted({ref.token() for ref in dependency_refs(obj)})


def _source_digest(record: EvidenceRecord) -> str:
    """Digest of a record's content: its event fields minus ``event_id``."""
    body = record.to_event().to_json_dict()
    body.pop("event_id", None)
    return canon.digest(body)


def _txn_digest(txn: BankTxn) -> str:
    """Digest of a bank txn's JSON minus provenance (retrieval time and cursor vary per fetch)."""
    body = txn.to_json_dict()
    body.pop("provenance", None)
    return canon.digest(body)


_SOURCE_IDENTITY = ("issuer_id", "subject_ref", "kind", "source_trust")
_TXN_IDENTITY = ("subject_ref", "account_ref")


def _check_identity(what: str, obj_id: str, current: Any, new: Any, names: tuple[str, ...]) -> None:
    changed = [name for name in names if getattr(current, name) != getattr(new, name)]
    if changed:
        raise RevisionConflict(
            f"{what} {obj_id!r} revision {new.revision} changes {', '.join(changed)}; every "
            f"revision must keep the same {', '.join(names)}")


def _check_namespace(record: EvidenceRecord) -> None:
    own = record.record_id.startswith(SELF_SUPPLIED_PREFIX)
    if record.source_trust == SourceTrust.SELF_SUPPLIED and not own:
        raise NamespaceError(f"self-supplied record ids must start with {SELF_SUPPLIED_PREFIX!r}; "
                             f"got {record.record_id!r}")
    if record.source_trust != SourceTrust.SELF_SUPPLIED and own:
        raise NamespaceError(f"record id {record.record_id!r} uses the {SELF_SUPPLIED_PREFIX!r} "
                             "prefix, which is reserved for self-supplied evidence")


_IDENT = TypeAdapter(Ident)
_SHORT_TEXT = TypeAdapter(ShortText)


def _errors(exc: ValidationError) -> str:
    """Field locations and messages only: pydantic's input values could be sensitive."""
    parts = [f"{'.'.join(str(p) for p in err['loc']) or 'value'}: {err['msg']}"
             for err in exc.errors()[:3]]
    return "; ".join(parts)


def _checked(adapter: TypeAdapter, value: Any, name: str) -> Any:
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise StoreError(f"invalid {name}: {_errors(exc)}") from None


def _coerce(cls: type[M], obj: Any) -> M:
    """Accept a model instance or a plain dict for ``cls``."""
    if isinstance(obj, cls):
        return obj
    if isinstance(obj, dict):
        try:
            return cls.model_validate(obj)
        except ValidationError as exc:
            raise StoreError(f"invalid {cls.__name__}: {_errors(exc)}") from None
    raise StoreError(f"expected {cls.__name__}, got {type(obj).__name__}")


def _parse_raw(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, str)):
        try:
            raw = canon.parse_json(raw)
        except (canon.CanonError, RecursionError):
            raise StoreError("raw_payload is not valid JSON") from None
    if not isinstance(raw, dict):
        raise StoreError("raw_payload must be a JSON object")
    return raw


def _kinds(kinds: Any) -> set[RecordKind] | None:
    if kinds is None:
        return None
    if isinstance(kinds, str):
        kinds = [kinds]
    try:
        return {RecordKind(kind) for kind in kinds}
    except (TypeError, ValueError):
        raise StoreError("kinds must be record kinds") from None


def _revision(value: Any, what: str) -> int:
    """A revision/version argument. Wrong types are errors; out of range is not found."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreError("revision and version arguments must be integers")
    if not 1 <= value <= MAX_REVISION:
        raise NotFound(f"no {what} revision {value}")
    return value


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _attr(obj: Any, path: str) -> Any:
    for name in path.split("."):
        obj = getattr(obj, name)
    return _value(obj)


def _locked(method):
    """Run a read method under the store lock (it never sees another thread's open write)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


# (result key, dependency type, table, id column, model, flag column, states to invalidate)
_INVALIDATION = (
    ("links", "link", "links", "link_id", Link, "state", ("active",)),
    ("claims", "claim", "claims", "claim_id", Claim, "state", ("active",)),
    ("drafts", "draft", "drafts", "draft_id", Draft, "status", ("pending", "approved")),
    ("approvals", "approval", "approvals", "approval_id", Approval, "status", ("pending",)),
)

# States a derived object never leaves.
_TERMINAL = {
    "links": frozenset({"invalidated"}),
    "claims": frozenset({"invalidated"}),
    "drafts": frozenset({"invalidated", "packaged"}),
    "approvals": frozenset({"invalidated", "consumed"}),
}

# Clear column -> attribute path of the decrypted object. Checked on every read.
_ROW_FIELDS: dict[str, dict[str, str]] = {
    "sources": {"record_id": "record_id", "revision": "revision", "subject_ref": "subject_ref",
                "connector": "connector", "event_id": "event_id"},
    "bank_txns": {"txn_id": "txn_id", "revision": "revision", "subject_ref": "subject_ref"},
    "links": {"link_id": "link_id", "txn_id": "txn_ref.ref_id", "state": "state"},
    "claims": {"claim_id": "claim_id", "request_id": "request_id", "state": "state"},
    "alerts": {"alert_id": "alert_id", "subject_ref": "subject_ref", "state": "state"},
    "policies": {"policy_id": "policy_id", "version": "version", "subject_ref": "subject_ref"},
    "requests": {"issuer": "issuer", "request_id": "request_id", "subject_ref": "subject_ref",
                 "audience": "audience"},
    "drafts": {"draft_id": "draft_id", "request_id": "request_id", "status": "status"},
    "approvals": {"approval_id": "approval_id", "request_id": "request_id", "status": "status"},
    "packages": {"package_id": "package_id", "request_id": "body.request_id",
                 "audience": "body.audience"},
    "deliveries": {"delivery_id": "delivery_id", "package_id": "package_id",
                   "request_id": "request_id", "audience": "audience"},
    "audit": {"audit_id": "audit_id", "target_id": "target_id"},
    "traces": {"trace_id": "trace_id", "run_id": "run_id", "seq": "seq"},
}

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    subject_ref TEXT NOT NULL,
    connector TEXT NOT NULL,
    event_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    payload BLOB NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS sources_one_active ON sources (record_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS sources_by_subject ON sources (subject_ref, active);
CREATE TABLE IF NOT EXISTS source_events (
    connector TEXT NOT NULL,
    issuer_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (connector, issuer_id, event_id)
);
CREATE TABLE IF NOT EXISTS bank_txns (
    txn_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    subject_ref TEXT NOT NULL,
    connector TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    payload BLOB NOT NULL,
    PRIMARY KEY (txn_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS bank_txns_one_active ON bank_txns (txn_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS bank_txns_by_subject ON bank_txns (subject_ref, active);
CREATE TABLE IF NOT EXISTS cursors (
    connector TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (connector, connection_id)
);
CREATE TABLE IF NOT EXISTS links (
    link_id TEXT PRIMARY KEY, txn_id TEXT NOT NULL, state TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, state TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY, subject_ref TEXT NOT NULL, state TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL, version INTEGER NOT NULL, subject_ref TEXT NOT NULL,
    payload BLOB NOT NULL, PRIMARY KEY (policy_id, version)
);
CREATE TABLE IF NOT EXISTS requests (
    issuer TEXT NOT NULL, request_id TEXT NOT NULL, subject_ref TEXT NOT NULL,
    audience TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (issuer, request_id)
);
CREATE INDEX IF NOT EXISTS requests_by_id ON requests (request_id);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, status TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, status TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, request_id TEXT NOT NULL,
    audience TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, request_id TEXT NOT NULL,
    audience TEXT NOT NULL, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    audit_id TEXT PRIMARY KEY, target_id TEXT, payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS deps (
    obj_type TEXT NOT NULL, obj_id TEXT NOT NULL, token TEXT NOT NULL,
    PRIMARY KEY (obj_type, obj_id, token)
);
CREATE INDEX IF NOT EXISTS deps_by_token ON deps (token, obj_type);
COMMIT;
"""


# --------------------------------------------------------------------------- store


class Store:
    """The passport database. Use as a context manager or call ``close()``."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH, *,
                 master_key: bytes | str | None = None, key_path: Path | str | None = None):
        in_memory = str(path) == ":memory:"
        self.path = Path(path)
        if key_path is not None:
            self.key_path: Path | None = Path(key_path)
        else:
            self.key_path = None if in_memory else self.path.parent / KEY_FILE_NAME
        self._lock = threading.RLock()
        self._depth = 0
        self._conn: sqlite3.Connection | None = None
        if not in_memory:
            try:
                if self.key_path is not None and self.key_path.resolve() == self.path.resolve():
                    raise StoreError("key_path must be a separate file from the database")
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(f"cannot create the passport directory {self.path.parent}: "
                                 f"{type(exc).__name__}") from None
        try:
            self._conn = sqlite3.connect(":memory:" if in_memory else str(self.path),
                                         timeout=10.0, check_same_thread=False,
                                         isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            self.close()
            raise StoreError(f"cannot open the passport database: {exc}") from exc
        try:
            has_key_check = bool(self._q("SELECT 1 FROM meta WHERE key = 'key_check'"))
            key = self._resolve_key(master_key, create=not has_key_check)
            self._fernet = Fernet(key)
            self._fingerprint_key = hmac.new(base64.urlsafe_b64decode(key),
                                             _FINGERPRINT_CONTEXT, hashlib.sha256).digest()
            self._check_meta()
        except BaseException:
            self.close()
            raise

    def _resolve_key(self, master_key: bytes | str | None, *, create: bool) -> bytes:
        if master_key is not None:
            return _check_key(master_key, "master_key")
        env = os.environ.get(MASTER_KEY_ENV, "").strip()
        if env:
            return _check_key(env, f"environment variable {MASTER_KEY_ENV}")
        if self.key_path is None:
            return Fernet.generate_key()  # in-memory store with no configured key: ephemeral
        return load_master_key(self.key_path, create=create)

    def _check_meta(self) -> None:
        with self.transaction():
            meta = {row["key"]: row["value"] for row in self._q("SELECT key, value FROM meta")}
            version = meta.get("schema_version")
            check = meta.get("key_check")
            if check is None:
                self._q("INSERT INTO meta (key, value) VALUES ('key_check', ?)",
                        (self._fernet.encrypt(_KEY_CHECK),))
            else:
                try:
                    ok = self._fernet.decrypt(check) == _KEY_CHECK
                except InvalidToken:
                    ok = False
                if not ok:
                    raise StoreError("the master key does not match this database")
            if version is None:
                self._q("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                        (SCHEMA_VERSION,))
            elif version == "2":
                self._migrate_requests_v2()
                self._q("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                        (SCHEMA_VERSION,))
            elif version != SCHEMA_VERSION:
                raise StoreError(f"unsupported passport database schema version {version!r}")

    def _migrate_requests_v2(self) -> None:
        """Schema 2 keyed requests by ``request_id`` alone. Rebuild the table keyed by
        ``(issuer, request_id)``, filling ``issuer`` from each decrypted request. Runs inside
        the opening transaction, so a failure leaves the file untouched."""
        columns = {row["name"] for row in self._q("PRAGMA table_info(requests)")}
        if "issuer" in columns:
            return
        rows = self._q("SELECT * FROM requests ORDER BY rowid")
        requests = [self._row_obj("requests", RequestForContext, row) for row in rows]
        self._q("ALTER TABLE requests RENAME TO requests_v2")
        self._q("CREATE TABLE requests (issuer TEXT NOT NULL, request_id TEXT NOT NULL, "
                "subject_ref TEXT NOT NULL, audience TEXT NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (issuer, request_id))")
        self._q("CREATE INDEX IF NOT EXISTS requests_by_id ON requests (request_id)")
        for row, rfc in zip(rows, requests):
            self._q("INSERT INTO requests (issuer, request_id, subject_ref, audience, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rfc.issuer, rfc.request_id, rfc.subject_ref, rfc.audience, row["payload"]))
        self._q("DROP TABLE requests_v2")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ----------------------------------------------------------------------- plumbing

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """One write transaction (``BEGIN IMMEDIATE``) held under the store lock.

        Re-entrant: a nested block runs as a savepoint, so an exception inside it undoes
        only that block's writes; the outermost block commits or rolls back everything.
        """
        with self._lock:
            depth = self._depth
            self._q("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT sp{depth}")
            self._depth = depth + 1
            try:
                yield self
            except BaseException:
                self._depth = depth
                self._undo(depth)
                raise
            self._depth = depth
            if depth:
                self._q(f"RELEASE sp{depth}")
                return
            try:
                self._q("COMMIT")
            except BaseException:
                self._undo(0)
                raise

    def _undo(self, depth: int) -> None:
        if self._conn is None:
            return
        try:
            if depth == 0:
                self._conn.execute("ROLLBACK")
            else:
                self._conn.execute(f"ROLLBACK TO sp{depth}")
                self._conn.execute(f"RELEASE sp{depth}")
        except sqlite3.Error:
            pass

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            if self._conn is None:
                raise StoreError("the store is closed")
            try:
                return self._conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(f"database error: {exc}") from exc
            except (OverflowError, TypeError, ValueError) as exc:
                raise StoreError(f"invalid query parameter ({type(exc).__name__})") from None

    @staticmethod
    def _canonical(value: Any) -> bytes:
        try:
            return canon.canonical_bytes(value)
        except (canon.CanonError, RecursionError):
            raise StoreError("value cannot be stored: it is not canonical JSON") from None

    def _seal(self, model: M) -> tuple[M, bytes]:
        """Validate the exact JSON that will be stored and encrypt it: (stored model, blob)."""
        data = self._canonical(model)
        try:
            stored = type(model).model_validate_json(data)
        except ValidationError as exc:
            raise StoreError(f"cannot store invalid {type(model).__name__}: {_errors(exc)}") from None
        return stored, self._fernet.encrypt(data)

    def _decrypt(self, blob: bytes) -> bytes:
        try:
            return self._fernet.decrypt(blob)
        except InvalidToken:
            raise StoreError("cannot decrypt stored data: the master key does not match this "
                             "database or the data was altered") from None

    def _load(self, cls: type[M], blob: bytes) -> M:
        data = self._decrypt(blob)
        try:
            return cls.model_validate_json(data)
        except ValidationError as exc:
            raise StoreError(f"stored {cls.__name__} failed validation: {_errors(exc)}") from None

    def _row_obj(self, table: str, cls: type[M], row: sqlite3.Row) -> M:
        """Decrypt a row's payload and check it matches the row's clear columns."""
        obj = self._load(cls, row["payload"])
        columns = row.keys()
        for column, path in _ROW_FIELDS[table].items():
            if column in columns and _attr(obj, path) != row[column]:
                raise StoreError(f"integrity check failed: a {table} row does not match its "
                                 f"encrypted payload ({column}); the database was altered")
        return obj

    def _fingerprint(self, digest: str) -> str:
        return hmac.new(self._fingerprint_key, digest.encode("ascii"), hashlib.sha256).hexdigest()

    def _get(self, table: str, id_col: str, obj_id: str, cls: type[M], what: str) -> M:
        rows = self._q(f"SELECT * FROM {table} WHERE {id_col} = ?", (obj_id,))
        if not rows:
            raise NotFound(f"no {what} {obj_id!r}")
        return self._row_obj(table, cls, rows[0])

    def _select(self, table: str, cls: type[M], filters: dict[str, Any],
                order: str = "rowid") -> list[M]:
        """Rows matching the optional filters (a ``None`` value means no filter)."""
        clauses, params = [], []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(_value(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._q(f"SELECT * FROM {table}{where} ORDER BY {order}", params)
        return [self._row_obj(table, cls, row) for row in rows]

    def _upsert(self, table: str, id_col: str, obj_id: str, columns: dict[str, Any],
                blob: bytes) -> None:
        names = [id_col, *columns, "payload"]
        values = [obj_id, *(_value(v) for v in columns.values()), blob]
        updates = ", ".join(f"{name} = excluded.{name}" for name in names[1:])
        self._q(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
                f"ON CONFLICT ({id_col}) DO UPDATE SET {updates}", values)

    def _append_once(self, table: str, id_col: str, obj: M, obj_id: str,
                     columns: dict[str, Any], blob: bytes, what: str,
                     scope: dict[str, Any] | None = None) -> bool:
        """Insert an append-only row. Identical content again is a no-op (False); different
        content under the same key (``id_col`` plus the ``scope`` columns, which must also be
        in ``columns``) raises ``IdempotencyConflict``."""
        keys = {id_col: obj_id, **(scope or {})}
        where = " AND ".join(f"{column} = ?" for column in keys)
        rows = self._q(f"SELECT * FROM {table} WHERE {where}", [_value(v) for v in keys.values()])
        if rows:
            if canon.digest(self._row_obj(table, type(obj), rows[0])) == canon.digest(obj):
                return False
            raise IdempotencyConflict(f"{what} {obj_id!r} already exists with different content")
        names = [id_col, *columns, "payload"]
        self._q(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [obj_id, *(_value(v) for v in columns.values()), blob])
        return True

    def _set_deps(self, obj_type: str, obj_id: str, tokens: Iterable[str]) -> None:
        self._q("DELETE FROM deps WHERE obj_type = ? AND obj_id = ?", (obj_type, obj_id))
        for token in tokens:
            self._q("INSERT INTO deps (obj_type, obj_id, token) VALUES (?, ?, ?)",
                    (obj_type, obj_id, token))

    def _active_rev(self, table: str, id_col: str, obj_id: str) -> int | None:
        """The active revision of a record or txn, checked to be its highest revision."""
        rows = self._q(f"SELECT MAX(revision) AS top, COALESCE(SUM(active), 0) AS n, "
                       f"MAX(CASE WHEN active = 1 THEN revision END) AS act "
                       f"FROM {table} WHERE {id_col} = ?", (obj_id,))
        top = rows[0]["top"]
        if top is None:
            return None
        if rows[0]["n"] != 1 or rows[0]["act"] != top:
            raise StoreError(f"integrity check failed: the active revision of {obj_id!r} is not "
                             "its latest revision; the database was altered")
        return top

    # ----------------------------------------------------------------------- sources

    def put_source(self, record: EvidenceRecord, *, raw_payload: dict) -> PutResult:
        """Store one merchant evidence revision (append-only, see the module docstring).

        ``record.payload_sha256`` must equal ``canon.digest(raw_payload)``. Replaying the
        same ``(connector, issuer_id, event_id)`` with identical bytes returns the existing
        row as ``duplicate``; different bytes raise ``IdempotencyConflict``. The same
        ``(record_id, revision)`` under another event with identical content is a
        ``duplicate`` while that revision is active and ``stale`` once a later revision has
        superseded it (nothing changes either way); different content is a
        ``RevisionConflict``. A record id in the wrong namespace raises ``NamespaceError``.
        """
        record = _coerce(EvidenceRecord, record)
        _check_namespace(record)
        raw_bytes = self._canonical(_parse_raw(raw_payload))
        if canon.sha256_hex(raw_bytes) != record.payload_sha256:
            raise StoreError(
                f"payload_sha256 of record {record.record_id!r} does not match its raw payload")
        stored, blob = self._seal(record)
        raw_blob = self._fernet.encrypt(raw_bytes)
        fingerprint = self._fingerprint(stored.payload_sha256)
        with self.transaction():
            seen = self._q("SELECT fingerprint, record_id, revision FROM source_events "
                           "WHERE connector = ? AND issuer_id = ? AND event_id = ?",
                           (stored.connector, stored.issuer_id, stored.event_id))
            if seen:
                if not hmac.compare_digest(seen[0]["fingerprint"], fingerprint):
                    raise IdempotencyConflict(
                        f"event {stored.event_id!r} from issuer {stored.issuer_id!r} via "
                        f"connector {stored.connector!r} was already received with different bytes")
                existing, active = self._source_row(seen[0]["record_id"], seen[0]["revision"])
                return PutResult("duplicate", existing, active)
            current = self._active_source(stored.record_id)
            if current is not None:
                _check_identity("record", stored.record_id, current, stored, _SOURCE_IDENTITY)
            same = self._find_source(stored.record_id, stored.revision)
            if same is not None:
                existing, active = same
                if _source_digest(existing) != _source_digest(stored):
                    raise RevisionConflict(
                        f"record {stored.record_id!r} revision {stored.revision} already exists "
                        "with different content")
                self._remember_event(stored, fingerprint)
                return PutResult("duplicate" if active else "stale", existing, active)
            self._remember_event(stored, fingerprint)
            if current is None:
                self._insert_source(stored, blob, raw_blob, active=True)
                return PutResult("inserted", stored, True)
            if stored.revision > current.revision:
                self._q("UPDATE sources SET active = 0 WHERE record_id = ? AND active = 1",
                        (stored.record_id,))
                self._insert_source(stored, blob, raw_blob, active=True)
                token = f"record:{stored.record_id}"
                invalidated = self.invalidate_dependents(
                    token, f"{token} superseded by revision {stored.revision}",
                    now=stored.received_at)
                return PutResult("corrected", stored, True, invalidated)
            self._insert_source(stored, blob, raw_blob, active=False)
            return PutResult("stale", stored, False)

    def _remember_event(self, record: EvidenceRecord, fingerprint: str) -> None:
        self._q("INSERT INTO source_events (connector, issuer_id, event_id, fingerprint, "
                "record_id, revision) VALUES (?, ?, ?, ?, ?, ?)",
                (record.connector, record.issuer_id, record.event_id, fingerprint,
                 record.record_id, record.revision))

    def _insert_source(self, record: EvidenceRecord, blob: bytes, raw_blob: bytes, *,
                       active: bool) -> None:
        self._q("INSERT INTO sources (record_id, revision, subject_ref, connector, event_id, "
                "active, payload, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record.record_id, record.revision, record.subject_ref, record.connector,
                 record.event_id, int(active), blob, raw_blob))

    def _find_source(self, record_id: str, revision: int) -> tuple[EvidenceRecord, bool] | None:
        rows = self._q("SELECT * FROM sources WHERE record_id = ? AND revision = ?",
                       (record_id, revision))
        if not rows:
            return None
        return self._row_obj("sources", EvidenceRecord, rows[0]), bool(rows[0]["active"])

    def _source_row(self, record_id: str, revision: int) -> tuple[EvidenceRecord, bool]:
        found = self._find_source(record_id, revision)
        if found is None:
            raise NotFound(f"no source record {record_id!r} revision {revision}")
        return found

    def _active_source(self, record_id: str) -> EvidenceRecord | None:
        top = self._active_rev("sources", "record_id", record_id)
        return None if top is None else self._source_row(record_id, top)[0]

    @_locked
    def get_source(self, record_id: str, revision: int | None = None) -> EvidenceRecord:
        """The active revision of a record, or the named revision."""
        if revision is None:
            record = self._active_source(record_id)
            if record is None:
                raise NotFound(f"no source record {record_id!r}")
            return record
        return self._source_row(record_id, _revision(revision, f"source record {record_id!r}"))[0]

    @_locked
    def source_history(self, record_id: str) -> list[EvidenceRecord]:
        """Every stored revision of a record, ascending (empty when unknown)."""
        rows = self._q("SELECT * FROM sources WHERE record_id = ? ORDER BY revision",
                       (record_id,))
        return [self._row_obj("sources", EvidenceRecord, row) for row in rows]

    @_locked
    def list_sources(self, subject_ref: str, *, kinds: Iterable[RecordKind | str] | None = None,
                     active_only: bool = True) -> list[EvidenceRecord]:
        """The subject's records ordered by (occurred_at, record_id, revision)."""
        subject_ref = _checked(_IDENT, subject_ref, "subject_ref")
        wanted = _kinds(kinds)
        if active_only:
            ids = [row["record_id"] for row in self._q(
                "SELECT DISTINCT record_id FROM sources WHERE subject_ref = ?", (subject_ref,))]
            records = [self._active_source(record_id) for record_id in ids]
        else:
            rows = self._q("SELECT * FROM sources WHERE subject_ref = ?", (subject_ref,))
            records = [self._row_obj("sources", EvidenceRecord, row) for row in rows]
        records = [r for r in records if r is not None and r.subject_ref == subject_ref]
        if wanted is not None:
            records = [record for record in records if record.kind in wanted]
        return sorted(records, key=lambda r: (r.occurred_at, r.record_id, r.revision))

    @_locked
    def get_raw_payload(self, record_id: str, revision: int) -> dict:
        """The raw received payload that created this revision."""
        revision = _revision(revision, f"source record {record_id!r}")
        rows = self._q("SELECT * FROM sources WHERE record_id = ? AND revision = ?",
                       (record_id, revision))
        if not rows:
            raise NotFound(f"no source record {record_id!r} revision {revision}")
        record = self._row_obj("sources", EvidenceRecord, rows[0])
        raw = self._decrypt(rows[0]["raw"])
        if canon.sha256_hex(raw) != record.payload_sha256:
            raise StoreError("integrity check failed: a raw payload does not match its record; "
                             "the database was altered")
        return canon.parse_json(raw)

    # ----------------------------------------------------------------------- bank feed

    def put_bank_txns(self, txns: Iterable[BankTxn], *, connector: str,
                      cursor: str | None = None,
                      connection_id: str | None = None) -> list[PutResult]:
        """Store a batch of bank txn revisions and the feed cursor in one transaction.

        The idempotency key is ``(connector, txn_id, revision)`` plus the txn's content
        digest (provenance excluded). The cursor is kept per ``(connector, connection_id)``;
        ``connection_id`` defaults to the batch's ``provenance.connection_id`` and a batch
        with a cursor must come from that one connection. On any error nothing is written,
        cursor included.
        """
        connector = _checked(_IDENT, connector, "connector")
        prepared = [self._seal(_coerce(BankTxn, txn)) for txn in txns]
        cursor_blob = None
        if cursor is not None:
            cursor = _checked(_SHORT_TEXT, cursor, "cursor")
            ids = {txn.provenance.connection_id for txn, _ in prepared}
            if connection_id is None:
                if len(ids) != 1:
                    raise StoreError("a cursor needs one connection: pass connection_id or send "
                                     "a non-empty batch from a single connection")
                connection_id = next(iter(ids))
            connection_id = _checked(_IDENT, connection_id, "connection_id")
            if ids - {connection_id}:
                raise StoreError("every txn in a batch with a cursor must come from its connection_id")
            cursor_blob = self._fernet.encrypt(self._canonical(
                {"connector": connector, "connection_id": connection_id, "cursor": cursor}))
        with self.transaction():
            results = [self._put_txn(txn, blob, connector) for txn, blob in prepared]
            if cursor_blob is not None:
                self._q("INSERT INTO cursors (connector, connection_id, payload) VALUES (?, ?, ?) "
                        "ON CONFLICT (connector, connection_id) DO UPDATE SET "
                        "payload = excluded.payload", (connector, connection_id, cursor_blob))
        return results

    def _put_txn(self, txn: BankTxn, blob: bytes, connector: str) -> PutResult:
        rows = self._q("SELECT * FROM bank_txns WHERE txn_id = ? AND revision = ?",
                       (txn.txn_id, txn.revision))
        if rows:
            existing = self._row_obj("bank_txns", BankTxn, rows[0])
            if _txn_digest(existing) == _txn_digest(txn):
                return PutResult("duplicate", existing, bool(rows[0]["active"]))
            if rows[0]["connector"] == connector:
                raise IdempotencyConflict(
                    f"bank txn {txn.txn_id!r} revision {txn.revision} from connector "
                    f"{connector!r} was already received with different content")
            raise RevisionConflict(
                f"bank txn {txn.txn_id!r} revision {txn.revision} already exists with "
                "different content")
        current = self._active_txn(txn.txn_id)
        if current is None:
            self._insert_txn(txn, blob, connector, active=True)
            return PutResult("inserted", txn, True)
        _check_identity("bank txn", txn.txn_id, current, txn, _TXN_IDENTITY)
        if txn.revision > current.revision:
            self._q("UPDATE bank_txns SET active = 0 WHERE txn_id = ? AND active = 1", (txn.txn_id,))
            self._insert_txn(txn, blob, connector, active=True)
            token = f"txn:{txn.txn_id}"
            invalidated = self.invalidate_dependents(
                token, f"{token} superseded by revision {txn.revision}",
                now=txn.provenance.retrieved_at)
            return PutResult("corrected", txn, True, invalidated)
        self._insert_txn(txn, blob, connector, active=False)
        return PutResult("stale", txn, False)

    def _insert_txn(self, txn: BankTxn, blob: bytes, connector: str, *, active: bool) -> None:
        self._q("INSERT INTO bank_txns (txn_id, revision, subject_ref, connector, active, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (txn.txn_id, txn.revision, txn.subject_ref, connector, int(active), blob))

    def _txn_row(self, txn_id: str, revision: int) -> BankTxn:
        rows = self._q("SELECT * FROM bank_txns WHERE txn_id = ? AND revision = ?",
                       (txn_id, revision))
        if not rows:
            raise NotFound(f"no bank txn {txn_id!r} revision {revision}")
        return self._row_obj("bank_txns", BankTxn, rows[0])

    def _active_txn(self, txn_id: str) -> BankTxn | None:
        top = self._active_rev("bank_txns", "txn_id", txn_id)
        return None if top is None else self._txn_row(txn_id, top)

    @_locked
    def get_bank_txn(self, txn_id: str, revision: int | None = None) -> BankTxn:
        """The active revision of a bank txn, or the named revision."""
        if revision is None:
            txn = self._active_txn(txn_id)
            if txn is None:
                raise NotFound(f"no bank txn {txn_id!r}")
            return txn
        return self._txn_row(txn_id, _revision(revision, f"bank txn {txn_id!r}"))

    @_locked
    def txn_history(self, txn_id: str) -> list[BankTxn]:
        """Every stored revision of a bank txn, ascending (empty when unknown)."""
        rows = self._q("SELECT * FROM bank_txns WHERE txn_id = ? ORDER BY revision", (txn_id,))
        return [self._row_obj("bank_txns", BankTxn, row) for row in rows]

    @_locked
    def list_bank_txns(self, subject_ref: str, *, active_only: bool = True) -> list[BankTxn]:
        """The subject's bank txns ordered by (booked_at, txn_id, revision)."""
        subject_ref = _checked(_IDENT, subject_ref, "subject_ref")
        if active_only:
            ids = [row["txn_id"] for row in self._q(
                "SELECT DISTINCT txn_id FROM bank_txns WHERE subject_ref = ?", (subject_ref,))]
            txns = [self._active_txn(txn_id) for txn_id in ids]
        else:
            rows = self._q("SELECT * FROM bank_txns WHERE subject_ref = ?", (subject_ref,))
            txns = [self._row_obj("bank_txns", BankTxn, row) for row in rows]
        txns = [t for t in txns if t is not None and t.subject_ref == subject_ref]
        return sorted(txns, key=lambda t: (t.booked_at, t.txn_id, t.revision))

    @_locked
    def get_cursor(self, connector: str, connection_id: str) -> str | None:
        """The last cursor committed with a bank batch for this connector and connection."""
        rows = self._q("SELECT payload FROM cursors WHERE connector = ? AND connection_id = ?",
                       (connector, connection_id))
        if not rows:
            return None
        data = canon.parse_json(self._decrypt(rows[0]["payload"]))
        if not isinstance(data, dict) or data.get("connector") != connector \
                or data.get("connection_id") != connection_id:
            raise StoreError("integrity check failed: a cursor row does not match its encrypted "
                             "payload; the database was altered")
        return data.get("cursor")

    # ----------------------------------------------------------------------- derived-result rules

    def _superseded(self, refs: Iterable[EvidenceRef]) -> str | None:
        """Why a set of cited revisions is stale (the first one not active), else None."""
        for ref in refs:
            table, column = ("sources", "record_id") if ref.ref_type == "record" else ("bank_txns", "txn_id")
            active = self._active_rev(table, column, ref.ref_id)
            if active is not None and active != ref.revision:
                return f"cites {ref.key()} but revision {active} is now active"
        return None

    def _guard(self, table: str, id_col: str, cls: type[M], obj: M, flag: str) -> M:
        """Apply the save rules for a derived object; call inside a transaction.

        A stored object in a terminal state never changes (an identical re-save is allowed).
        A non-terminal object that cites a revision which is no longer active is returned
        already invalidated, with the reason.
        """
        obj_id = getattr(obj, id_col)
        terminal = _TERMINAL[table]
        rows = self._q(f"SELECT * FROM {table} WHERE {id_col} = ?", (obj_id,))
        if rows:
            existing = self._row_obj(table, cls, rows[0])
            state = _value(getattr(existing, flag))
            if state in terminal:
                if canon.digest(existing) == canon.digest(obj):
                    return existing
                raise StoreError(f"{table[:-1]} {obj_id!r} is {state} and cannot change")
        if _value(getattr(obj, flag)) not in terminal:
            reason = self._superseded(dependency_refs(obj))
            if reason is not None:
                obj = _coerce(cls, {**obj.to_json_dict(), flag: "invalidated",
                                    "invalidated_reason": reason})
        return obj

    # ----------------------------------------------------------------------- links, claims, alerts

    def save_link(self, link: Link) -> None:
        """Insert or replace a link by ``link_id`` with its dependency tokens."""
        link = _coerce(Link, link)
        with self.transaction():
            link, blob = self._seal(self._guard("links", "link_id", Link, link, "state"))
            self._upsert("links", "link_id", link.link_id,
                         {"txn_id": link.txn_ref.ref_id, "state": link.state}, blob)
            self._set_deps("link", link.link_id, dependency_tokens(link))

    @_locked
    def list_links(self, *, txn_id: str | None = None, state: str | None = "active") -> list[Link]:
        return self._select("links", Link, {"txn_id": txn_id, "state": state})

    def save_claim(self, claim: Claim) -> None:
        """Insert or replace a claim by ``claim_id`` with its dependency tokens."""
        claim = _coerce(Claim, claim)
        with self.transaction():
            claim, blob = self._seal(self._guard("claims", "claim_id", Claim, claim, "state"))
            self._upsert("claims", "claim_id", claim.claim_id,
                         {"request_id": claim.request_id, "state": claim.state}, blob)
            self._set_deps("claim", claim.claim_id, dependency_tokens(claim))

    @_locked
    def list_claims(self, *, request_id: str | None = None, audience: str | None = None,
                    state: str | None = "active") -> list[Claim]:
        """Claims in storage order. A request is ``(request_id, audience)``: pass both to get
        one bank's request, since another bank may use the same request_id."""
        claims = self._select("claims", Claim, {"request_id": request_id, "state": state})
        return claims if audience is None else [c for c in claims if c.audience == audience]

    def save_alert(self, alert: Alert) -> None:
        """Insert or replace an alert by ``alert_id``."""
        alert, blob = self._seal(_coerce(Alert, alert))
        with self.transaction():
            self._upsert("alerts", "alert_id", alert.alert_id,
                         {"subject_ref": alert.subject_ref, "state": alert.state}, blob)

    @_locked
    def list_alerts(self, subject_ref: str, *, state: str | None = None) -> list[Alert]:
        subject_ref = _checked(_IDENT, subject_ref, "subject_ref")
        return self._select("alerts", Alert, {"subject_ref": subject_ref, "state": state})

    def set_alert_state(self, alert_id: str, state: str) -> Alert:
        """Set an alert's state (``open``, ``resolved`` or ``invalidated``)."""
        with self.transaction():
            alert = self._get("alerts", "alert_id", alert_id, Alert, "alert")
            updated, blob = self._seal(_coerce(Alert, {**alert.to_json_dict(), "state": state}))
            self._upsert("alerts", "alert_id", alert_id,
                         {"subject_ref": updated.subject_ref, "state": updated.state}, blob)
            return updated

    # ----------------------------------------------------------------------- policies

    def save_policy(self, policy: Policy) -> Literal["inserted", "duplicate"]:
        """Append ``(policy_id, version)``. Same version, different content: RevisionConflict."""
        policy, blob = self._seal(_coerce(Policy, policy))
        with self.transaction():
            rows = self._q("SELECT * FROM policies WHERE policy_id = ? AND version = ?",
                           (policy.policy_id, policy.version))
            if rows:
                if canon.digest(self._row_obj("policies", Policy, rows[0])) == canon.digest(policy):
                    return "duplicate"
                raise RevisionConflict(f"policy {policy.policy_id!r} version {policy.version} "
                                       "already exists with different content")
            if self._q("SELECT 1 FROM policies WHERE policy_id = ? AND subject_ref != ? LIMIT 1",
                       (policy.policy_id, policy.subject_ref)):
                raise RevisionConflict(f"policy {policy.policy_id!r} belongs to another subject")
            self._q("INSERT INTO policies (policy_id, version, subject_ref, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (policy.policy_id, policy.version, policy.subject_ref, blob))
            return "inserted"

    @_locked
    def get_policy(self, policy_id: str, version: int | None = None) -> Policy:
        """The named version of a policy, or its highest version."""
        if version is None:
            rows = self._q("SELECT * FROM policies WHERE policy_id = ? "
                           "ORDER BY version DESC LIMIT 1", (policy_id,))
        else:
            version = _revision(version, f"policy {policy_id!r}")
            rows = self._q("SELECT * FROM policies WHERE policy_id = ? AND version = ?",
                           (policy_id, version))
        if not rows:
            suffix = "" if version is None else f" version {version}"
            raise NotFound(f"no policy {policy_id!r}{suffix}")
        return self._row_obj("policies", Policy, rows[0])

    @_locked
    def latest_policy(self, subject_ref: str) -> Policy | None:
        """Highest version of the subject's most recently saved policy, if any."""
        subject_ref = _checked(_IDENT, subject_ref, "subject_ref")
        rows = self._q("SELECT policy_id FROM policies WHERE subject_ref = ? "
                       "ORDER BY rowid DESC LIMIT 1", (subject_ref,))
        return self.get_policy(rows[0]["policy_id"]) if rows else None

    # ----------------------------------------------------------------------- requests

    def save_request(self, rfc: RequestForContext) -> Literal["inserted", "duplicate"]:
        """Store a request, keyed by ``(issuer, request_id)``.

        The same issuer sending the same ``request_id`` with identical content is a no-op
        (``duplicate``); with different content it raises ``IdempotencyConflict``. Another
        issuer may use the same ``request_id``: that is a new request, never a conflict, so a
        refusal can never reveal that some other bank holds the id.
        """
        rfc, blob = self._seal(_coerce(RequestForContext, rfc))
        with self.transaction():
            rows = self._q("SELECT * FROM requests WHERE issuer = ? AND request_id = ?",
                           (rfc.issuer, rfc.request_id))
            if rows:
                existing = self._row_obj("requests", RequestForContext, rows[0])
                if canon.digest(existing) == canon.digest(rfc):
                    return "duplicate"
                raise IdempotencyConflict(
                    f"request {rfc.request_id!r} already exists with different content")
            self._q("INSERT INTO requests (issuer, request_id, subject_ref, audience, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rfc.issuer, rfc.request_id, rfc.subject_ref, rfc.audience, blob))
        return "inserted"

    @_locked
    def get_request(self, request_id: str, *, issuer: str | None = None,
                    audience: str | None = None, strict: bool = False) -> RequestForContext:
        """The request ``request_id``. With ``issuer`` (or its alias ``audience``: an RFC's issuer
        is the bank it is addressed to), that bank's request under the id.

        Without ``issuer`` a bare id resolves to the request received first under it, so an
        id that only one bank uses (the normal case) is unambiguous, and a later bank reusing
        the id never captures the drafts, approvals and packages that name the earlier one.
        With ``strict`` a bare id that several banks used raises ``AmbiguousRequest`` instead
        (the holder API uses this, so a call never guesses between banks).
        """
        if issuer is None:
            issuer = audience
        if issuer is not None:
            rows = self._q("SELECT * FROM requests WHERE issuer = ? AND request_id = ?",
                           (issuer, request_id))
        else:
            rows = self._q("SELECT * FROM requests WHERE request_id = ? ORDER BY rowid",
                           (request_id,))
        if not rows:
            raise NotFound(f"no request {request_id!r}")
        if len(rows) > 1 and strict:
            raise AmbiguousRequest(f"request {request_id!r} was received from more than one "
                                   "bank; pass audience")
        return self._row_obj("requests", RequestForContext, rows[0])

    @_locked
    def list_requests(self, *, subject_ref: str | None = None, audience: str | None = None,
                      issuer: str | None = None) -> list[RequestForContext]:
        return self._select("requests", RequestForContext,
                            {"subject_ref": subject_ref, "audience": audience, "issuer": issuer})

    # ----------------------------------------------------------------------- drafts, approvals

    def save_draft(self, draft: Draft) -> None:
        """Insert or replace a draft by ``draft_id`` with its dependency tokens."""
        draft = _coerce(Draft, draft)
        with self.transaction():
            self._write_draft(draft)

    def _write_draft(self, draft: Draft) -> Draft:
        draft, blob = self._seal(self._guard("drafts", "draft_id", Draft, draft, "status"))
        self._upsert("drafts", "draft_id", draft.draft_id,
                     {"request_id": draft.request_id, "status": draft.status}, blob)
        self._set_deps("draft", draft.draft_id, dependency_tokens(draft))
        return draft

    @_locked
    def get_draft(self, draft_id: str) -> Draft:
        return self._get("drafts", "draft_id", draft_id, Draft, "draft")

    @_locked
    def list_drafts(self, *, request_id: str | None = None,
                    audience: str | None = None) -> list[Draft]:
        drafts = self._select("drafts", Draft, {"request_id": request_id})
        return drafts if audience is None else [d for d in drafts if d.audience == audience]

    def update_draft_status(self, draft_id: str, status: DraftStatus | str,
                            reason: str | None = None) -> Draft:
        """Set a draft's status; ``reason`` (if given) becomes its ``invalidated_reason``.

        A draft that is invalidated or packaged cannot change status.
        """
        with self.transaction():
            draft = self._get("drafts", "draft_id", draft_id, Draft, "draft")
            changes: dict[str, Any] = {"status": _value(status)}
            if reason is not None:
                changes["invalidated_reason"] = reason
            return self._write_draft(_coerce(Draft, {**draft.to_json_dict(), **changes}))

    def save_approval(self, approval: Approval) -> None:
        """Insert or replace an approval by ``approval_id`` with its dependency tokens."""
        approval = _coerce(Approval, approval)
        with self.transaction():
            self._write_approval(approval)

    def update_approval(self, approval: Approval) -> None:
        """Replace an existing approval (e.g. mark it consumed with its package_id).

        An approval that is invalidated or consumed cannot change.
        """
        approval = _coerce(Approval, approval)
        with self.transaction():
            if not self._q("SELECT 1 FROM approvals WHERE approval_id = ?", (approval.approval_id,)):
                raise NotFound(f"no approval {approval.approval_id!r}")
            self._write_approval(approval)

    def _write_approval(self, approval: Approval) -> Approval:
        approval, blob = self._seal(
            self._guard("approvals", "approval_id", Approval, approval, "status"))
        self._upsert("approvals", "approval_id", approval.approval_id,
                     {"request_id": approval.request_id, "status": approval.status}, blob)
        self._set_deps("approval", approval.approval_id, dependency_tokens(approval))
        return approval

    @_locked
    def get_approval(self, approval_id: str) -> Approval:
        return self._get("approvals", "approval_id", approval_id, Approval, "approval")

    @_locked
    def list_approvals(self, *, request_id: str | None = None, audience: str | None = None,
                       status: str | None = None) -> list[Approval]:
        approvals = self._select("approvals", Approval, {"request_id": request_id,
                                                         "status": status})
        return approvals if audience is None else [a for a in approvals if a.audience == audience]

    # ----------------------------------------------------------------------- packages, deliveries

    def save_package(self, package: ContextPackage, *,
                     approval_id: str) -> Literal["inserted", "duplicate"]:
        """Store a signed package once. The same ``package_id`` again is a duplicate only with
        identical content and approval; otherwise IdempotencyConflict. Never altered later."""
        package, blob = self._seal(_coerce(ContextPackage, package))
        approval_id = _checked(_IDENT, approval_id, "approval_id")
        with self.transaction():
            rows = self._q("SELECT * FROM packages WHERE package_id = ?", (package.package_id,))
            if rows:
                existing = self._row_obj("packages", ContextPackage, rows[0])
                if rows[0]["approval_id"] == approval_id and canon.digest(existing) == canon.digest(package):
                    return "duplicate"
                raise IdempotencyConflict(
                    f"package {package.package_id!r} already exists with different content")
            self._q("INSERT INTO packages (package_id, approval_id, request_id, audience, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (package.package_id, approval_id, package.body.request_id,
                     package.body.audience, blob))
            self._set_deps("package", package.package_id, dependency_tokens(package))
            return "inserted"

    @_locked
    def get_package(self, package_id: str, *, audience: str | None = None) -> ContextPackage:
        """A stored package. With ``audience``, another recipient's package is ``NotFound``."""
        rows = self._q("SELECT * FROM packages WHERE package_id = ?", (package_id,))
        if not rows or (audience is not None and rows[0]["audience"] != audience):
            raise NotFound(f"no package {package_id!r}")
        return self._row_obj("packages", ContextPackage, rows[0])

    @_locked
    def list_packages(self, *, audience: str | None = None,
                      request_id: str | None = None) -> list[ContextPackage]:
        return self._select("packages", ContextPackage,
                            {"audience": audience, "request_id": request_id})

    def save_delivery(self, delivery: Delivery) -> None:
        """Record a delivery. For an existing ``delivery_id`` only ``status`` and
        ``receipt_id`` may change; anything else raises IdempotencyConflict."""
        delivery, blob = self._seal(_coerce(Delivery, delivery))
        with self.transaction():
            rows = self._q("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery.delivery_id,))
            if rows:
                existing = self._row_obj("deliveries", Delivery, rows[0])
                mutable = ("status", "receipt_id")
                before = {k: v for k, v in existing.to_json_dict().items() if k not in mutable}
                after = {k: v for k, v in delivery.to_json_dict().items() if k not in mutable}
                if before != after:
                    raise IdempotencyConflict(
                        f"delivery {delivery.delivery_id!r} already records a different package, "
                        "recipient or time")
            self._upsert("deliveries", "delivery_id", delivery.delivery_id,
                         {"package_id": delivery.package_id, "request_id": delivery.request_id,
                          "audience": delivery.audience}, blob)

    @_locked
    def list_deliveries(self, *, audience: str | None = None,
                        package_id: str | None = None) -> list[Delivery]:
        return self._select("deliveries", Delivery, {"audience": audience, "package_id": package_id})

    # ----------------------------------------------------------------------- audit, traces

    def append_audit(self, event: AuditEvent) -> None:
        """Append an audit event. Identical again is a no-op; a different event reusing the
        ``audit_id`` raises IdempotencyConflict."""
        event, blob = self._seal(_coerce(AuditEvent, event))
        with self.transaction():
            self._append_once("audit", "audit_id", event, event.audit_id,
                              {"target_id": event.target_id}, blob, "audit event")

    @_locked
    def list_audit(self, *, target_id: str | None = None) -> list[AuditEvent]:
        return self._select("audit", AuditEvent, {"target_id": target_id})

    def append_traces(self, events: Iterable[TraceEvent]) -> None:
        """Append trace events in one transaction, with the same rule as ``append_audit``."""
        sealed = [self._seal(_coerce(TraceEvent, event)) for event in events]
        with self.transaction():
            for event, blob in sealed:
                self._append_once("traces", "trace_id", event, event.trace_id,
                                  {"run_id": event.run_id, "seq": event.seq}, blob, "trace event")

    @_locked
    def list_traces(self, run_id: str) -> list[TraceEvent]:
        run_id = _checked(_IDENT, run_id, "run_id")
        return self._select("traces", TraceEvent, {"run_id": run_id}, order="seq, rowid")

    # ----------------------------------------------------------------------- invalidation

    def invalidate_dependents(self, token: str, reason: str, *,
                              now: datetime | None = None) -> dict[str, list[str]]:
        """Invalidate what depends on ``token`` (``record:<id>`` or ``txn:<id>``).

        Active links and claims become ``invalidated``; drafts in ``pending``/``approved``
        and approvals in ``pending`` become ``invalidated``; each gets ``reason``. Packages
        are never altered: each dependent package gets one ``evidence_superseded`` audit
        event. Returns the invalidated ids by kind.
        """
        reason = _checked(_SHORT_TEXT, reason, "reason")
        at = now or utc_now()
        out = _no_invalidations()
        with self.transaction():
            for key, obj_type, table, id_col, cls, flag, states in _INVALIDATION:
                rows = self._q(
                    f"SELECT o.* FROM {table} AS o "
                    f"JOIN deps AS d ON d.obj_type = ? AND d.obj_id = o.{id_col} "
                    f"WHERE d.token = ? AND o.{flag} IN ({', '.join('?' * len(states))}) "
                    "ORDER BY o.rowid", (obj_type, token, *states))
                for row in rows:
                    obj = self._row_obj(table, cls, row)
                    _, blob = self._seal(_coerce(cls, {**obj.to_json_dict(), flag: "invalidated",
                                                       "invalidated_reason": reason}))
                    self._q(f"UPDATE {table} SET {flag} = 'invalidated', payload = ? "
                            f"WHERE {id_col} = ?", (blob, row[id_col]))
                    out[key].append(row[id_col])
            packages = self._q("SELECT p.package_id FROM packages AS p JOIN deps AS d "
                               "ON d.obj_type = 'package' AND d.obj_id = p.package_id "
                               "WHERE d.token = ? ORDER BY p.rowid", (token,))
            for row in packages:
                self._note_superseded(row["package_id"], token, reason, at)
        return out

    def _note_superseded(self, package_id: str, token: str, reason: str, at: datetime) -> None:
        event = AuditEvent(audit_id=canon.make_id("aud", "evidence_superseded", package_id, token, reason),
                           at=at, actor="store", action="evidence_superseded",
                           target_id=package_id, outcome="ok", detail=reason)
        if self._q("SELECT 1 FROM audit WHERE audit_id = ?", (event.audit_id,)):
            return  # this supersession is already recorded
        event, blob = self._seal(event)
        self._q("INSERT INTO audit (audit_id, target_id, payload) VALUES (?, ?, ?)",
                (event.audit_id, event.target_id, blob))
