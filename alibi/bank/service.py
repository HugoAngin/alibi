"""The mock bank: two recipient identities and the holder's feed connection, as a FastAPI app.

``create_bank_app`` builds the service described in ``alibi/connectors/SERVICES.md`` ("Mock
bank"). Each bank identity (``bank_a`` with analyst ``analyst_a1``, ``bank_b`` with
``analyst_b1``) authenticates with its own bearer token, issues case Requests for Context
signed with its own key, receives Context Packages through ``alibi.api.verify_package``, and
records analyst dispositions whose ``request_more`` creates a signed follow-up request. The
``feed`` token serves the holder's read-only bank feed (``alibi/connectors/bank_feed.py``).

The recipient and analyst come from the token only; posted ``recipient``, ``analyst`` or
``analyst_name`` fields are ignored. Every refusal is an ``alibi.connectors.http.Refusal`` whose
reason names ids, fields and codes only. Tokens and keys are never logged or stored in state.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from alibi import api
from alibi.connectors import keys
from alibi.connectors.case_source import (
    FEED_VERSION,
    CaseClock,
    MappingError,
    load_case_without_gold,
    map_rfc,
    parse_time,
)
from alibi.connectors.http import Refusal, install_refusals
from alibi.connectors.plug import RfcSink
from alibi.core import canon
from alibi.core.canon import CanonError
from alibi.core.schemas import Ident, Period, Receipt, format_utc
from alibi.core.signing import DemoKeystore, TrustRegistry
from alibi.bank.state import RECIPIENTS, BankState, InboxEntry, RecipientState, SentRequest

__all__ = [
    "ANALYSTS", "DECISIONS", "DEFAULT_TOKENS_PATH", "FEED", "FOLLOW_UP_TTL", "SERVICE",
    "TERMINAL_DECISIONS", "TOKEN_ENV", "BankService", "create_bank_app", "load_tokens",
]

SERVICE = "mock-bank"
FEED = "feed"
ANALYSTS = {"bank_a": "analyst_a1", "bank_b": "analyst_b1"}
TOKEN_ENV = {"bank_a": "ALIBI_BANK_A_TOKEN", "bank_b": "ALIBI_BANK_B_TOKEN", FEED: "ALIBI_FEED_TOKEN"}
DEFAULT_TOKENS_PATH = Path("data/alibi/bank_tokens.json")
DECISIONS = ("clear", "escalate", "request_more")
TERMINAL_DECISIONS = ("clear", "escalate")
FOLLOW_UP_TTL = timedelta(days=7)
FEED_UNAVAILABLE = "the bank feed module is not available"
# Bounds on analyst-typed disposition fields, mirroring the core's ShortText / Ident / Question.text.
REASON_MAX = 300
EVIDENCE_TEXT_MAX = 1000
LIST_MAX = 50

_IDENT = TypeAdapter(Ident)
CaseLoader = Callable[[int], dict[str, Any]]


# --------------------------------------------------------------------------- tokens


def _read_tokens_file(path: Path) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"the tokens file {path.as_posix()} must map identities to strings")
    return data


def _write_tokens_file(path: Path, tokens: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(tokens, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)


def load_tokens(path: Path | str | None = None) -> dict[str, str]:
    """The bearer tokens for ``bank_a``, ``bank_b`` and ``feed``.

    Environment variables (``ALIBI_BANK_A_TOKEN``, ``ALIBI_BANK_B_TOKEN``, ``ALIBI_FEED_TOKEN``)
    win. Identities without one come from the JSON file at ``path`` (default
    ``data/alibi/bank_tokens.json``), which is created with ``secrets.token_urlsafe(32)`` per
    missing identity and mode 0600. Values are never logged.
    """
    tokens: dict[str, str] = {}
    from_env = {name: os.environ.get(var) for name, var in TOKEN_ENV.items()}
    missing = [name for name, value in from_env.items() if not value]
    if missing:
        file_path = DEFAULT_TOKENS_PATH if path is None else Path(path)
        stored = _read_tokens_file(file_path) if file_path.exists() else {}
        created = False
        for name in TOKEN_ENV:
            if not stored.get(name):
                stored[name] = secrets.token_urlsafe(32)
                created = True
        if created:
            _write_tokens_file(file_path, stored)
        tokens.update({name: stored[name] for name in TOKEN_ENV})
    for name, value in from_env.items():
        if value:
            tokens[name] = value
    return tokens


# --------------------------------------------------------------------------- helpers


def _is_ident(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _IDENT.validate_python(value)
    except ValidationError:
        return False
    return True


def _clip(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _scope_of(request_body: dict[str, Any]) -> dict[str, Any]:
    """``{"txn_ids": [...]}`` or ``{"period": {...}}`` from a core request body."""
    if request_body.get("txn_ids"):
        return {"txn_ids": list(request_body["txn_ids"])}
    return {"period": copy.deepcopy(request_body.get("period"))}


def _attachment_metadata(attachment: dict[str, Any]) -> dict[str, Any]:
    return {name: copy.deepcopy(attachment[name])
            for name in ("attachment_id", "ref", "form", "source_trust", "issuer_id",
                         "generalised_fields") if name in attachment}


def _package_view(package: dict[str, Any]) -> dict[str, Any]:
    """The package without attachment contents or issuer signatures."""
    body = package.get("body") or {}
    view_body = {name: copy.deepcopy(body[name])
                 for name in ("request_id", "parent_request_id", "audience", "case_ref", "scope",
                              "questions", "claims", "expires_at") if name in body}
    view_body["attachments"] = [_attachment_metadata(a) for a in body.get("attachments") or []
                                if isinstance(a, dict)]
    return {"package_id": package.get("package_id"), "issued_at": package.get("issued_at"),
            "body_sha256": package.get("body_sha256"), "body": view_body}


def _rejection(verification: dict[str, Any]) -> dict[str, Any]:
    return {"authentication": copy.deepcopy(verification.get("authentication")),
            "errors": list(verification.get("errors") or [])}


async def _json_object(request: Request, code: str, *, allow_empty: bool) -> dict[str, Any]:
    """The request body as a strict JSON object, or a refusal with ``code``."""
    raw = await request.body()
    if not raw.strip():
        if allow_empty:
            return {}
        raise Refusal(code, "the request body must be a JSON object")
    try:
        parsed = canon.parse_json(raw)
    except CanonError:
        raise Refusal(code, "the request body is not valid JSON") from None
    except RecursionError:
        raise Refusal(code, "the request body is too deeply nested") from None
    if not isinstance(parsed, dict):
        raise Refusal(code, "the request body must be a JSON object")
    return parsed


# --------------------------------------------------------------------------- the service


class BankService:
    """The mock bank's behaviour, independent of the HTTP layer.

    Routes in ``create_bank_app`` are thin wrappers over these methods. Every method takes the
    authenticated ``recipient`` as an argument; the HTTP layer resolves it from the token.
    """

    def __init__(self, *, keystore: DemoKeystore, registry: TrustRegistry,
                 tokens: dict[str, str], state_path: Path | str | None = None,
                 rfc_sink: RfcSink | None = None, case_loader: CaseLoader = load_case_without_gold):
        self.keystore = keystore
        self.registry = registry
        self.rfc_sink = rfc_sink
        self.case_loader = case_loader
        self.state_path = Path(state_path) if state_path is not None else None
        self._tokens = dict(tokens)
        self.case: dict[str, Any] | None = None
        self.clock: CaseClock | None = None
        self._feed_server: Any = None
        self.state = BankState()
        if self.state_path is not None and self.state_path.exists():
            self.state = BankState.load(self.state_path)
            if self.state.seeded:
                self.case = self.case_loader(self.state.case_number)
                self.clock = CaseClock(self.case, as_of=self.state.as_of)

    # ------------------------------------------------------------------ identity

    def identity(self, token: str | None) -> str | None:
        """The identity a bearer token belongs to (``bank_a``, ``bank_b``, ``feed``) or None."""
        if not token:
            return None
        for name, expected in self._tokens.items():
            if secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
                return name
        return None

    @staticmethod
    def analyst(recipient: str) -> str:
        return ANALYSTS[recipient]

    # ------------------------------------------------------------------ clock and state

    def _require_clock(self) -> CaseClock:
        if self.clock is None or not self.state.seeded:
            raise Refusal("not_found", "no case is seeded; call POST /admin/seed first")
        return self.clock

    @property
    def as_of_text(self) -> str:
        return self._require_clock().as_of_text

    def save(self) -> None:
        if self.state_path is not None:
            self.state.save(self.state_path)

    def _recipient(self, recipient: str) -> RecipientState:
        return self.state.recipient(recipient)

    @contextmanager
    def audited(self, actor: str, recipient: str, action: str, *, package_id: str | None = None,
                request_id: str | None = None):
        """Append one audit entry for the action: ``ok`` with the detail the caller sets on
        the yielded dict, or ``refused`` with the code and reason of the Refusal raised."""
        entry: dict[str, Any] = {"at": self.as_of_text, "actor": actor, "action": action,
                                 "outcome": "ok", "detail": ""}
        if package_id is not None:
            entry["package_id"] = package_id
        if request_id is not None:
            entry["request_id"] = request_id
        try:
            yield entry
        except Refusal as exc:
            entry["outcome"] = "refused"
            entry["detail"] = _clip(f"{exc.code}: {exc.reason}")
            self._recipient(recipient).audit.append(entry)
            self.save()
            raise
        self._recipient(recipient).audit.append(entry)
        self.save()

    def seed(self, case_number: int = 1, as_of: str | None = None) -> dict[str, Any]:
        """Load a case without gold, reset every recipient and set the clock."""
        try:
            case = self.case_loader(case_number)
        except KeyError:
            raise Refusal("unknown_case", f"case {case_number} is not available") from None
        try:
            clock = CaseClock(case, as_of=as_of)
        except MappingError as exc:
            raise Refusal("invalid_time", _clip(exc)) from None
        rows = case.get("bank_deliveries", [])
        accounts = [d["row"].get("account_ref") for d in rows if isinstance(d.get("row"), dict)]
        account_ref = next((a for a in accounts if a), None)
        self.case, self.clock, self._feed_server = case, clock, None
        self.state.reset(case_number=case_number, subject_ref=clock.subject_ref,
                         account_ref=account_ref, bank_rows=rows, as_of=clock.as_of_text)
        self.save()
        return {"case_number": case_number, "subject_ref": clock.subject_ref,
                "account_ref": account_ref, "as_of": clock.as_of_text,
                "recipients": list(RECIPIENTS)}

    def advance(self, as_of: str | None) -> dict[str, Any]:
        """Move the clock forward only."""
        clock = self._require_clock()
        if as_of is None:
            raise Refusal("invalid_time", "as_of is required")
        try:
            moment = parse_time(as_of, "as_of")
        except MappingError as exc:
            raise Refusal("invalid_time", _clip(exc)) from None
        if moment < clock.as_of:
            raise Refusal("clock_backwards", f"the clock is at {clock.as_of_text} and cannot move "
                                             f"back to {format_utc(moment)}")
        clock.advance(moment)
        self.state.as_of = clock.as_of_text
        self.save()
        return {"as_of": clock.as_of_text}

    def identity_view(self) -> dict[str, Any]:
        return {"service": SERVICE, "case_number": self.state.case_number,
                "as_of": self.state.as_of, "recipients": list(RECIPIENTS)}

    # ------------------------------------------------------------------ requests

    def _case_rfcs(self, recipient: str) -> list[dict[str, Any]]:
        """The case RFCs for this recipient that are visible now (``issued_at <= as_of``)."""
        clock = self._require_clock()
        return [r for r in clock.visible_rfcs() if r.get("audience") == recipient]

    def _check_chosen_id(self, recipient: str, request_id: Any, code: str) -> str:
        """A request id chosen by the recipient (custom or follow-up): an identifier that is not
        a case RFC id (any audience) and not a request another recipient has sent."""
        if not _is_ident(request_id):
            raise Refusal(code, "request_id must be an identifier")
        case_ids = {r.get("request_id") for r in (self.case or {}).get("rfcs", [])}
        if request_id in case_ids:
            raise Refusal(code, f"request_id {request_id} is a case request id; issue it with "
                                "{request_id}")
        for name, other in self.state.recipients.items():
            if name != recipient and request_id in other.requests:
                raise Refusal(code, f"request_id {request_id} is not available")
        return request_id

    def _issue(self, recipient: str, case_rfc: dict[str, Any], *,
               source: str) -> tuple[SentRequest, dict[str, Any] | None, bool]:
        """Map, check the clock (not issued in the future, not expired), sign and store one
        request. Returns (sent, forwarded, created)."""
        clock = self._require_clock()
        sent = self._recipient(recipient)
        try:
            mapped = map_rfc(case_rfc)
        except MappingError as exc:
            raise Refusal("invalid_request", _clip(exc)) from None
        body = mapped.body
        request_id = body["request_id"]
        if parse_time(body["issued_at"], "issued_at") > clock.as_of:
            raise Refusal("invalid_request", f"request {request_id} has issued_at {body['issued_at']}, "
                                             f"after the clock {clock.as_of_text}")
        if clock.as_of >= parse_time(body["expires_at"], "expires_at"):
            raise Refusal("request_expired", f"request {request_id} expired at {body['expires_at']}; "
                                             f"the clock is at {clock.as_of_text}")
        signed = keys.sign_request(body, self.keystore, kid=keys.BANK_KIDS[recipient])
        existing = sent.requests.get(request_id)
        if existing is not None:
            if existing.rfc["request"] != signed["request"]:
                raise Refusal("invalid_request", f"request {request_id} was already issued with "
                                                 "different content")
            return existing, None, False
        account_refs = [self.state.account_ref] if self.state.account_ref else []
        record = SentRequest(
            request_id=request_id, rfc=signed, account_refs=account_refs,
            parent_request_id=body.get("parent_request_id"), issued_at=body["issued_at"],
            expires_at=body["expires_at"], scope=_scope_of(body),
            question_ids=[q["question_id"] for q in body["questions"]], source=source)
        forwarded = None
        if self.rfc_sink is not None:
            try:
                forwarded = self.rfc_sink(copy.deepcopy(signed), recipient, list(account_refs),
                                          clock.as_of_text)
            except Exception as exc:  # noqa: BLE001 - a sink failure is an outcome, not a 500
                forwarded = {"status": "error",
                             "reason": f"the rfc_sink raised {type(exc).__name__}"}
        sent.requests[request_id] = record
        return record, forwarded, True

    def issue_request(self, recipient: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """``POST /rfc``: issue a case RFC (by id or the next unissued one) or a custom one.
        Returns the response dict and the status (201 new, 200 re-issue)."""
        self._require_clock()
        sent = self._recipient(recipient)
        hint = body.get("request_id") if isinstance(body.get("request_id"), str) else None
        with self.audited(recipient, recipient, "issue_request", request_id=hint) as entry:
            if "request" in body:
                custom = body["request"]
                if not isinstance(custom, dict):
                    raise Refusal("invalid_request", "request must be a JSON object")
                case_rfc = {**custom, "issuer": recipient, "audience": recipient}
                if case_rfc.get("subject_ref") != self.state.subject_ref:
                    raise Refusal("invalid_request", "request.subject_ref must be the case subject")
                self._check_chosen_id(recipient, case_rfc.get("request_id"), "invalid_request")
                parent_id = case_rfc.get("parent_request_id")
                if parent_id is not None:
                    parent = sent.requests.get(parent_id) if isinstance(parent_id, str) else None
                    if parent is None:
                        named = f" {parent_id}" if _is_ident(parent_id) else ""
                        raise Refusal("invalid_request", f"parent_request_id{named} was not "
                                                         f"issued by {recipient}")
                    # A custom follow-up inherits the parent's scope, narrowed at most.
                    for name in ("txn_ids", "period"):
                        if case_rfc.get(name) in (None, []):
                            case_rfc.pop(name, None)
                    try:
                        scope = self._follow_up_scope(parent, case_rfc)
                    except Refusal as exc:
                        if exc.code == "invalid_disposition":
                            raise Refusal("invalid_request", exc.reason) from None
                        raise
                    case_rfc.pop("txn_ids", None)
                    case_rfc.pop("period", None)
                    case_rfc.update(scope)
                source = "custom"
            elif hint is not None:
                matches = [r for r in self._case_rfcs(recipient) if r.get("request_id") == hint]
                if not matches:
                    raise Refusal("unknown_request", f"no request {hint} for this recipient",
                                  status=404)
                case_rfc, source = matches[0], "case"
            elif body:
                raise Refusal("invalid_request", "body must be {}, {request_id} or {request}")
            else:
                unissued = [r for r in self._case_rfcs(recipient)
                            if r.get("request_id") not in sent.requests]
                if not unissued:
                    raise Refusal("unknown_request", "no unissued request for this recipient",
                                  status=404)
                case_rfc, source = unissued[0], "case"
            record, forwarded, created = self._issue(recipient, case_rfc, source=source)
            entry["request_id"] = record.request_id
            entry["detail"] = (f"issued {record.source} request {record.request_id}"
                               if created else f"re-issued request {record.request_id}")
            if forwarded is not None:
                entry["detail"] += f"; forwarded {forwarded.get('status')}"
        return ({"recipient": recipient, "request_id": record.request_id,
                 "rfc": copy.deepcopy(record.rfc), "account_refs": list(record.account_refs or []),
                 "forwarded": forwarded}, 201 if created else 200)

    def list_requests(self, recipient: str) -> dict[str, Any]:
        self._require_clock()
        return {"requests": [r.summary() for r in self._recipient(recipient).requests.values()]}

    def get_request(self, recipient: str, request_id: str) -> dict[str, Any]:
        self._require_clock()
        record = self._recipient(recipient).requests.get(request_id)
        if record is None:
            raise Refusal("not_found", f"no request {request_id} sent by this recipient")
        return {"request_id": request_id, "rfc": copy.deepcopy(record.rfc),
                "account_refs": list(record.account_refs or [])}

    # ------------------------------------------------------------------ delivery

    def _verified_response(self, entry: InboxEntry) -> dict[str, Any]:
        verification = entry.verification
        return {"status": "verified", "receipt": copy.deepcopy(entry.receipt),
                "authentication": copy.deepcopy(verification.get("authentication")),
                "sources": copy.deepcopy(verification.get("sources") or []),
                "claims": copy.deepcopy(verification.get("claims") or [])}

    def _rejected_refusal(self, entry: InboxEntry) -> Refusal:
        return Refusal(entry.refusal_code or "package_rejected",
                       entry.refusal_reason or f"package {entry.package_id} was rejected",
                       receipt=copy.deepcopy(entry.receipt), rejection=_rejection(entry.verification))

    def deliver(self, recipient: str, raw: dict[str, Any]) -> dict[str, Any]:
        """``POST /deliver``: verify a Context Package and file it in this recipient's inbox."""
        clock = self._require_clock()
        inbox_owner = self._recipient(recipient)
        package_id = raw.get("package_id")
        if not _is_ident(package_id):
            raise Refusal("invalid_package", "the package needs a package_id identifier")
        with self.audited(recipient, recipient, "deliver", package_id=package_id) as entry:
            try:
                digest = canon.digest(raw)
            except (CanonError, RecursionError):
                raise Refusal("invalid_package", f"package {package_id} cannot be canonicalised "
                                                 "(RFC 8785)") from None
            existing = inbox_owner.inbox.get(package_id)
            if existing is not None:
                if existing.package_sha256 != digest:
                    raise Refusal("package_id_reused",
                                  f"package {package_id} is already in this inbox with a different "
                                  "digest", expected_digest=existing.package_sha256)
                entry["detail"] = f"replay of package {package_id}: {existing.status}"
                if existing.verified:
                    return self._verified_response(existing)
                raise self._rejected_refusal(existing)
            body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
            # Only identifier-shaped ids are quoted or stored; anything else is named by field.
            audience, request_id = body.get("audience"), body.get("request_id")
            audience_ok, request_ok = _is_ident(audience), _is_ident(request_id)
            sent = inbox_owner.requests.get(request_id) if request_ok else None
            code: str | None = None
            reasons: list[str] = []
            if audience is not None and not audience_ok:
                code = "audience_mismatch"
                reasons.append(f"body.audience is not an identifier; this inbox is {recipient}")
            elif audience_ok and audience != recipient:
                code = "audience_mismatch"
                reasons.append(f"the package is addressed to {audience}, not {recipient}")
            elif request_id is not None and not request_ok:
                code = "unknown_request"
                reasons.append("body.request_id is not an identifier")
            elif request_ok and sent is None:
                code = "unknown_request"
                reasons.append(f"request {request_id} was not issued by {recipient}")
            expires_at: str | None = None
            if isinstance(body.get("expires_at"), str):
                try:
                    expires_at = format_utc(parse_time(body["expires_at"], "expires_at"))
                except MappingError:
                    expires_at = None
            rfc_body = sent.rfc["request"] if sent is not None else {}
            result = api.verify_package(
                raw, expected_audience=recipient, registry=self.registry, now=clock.as_of,
                expected_subject=rfc_body.get("subject_ref", self.state.subject_ref),
                expected_request_id=rfc_body.get("request_id"))
            verification = result.to_json_dict()
            accepted = result.accepted and code is None
            if not accepted and code is None:
                code = "package_rejected"
            for reason in verification.get("errors") or []:
                if reason not in reasons:
                    reasons.append(reason)
            if not accepted and not reasons:
                reasons.append(f"package {package_id} failed verification")
            receipt = Receipt(receipt_id=canon.make_id("rcp", recipient, package_id, digest),
                              package_id=package_id, package_sha256=digest, recipient=recipient,
                              received_at=clock.as_of, accepted=accepted,
                              reasons=[_clip(r) for r in reasons[:20]]).to_json_dict()
            # A rejected entry keeps the authentication result and the errors only: no package
            # bytes, and none of the sources or claims the verifier derived from the body.
            stored = InboxEntry(
                package_id=package_id, request_id=request_id if request_ok else None,
                status="verified" if accepted else "rejected", received_at=clock.as_of_text,
                package_sha256=digest, receipt=receipt,
                verification=verification if accepted else _rejection(verification),
                reasons=[] if accepted else list(receipt["reasons"]),
                package=copy.deepcopy(raw) if accepted else None,
                expires_at=expires_at,
                refusal_code=code,
                refusal_reason=None if accepted else _clip(f"package {package_id}: {reasons[0]}"))
            inbox_owner.inbox[package_id] = stored
            if accepted:
                if sent is not None and package_id not in sent.answered_by:
                    sent.answered_by.append(package_id)
                entry["request_id"] = request_id
                entry["detail"] = (f"verified package {package_id} for request {request_id}; "
                                   f"receipt {receipt['receipt_id']}")
                return self._verified_response(stored)
            raise self._rejected_refusal(stored)

    # ------------------------------------------------------------------ inbox views

    def _entry(self, recipient: str, package_id: str) -> InboxEntry:
        entry = self._recipient(recipient).inbox.get(package_id)
        if entry is None:
            raise Refusal("not_found", f"no package {package_id} in this inbox")
        return entry

    def _verified_entry(self, recipient: str, package_id: str) -> InboxEntry:
        entry = self._entry(recipient, package_id)
        if not entry.verified:
            raise Refusal("package_rejected", f"package {package_id} was rejected and holds no "
                                              "contents", status=409)
        return entry

    def inbox(self, recipient: str) -> dict[str, Any]:
        self._require_clock()
        owner = self._recipient(recipient)
        packages = []
        for entry in owner.inbox.values():
            dispositions = owner.dispositions_of(entry.package_id)
            item: dict[str, Any] = {
                "package_id": entry.package_id, "request_id": entry.request_id,
                "received_at": entry.received_at, "status": entry.status,
                "package_sha256": entry.package_sha256, "receipt_id": entry.receipt["receipt_id"],
                "disposition": dispositions[-1]["decision"] if dispositions else None,
            }
            if entry.expires_at is not None:
                item["expires_at"] = entry.expires_at
            if not entry.verified:
                item["reasons"] = list(entry.reasons)
            packages.append(item)
        return {"recipient": recipient, "as_of": self.as_of_text, "packages": packages,
                "requests_sent": [r.summary() for r in owner.requests.values()]}

    def package(self, recipient: str, package_id: str) -> dict[str, Any]:
        self._require_clock()
        entry = self._entry(recipient, package_id)
        if not entry.verified:
            return {"status": "rejected", "receipt": copy.deepcopy(entry.receipt),
                    "rejection": {"reasons": list(entry.reasons)}}
        view = self._verified_response(entry)
        view["package"] = _package_view(entry.package or {})
        view["dispositions"] = self._recipient(recipient).dispositions_of(package_id)
        view["follow_ups"] = list(entry.follow_ups)
        return view

    def attachments(self, recipient: str, package_id: str) -> dict[str, Any]:
        self._require_clock()
        entry = self._verified_entry(recipient, package_id)
        body = (entry.package or {}).get("body") or {}
        return {"package_id": package_id, "attachments": copy.deepcopy(body.get("attachments") or [])}

    def raw_package(self, recipient: str, package_id: str) -> dict[str, Any]:
        self._require_clock()
        return copy.deepcopy(self._verified_entry(recipient, package_id).package or {})

    def dispositions(self, recipient: str, package_id: str) -> dict[str, Any]:
        self._require_clock()
        self._entry(recipient, package_id)
        return {"dispositions": self._recipient(recipient).dispositions_of(package_id)}

    def audit(self, recipient: str) -> dict[str, Any]:
        self._require_clock()
        return {"entries": copy.deepcopy(self._recipient(recipient).audit)}

    # ------------------------------------------------------------------ dispositions

    @staticmethod
    def _string_list(body: dict[str, Any], name: str, *, max_length: int,
                     ident: bool = False) -> list[str]:
        """A bounded list of non-empty strings (identifiers when ``ident``) from the body."""
        value = body.get(name)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise Refusal("invalid_disposition", f"{name} must be a list of non-empty strings")
        if len(value) > LIST_MAX:
            raise Refusal("invalid_disposition", f"{name} may hold at most {LIST_MAX} entries")
        if ident and not all(_is_ident(v) for v in value):
            raise Refusal("invalid_disposition", f"{name} must be a list of identifiers")
        if any(len(v) > max_length for v in value):
            raise Refusal("invalid_disposition", f"{name} entries may be at most {max_length} "
                                                 "characters")
        return list(value)

    def _follow_up_scope(self, parent: SentRequest, body: dict[str, Any]) -> dict[str, Any]:
        """The parent's scope, narrowed by the body when it asks; wider or of another kind is
        ``scope_wider_than_parent``."""
        parent_scope = parent.scope
        wants_txns, wants_period = body.get("txn_ids") is not None, body.get("period") is not None
        if "txn_ids" in parent_scope:
            if wants_period:
                raise Refusal("scope_wider_than_parent",
                              f"request {parent.request_id} is transaction-scoped; a follow-up "
                              "cannot ask for a period")
            if not wants_txns:
                return {"txn_ids": list(parent_scope["txn_ids"])}
            txn_ids = body["txn_ids"]
            if not isinstance(txn_ids, list) or not txn_ids \
                    or not all(isinstance(t, str) for t in txn_ids):
                raise Refusal("invalid_disposition", "txn_ids must be a non-empty list of ids")
            outside = [t for t in txn_ids if t not in parent_scope["txn_ids"]]
            if outside:
                raise Refusal("scope_wider_than_parent",
                              f"txn_ids must be a subset of request {parent.request_id}'s "
                              f"({len(outside)} outside)")
            return {"txn_ids": list(dict.fromkeys(txn_ids))}
        if wants_txns:
            raise Refusal("scope_wider_than_parent",
                          f"request {parent.request_id} is period-scoped; a follow-up cannot "
                          "name txn_ids")
        if not wants_period:
            return {"period": copy.deepcopy(parent_scope["period"])}
        try:
            parent_period = Period.model_validate(parent_scope["period"])
            period = Period.model_validate(body["period"])
        except ValidationError:
            raise Refusal("invalid_disposition", "period must be {from, to} UTC timestamps with "
                                                 "from before to") from None
        if period.from_ < parent_period.from_ or period.to > parent_period.to:
            raise Refusal("scope_wider_than_parent",
                          f"period must sit inside request {parent.request_id}'s period")
        return {"period": period.to_json_dict()}

    def _follow_up_questions(self, body: dict[str, Any]) -> list[dict[str, str]]:
        questions = body.get("questions")
        if questions is not None:
            if not isinstance(questions, list) or not all(
                    isinstance(q, dict) and isinstance(q.get("question_id"), str)
                    and isinstance(q.get("text"), str) and q["text"].strip() for q in questions):
                raise Refusal("invalid_disposition",
                              "questions must be a list of {question_id, text}")
            built = [{"question_id": q["question_id"], "text": q["text"]} for q in questions]
        else:
            built = [{"question_id": f"q.followup_{i}", "text": text}
                     for i, text in enumerate(self._string_list(
                         body, "requested_evidence", max_length=EVIDENCE_TEXT_MAX), 1)]
        if not built:
            raise Refusal("invalid_disposition", "a request_more needs questions or "
                                                 "requested_evidence")
        return built

    def _follow_up_id(self, recipient: str, owner: RecipientState, parent_id: str,
                      body: dict[str, Any]) -> str:
        wanted = body.get("request_id")
        if wanted is not None:
            self._check_chosen_id(recipient, wanted, "invalid_disposition")
            if wanted in owner.requests:
                raise Refusal("invalid_disposition", f"request {wanted} was already issued")
            return wanted
        n = owner.follow_up_count(parent_id) + 1
        while f"{parent_id}-f{n}" in owner.requests:
            n += 1
        return f"{parent_id}-f{n}"

    def dispose(self, recipient: str, package_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /packages/{id}/disposition``: record the analyst's decision; ``request_more``
        also issues a signed follow-up request narrowed from the parent's scope."""
        clock = self._require_clock()
        analyst = self.analyst(recipient)
        owner = self._recipient(recipient)
        with self.audited(analyst, recipient, "disposition", package_id=package_id) as audit:
            entry = self._entry(recipient, package_id)
            decision, reason = body.get("decision"), body.get("reason")
            if decision not in DECISIONS:
                raise Refusal("invalid_disposition", "decision must be one of clear, escalate, "
                                                     "request_more")
            if not isinstance(reason, str) or not reason.strip():
                raise Refusal("invalid_disposition", "reason must be a non-empty string")
            if len(reason) > REASON_MAX:
                raise Refusal("invalid_disposition", f"reason may be at most {REASON_MAX} characters")
            if not entry.verified:
                raise Refusal("package_rejected", f"package {package_id} was rejected; it cannot "
                                                  "be dispositioned", status=409)
            previous = owner.dispositions_of(package_id)
            if any(d["decision"] in TERMINAL_DECISIONS for d in previous):
                raise Refusal("disposition_final", f"package {package_id} already has a final "
                                                   f"disposition {previous[-1]['disposition_id']}")
            question_ids = self._string_list(body, "question_ids", max_length=128, ident=True)
            requested_evidence = self._string_list(body, "requested_evidence",
                                                   max_length=EVIDENCE_TEXT_MAX)
            parent = owner.requests.get(entry.request_id or "")
            follow_up: dict[str, Any] | None = None
            if decision == "request_more":
                if parent is None:
                    raise Refusal("unknown_request", f"request {entry.request_id} was not issued "
                                                     f"by {recipient}")
                scope = self._follow_up_scope(parent, body)
                questions = self._follow_up_questions(body)
                request_id = self._follow_up_id(recipient, owner, parent.request_id, body)
                case_rfc = {
                    "request_id": request_id, "parent_request_id": parent.request_id,
                    "subject_ref": parent.rfc["request"]["subject_ref"], "audience": recipient,
                    "issuer": recipient, "questions": questions,
                    "issued_at": clock.as_of_text,
                    "expires_at": format_utc(clock.as_of + FOLLOW_UP_TTL), **scope,
                }
                try:
                    record, forwarded, _ = self._issue(recipient, case_rfc, source="follow_up")
                except Refusal as exc:
                    if exc.code == "invalid_request":
                        raise Refusal("invalid_disposition", exc.reason) from None
                    raise
                entry.follow_ups.append(record.request_id)
                follow_up = {"request_id": record.request_id, "rfc": copy.deepcopy(record.rfc),
                             "account_refs": list(record.account_refs or []), "forwarded": forwarded}
            disposition = {
                "disposition_id": canon.make_id("dsp", recipient, package_id, len(previous),
                                                clock.as_of_text),
                "package_id": package_id, "package_digest": entry.package_sha256,
                "request_id": entry.request_id, "recipient": recipient, "analyst": analyst,
                "decision": decision, "reason": reason, "question_ids": question_ids,
                "requested_evidence": requested_evidence, "decided_at": clock.as_of_text,
                "supersedes": previous[-1]["disposition_id"] if previous else None,
            }
            owner.dispositions.append(disposition)
            audit["request_id"] = entry.request_id
            audit["detail"] = f"{decision} on package {package_id}: {disposition['disposition_id']}"
            if follow_up is not None:
                audit["detail"] += f"; follow-up request {follow_up['request_id']}"
        return {"disposition": copy.deepcopy(disposition), "follow_up": follow_up}

    # ------------------------------------------------------------------ feed

    def feed_server(self) -> Any:
        """The ``FeedServer`` over this case's clock, built on first use. ``sink_unavailable``
        (503) when the bank_feed module is not present."""
        clock = self._require_clock()
        if self._feed_server is None:
            try:
                from alibi.connectors.bank_feed import FeedServer
            except ImportError:
                raise Refusal("sink_unavailable", FEED_UNAVAILABLE, status=503) from None
            self._feed_server = FeedServer(clock, account_ref=self.state.account_ref,
                                           connection_id=f"conn-{clock.subject_ref}")
        return self._feed_server

    def feed_page(self, cursor: str | None, limit: int | None) -> dict[str, Any]:
        if limit is None:
            limit = 100
        if limit < 1:
            raise Refusal("invalid_query", "limit must be at least 1")
        return self.feed_server().page(cursor, min(limit, 500))

    def feed_txn(self, txn_id: str) -> dict[str, Any]:
        row = self.feed_server().latest(txn_id)
        if row is None:
            raise Refusal("not_found", f"no visible transaction {_clip(txn_id, 80)}")
        return {"as_of": self.as_of_text, "source_version": FEED_VERSION, "transaction": row}


# --------------------------------------------------------------------------- the app


def create_bank_app(*, keystore: DemoKeystore, registry: TrustRegistry,
                    tokens: dict[str, str] | None = None, state_path: Path | str | None = None,
                    rfc_sink: RfcSink | None = None,
                    case_loader: CaseLoader = load_case_without_gold) -> FastAPI:
    """The mock bank as a FastAPI app (SERVICES.md, "Mock bank").

    ``tokens`` maps ``bank_a``, ``bank_b`` and ``feed`` to bearer tokens; without it
    ``load_tokens()`` reads the environment or ``data/alibi/bank_tokens.json``. State lives in
    memory, or at ``state_path`` as JSON (loaded when the file exists, replaced atomically on
    every write). ``rfc_sink`` receives every issued request (``plug.RfcSink``). The service
    object is reachable as ``app.state.bank``.
    """
    resolved = dict(tokens) if tokens is not None else load_tokens()
    missing = [name for name in TOKEN_ENV if not resolved.get(name)]
    if missing:
        raise ValueError(f"tokens are missing for {', '.join(missing)}")
    bank = BankService(keystore=keystore, registry=registry, tokens=resolved,
                       state_path=state_path, rfc_sink=rfc_sink, case_loader=case_loader)
    app = FastAPI(title="Alibi mock bank", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bank = bank
    install_refusals(app)

    def _bearer(request: Request) -> str | None:
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" and token.strip() else None

    def require_recipient(request: Request) -> str:
        identity = bank.identity(_bearer(request))
        if identity not in RECIPIENTS:
            raise Refusal("unauthenticated", "a valid recipient bearer token is required")
        return identity

    def require_feed(request: Request) -> str:
        identity = bank.identity(_bearer(request))
        if identity != FEED:
            raise Refusal("unauthenticated", "a valid feed bearer token is required")
        return identity

    # -- admin and identity

    @app.get("/")
    def service_identity() -> dict[str, Any]:
        return bank.identity_view()

    @app.post("/admin/seed")
    def admin_seed(case: int = 1, as_of: str | None = None) -> dict[str, Any]:
        return bank.seed(case, as_of)

    @app.post("/admin/advance")
    def admin_advance(as_of: str | None = None) -> dict[str, Any]:
        return bank.advance(as_of)

    @app.get("/whoami")
    def whoami(recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return {"recipient": recipient, "analyst": bank.analyst(recipient)}

    # -- requests

    @app.post("/rfc")
    async def post_rfc(request: Request, recipient: str = Depends(require_recipient)) -> JSONResponse:
        body = await _json_object(request, "invalid_request", allow_empty=True)
        content, status = bank.issue_request(recipient, body)
        return JSONResponse(status_code=status, content=content)

    @app.get("/rfc")
    def get_rfcs(recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.list_requests(recipient)

    @app.get("/rfc/{request_id}")
    def get_rfc(request_id: str, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.get_request(recipient, request_id)

    # -- delivery and inbox

    @app.post("/deliver")
    async def deliver(request: Request, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        raw = await _json_object(request, "invalid_package", allow_empty=False)
        return bank.deliver(recipient, raw)

    @app.get("/inbox")
    def inbox(recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.inbox(recipient)

    @app.get("/packages/{package_id}")
    def package(package_id: str, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.package(recipient, package_id)

    @app.get("/packages/{package_id}/attachments")
    def attachments(package_id: str, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.attachments(recipient, package_id)

    @app.get("/packages/{package_id}/raw")
    def raw_package(package_id: str, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.raw_package(recipient, package_id)

    @app.post("/packages/{package_id}/disposition")
    async def disposition(package_id: str, request: Request,
                          recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        body = await _json_object(request, "invalid_disposition", allow_empty=False)
        return bank.dispose(recipient, package_id, body)

    @app.get("/packages/{package_id}/dispositions")
    def dispositions(package_id: str, recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.dispositions(recipient, package_id)

    @app.get("/audit")
    def audit(recipient: str = Depends(require_recipient)) -> dict[str, Any]:
        return bank.audit(recipient)

    # -- feed

    @app.get("/feed/transactions")
    def feed_page(cursor: str | None = None, limit: int | None = None,
                  _: str = Depends(require_feed)) -> dict[str, Any]:
        return bank.feed_page(cursor, limit)

    @app.get("/feed/transactions/{txn_id}")
    def feed_txn(txn_id: str, _: str = Depends(require_feed)) -> dict[str, Any]:
        return bank.feed_txn(txn_id)

    return app
