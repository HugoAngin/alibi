"""Mock bank state: what each recipient identity has sent, received and decided.

``BankState`` is plain data with no FastAPI in it. It holds the seeded case summary (number,
subject, account, bank rows), the simulated clock as UTC text, and one ``RecipientState`` per
recipient identity (``bank_a``, ``bank_b``):

* ``requests``: Requests for Context this recipient issued, in issue order, by ``request_id``
  (the signed ``{"request", "signature"}`` dict, the account refs, the parent, times, scope,
  question ids, the source ``case | follow_up | custom`` and the packages that answered it);
* ``inbox``: delivered Context Packages by ``package_id``. A ``verified`` entry keeps the raw
  package dict and the full verification report; a ``rejected`` entry keeps the receipt, the
  reasons and digest, and of the verification report only ``authentication`` and ``errors``:
  never the package bytes, nor the sources or claims the verifier derived from the body;
* ``dispositions``: analyst decisions, in order;
* ``audit``: every accepted and refused action, in order.

``save(path)`` writes JSON to a temporary file beside ``path`` and ``os.replace``s it, so a
reader never sees a half-written file. ``load(path)`` reads it back. Nothing here is a token
or a key: the state file holds case data, signed requests and packages only.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["RECIPIENTS", "BankState", "InboxEntry", "RecipientState", "SentRequest"]

RECIPIENTS = ("bank_a", "bank_b")
STATE_VERSION = "alibi.bank_state.v1"


@dataclass
class SentRequest:
    """One Request for Context this recipient issued (``rfc`` is the signed dict)."""

    request_id: str
    rfc: dict[str, Any]
    account_refs: list[str] | None
    parent_request_id: str | None
    issued_at: str
    expires_at: str
    scope: dict[str, Any]
    question_ids: list[str]
    source: str                         # case | follow_up | custom
    answered_by: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """The ``GET /rfc`` listing shape."""
        return {
            "request_id": self.request_id,
            "parent_request_id": self.parent_request_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "scope": copy.deepcopy(self.scope),
            "question_ids": list(self.question_ids),
            "source": self.source,
            "answered_by": list(self.answered_by),
        }


@dataclass
class InboxEntry:
    """One delivered package. ``package`` is kept for verified entries only; for a rejected
    entry ``verification`` holds ``{authentication, errors}`` only."""

    package_id: str
    request_id: str | None
    status: str                         # verified | rejected
    received_at: str
    package_sha256: str
    receipt: dict[str, Any]
    verification: dict[str, Any]
    reasons: list[str] = field(default_factory=list)
    package: dict[str, Any] | None = None
    expires_at: str | None = None
    refusal_code: str | None = None     # the code the first delivery was refused with
    refusal_reason: str | None = None
    follow_ups: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == "verified"


@dataclass
class RecipientState:
    """Everything one recipient identity has sent, received and decided."""

    requests: dict[str, SentRequest] = field(default_factory=dict)
    inbox: dict[str, InboxEntry] = field(default_factory=dict)
    dispositions: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def dispositions_of(self, package_id: str) -> list[dict[str, Any]]:
        """The dispositions recorded for one package, in order."""
        return [copy.deepcopy(d) for d in self.dispositions if d["package_id"] == package_id]

    def follow_up_count(self, parent_request_id: str) -> int:
        return sum(1 for r in self.requests.values() if r.parent_request_id == parent_request_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "requests": [asdict(r) for r in self.requests.values()],
            "inbox": [asdict(e) for e in self.inbox.values()],
            "dispositions": copy.deepcopy(self.dispositions),
            "audit": copy.deepcopy(self.audit),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RecipientState":
        state = cls()
        for raw in data.get("requests", []):
            request = SentRequest(**raw)
            state.requests[request.request_id] = request
        for raw in data.get("inbox", []):
            entry = InboxEntry(**raw)
            state.inbox[entry.package_id] = entry
        state.dispositions = list(data.get("dispositions", []))
        state.audit = list(data.get("audit", []))
        return state


@dataclass
class BankState:
    """The mock bank's whole state: case summary, clock text and per-recipient state."""

    case_number: int | None = None
    subject_ref: str | None = None
    account_ref: str | None = None
    bank_rows: list[dict[str, Any]] = field(default_factory=list)
    as_of: str | None = None
    recipients: dict[str, RecipientState] = field(
        default_factory=lambda: {name: RecipientState() for name in RECIPIENTS})

    @property
    def seeded(self) -> bool:
        return self.case_number is not None and self.as_of is not None

    def reset(self, *, case_number: int, subject_ref: str, account_ref: str | None,
              bank_rows: list[dict[str, Any]], as_of: str) -> None:
        """Load a case summary and clear every recipient's state."""
        self.case_number = case_number
        self.subject_ref = subject_ref
        self.account_ref = account_ref
        self.bank_rows = copy.deepcopy(bank_rows)
        self.as_of = as_of
        self.recipients = {name: RecipientState() for name in RECIPIENTS}

    def recipient(self, name: str) -> RecipientState:
        """The state of one recipient identity (created empty when missing)."""
        if name not in self.recipients:
            self.recipients[name] = RecipientState()
        return self.recipients[name]

    # ------------------------------------------------------------------ serialisation

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "case_number": self.case_number,
            "subject_ref": self.subject_ref,
            "account_ref": self.account_ref,
            "bank_rows": copy.deepcopy(self.bank_rows),
            "as_of": self.as_of,
            "recipients": {name: r.to_json() for name, r in self.recipients.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BankState":
        if not isinstance(data, dict):
            raise ValueError("bank state must be a JSON object")
        state = cls(case_number=data.get("case_number"), subject_ref=data.get("subject_ref"),
                    account_ref=data.get("account_ref"), bank_rows=list(data.get("bank_rows", [])),
                    as_of=data.get("as_of"))
        recipients = data.get("recipients") or {}
        state.recipients = {name: RecipientState.from_json(raw) for name, raw in recipients.items()}
        for name in RECIPIENTS:
            state.recipients.setdefault(name, RecipientState())
        return state

    def save(self, path: Path | str) -> Path:
        """Write the state as JSON with an atomic replace (temp file, then ``os.replace``)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        text = json.dumps(self.to_json(), indent=1, sort_keys=True, ensure_ascii=False)
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return target

    @classmethod
    def load(cls, path: Path | str) -> "BankState":
        """Read a state file written by ``save``."""
        with open(path, encoding="utf-8") as handle:
            return cls.from_json(json.load(handle))
