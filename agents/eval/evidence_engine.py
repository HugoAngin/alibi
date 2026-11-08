"""Deterministic evidence-dependency checks and bounded retrieval planning for one RFC.

Advisory only. The engine maps a bank credit and the evidence visible at the request's issue
time to explicit prerequisite checks (pass, fail or missing), plans a small number of lookups
from references and gaps it can see, executes them through the request-scoped lookup callback,
and records what each lookup changed. It never signs, approves, applies policy or decides a
verdict: S5's backend gates own those. Its answers are drafts produced by the same rules as the
fixed baseline (``agents.eval.baselines._rules``). A difference from the baseline can come from
three places, which the benchmark counts separately: retrieval (which lookups ran), the
engine's stricter index (naive timestamps, records dated after the request and same-ID
cross-issuer records are excluded, and an identity whose bytes conflict is quarantined
whatever the arrival order), and holds (an identity failure, incomplete retrieval, or support
that cites a self-supplied record forces an unresolved draft).

Matching is a scan, not an index. Records are stored by identity, but each evaluation runs the
shared candidate rule over the whole visible-and-retrieved pool for each requested credit.
That is fine for one passport's records and has not been measured at scale.

Inputs are a case without gold and a request ID (``evidence_report``), or a prepared request
view plus its lookup callback (``run_engine``). Case number, family, variant, title and any
hidden lookup pool are never read. Money is integer minor units throughout.

What a check's ``basis`` means:

* ``observed``: a field read from a bank row or merchant record as it was delivered to the
  passport or returned by the lookup. This engine does not verify signatures, so ``observed``
  means "as delivered", not "issuer-signed".
* ``computed``: integer arithmetic over observed fields.
* ``asserted``: the holder's claim, or a self-supplied import. Its provenance is unverified
  whatever trust the import itself claims.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Callable

from agents.data.api import to_contract
from agents.eval.baselines import _rules
from agents.eval.case_io import request_context
from agents.eval.settlement import BUCKETS, WINDOW, _candidate_import, match_credit
from tools.lockbox import is_open, lockbox_from

DEFAULT_BUDGET = 6
MAX_DATE_LOOKUPS = 2
PASS, FAIL, MISSING = "pass", "fail", "missing"
TIERS = ("exact_reference", "document_reference", "covering_record", "payout_date")
LOOKUP_KEYS = ("payout_reference", "record_id", "covers_record_id", "payout_date")
TOKEN = re.compile(r"\b([A-Z]{1,6}(?:[-_][A-Z0-9]+)+)\b")
MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d+)[.,](\d{2})(?![.,]?\d)")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DOTTED_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
WORD_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", re.IGNORECASE)
ORDER = re.compile(r"#(\d{3,})\b")
MONEY_KINDS = (*BUCKETS, "payout", "balance_release")


# --------------------------------------------------------------------------- typed output

@dataclass(frozen=True)
class SourceRef:
    source_id: str          # bank:<txn_id>, merchant:<record_id> or import:<record_id>
    revision: int
    provenance: str         # bank_record, merchant_record or self_supplied


@dataclass(frozen=True)
class Check:
    check_id: str           # <txn_id>/<name>, stable across runs
    name: str
    status: str             # pass, fail or missing
    basis: str              # observed, computed or asserted
    detail: str             # built in code from ids, amounts and statuses only
    refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class Residual:
    """Credit minus the recomputed net of the covered components, in minor units.

    ``basis`` is ``settlement`` when the components are those of the linked payout,
    ``unlinked_payout`` for a reference-bound payout that fails the settlement rule (pending,
    failed, late or wrong amount), or ``balance_release`` when only a release to the platform
    balance names them. Only ``settlement`` can back a claim; a zero residual on the other
    bases supports nothing.
    """
    basis: str
    currency: str
    credit_minor: int
    sales_minor: int
    refunds_minor: int
    fees_minor: int
    net_minor: int
    residual_minor: int
    component_count: int


@dataclass(frozen=True)
class LookupAction:
    action_id: str          # L1, L2, ... in planning order
    txn_id: str | None
    key: str                # one of LOOKUP_KEYS
    value: str
    tier: str               # TIERS or missing_component
    reason: str
    justified_by: tuple[str, ...]   # source IDs whose visible fields justify the call
    state: str              # executed, cached or skipped_budget
    outcome: str | None     # ok, not_found, refused, error, or None when not executed
    returned: tuple[SourceRef, ...] = ()
    rejected: int = 0       # returned items dropped: foreign subject, future, malformed


@dataclass(frozen=True)
class Transition:
    step: int               # 0 is the evaluation of the visible evidence before any lookup
    action_ids: tuple[str, ...]
    new_refs: tuple[SourceRef, ...]
    changes: tuple[tuple[str, str | None, str], ...]       # (check_id, before, after)
    link_states: tuple[tuple[str, str | None, str], ...]   # (txn_id, before, after)


@dataclass(frozen=True)
class CreditReport:
    txn_id: str
    link_state: str
    payout_record_ids: tuple[str, ...]
    checks: tuple[Check, ...]
    residual: Residual | None
    gaps: tuple[str, ...]                # names of checks that did not pass
    retrieval_complete: bool
    diagnosed_gap: str | None            # team gap code read from the checks; None when nothing is open


@dataclass(frozen=True)
class Answer:
    """Advisory draft from the baseline rules over the engine's evidence, for S5's gate."""
    question_id: str
    status: str
    gap: str | None
    facts_json: str                      # canonical JSON object, integer minor units
    evidence_refs: tuple[SourceRef, ...]
    hold_reasons: tuple[str, ...] = ()   # non-empty forces an unresolved draft with no facts

    @property
    def facts(self) -> dict:
        return json.loads(self.facts_json)


@dataclass(frozen=True)
class EvidenceReport:
    request_id: str
    audience: str
    subject_ref: str
    decision_time: str
    scope: str                          # txn_ids or period
    credits: tuple[CreditReport, ...]
    answers: tuple[Answer, ...]
    actions: tuple[LookupAction, ...]
    trace: tuple[Transition, ...]
    dependencies: tuple[SourceRef, ...]
    exclusions: tuple[tuple[str, str], ...]   # (source_id, reason)
    budget: int
    calls: int
    cache_hits: int
    observed_inputs: int
    visible_digest: str
    advisory: str = ("Planning output for review. Signature verification, policy, approval and the "
                     "final verdict stay with the backend gates.")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["answers"] = {answer.question_id: {key: value for key, value in asdict(answer).items()
                                                if key != "facts_json"} | {"facts": answer.facts}
                           for answer in self.answers}
        return json.loads(json.dumps(data))

    def checks(self) -> dict[str, str]:
        return {check.check_id: check.status for credit in self.credits for check in credit.checks}

    def answer(self, question_id: str) -> Answer:
        return next(answer for answer in self.answers if answer.question_id == question_id)


# --------------------------------------------------------------------------- cache

class LookupCache:
    """Memo of lookup results for one request scope, reused across calls only on request.

    Within one ``run_engine`` call each (key, value) is fetched at most once regardless of the
    cache. Across calls, results are reused only when the caller passes the same
    ``source_version`` (a watermark from the merchant connector that changes whenever any record
    it serves may have changed). The engine cannot see lookup-only records, so without that
    token it cannot know a served revision changed, and every call refetches. The scope digest
    also covers subject, request, audience, decision time and every visible input with its
    revision, so a different recipient, request, time or delivered revision clears the memo.
    """

    def __init__(self):
        self.scope: str | None = None
        self._rows: dict[tuple[str, str], tuple[str, tuple[dict, ...]]] = {}
        self.hits = 0

    def bind(self, scope: str | None) -> None:
        if scope is None or scope != self.scope:
            self._rows.clear()
            self.scope = scope

    def get(self, key: tuple[str, str]):
        row = self._rows.get(key)
        if row is not None:
            self.hits += 1
        return row

    def put(self, key: tuple[str, str], outcome: str, events: list[dict]) -> None:
        if outcome in ("ok", "not_found"):     # failures are never memoised
            self._rows[key] = (outcome, tuple(json.loads(json.dumps(event)) for event in events))


# --------------------------------------------------------------------------- index

def _time(value: str) -> datetime:
    """Parse a contract timestamp. Naive or non-UTC values raise ValueError, never TypeError later."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC with an explicit offset")
    return parsed


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _valid_merchant(row: dict) -> bool:
    try:
        if not all(isinstance(row.get(key), str) and row[key]
                   for key in ("record_id", "event_id", "issuer_id", "subject_ref", "kind", "occurred_at")):
            return False
        if type(row.get("revision")) is not int or row["revision"] < 1:
            return False
        _time(row["occurred_at"])
        if row["kind"] in MONEY_KINDS:
            if type(row.get("amount_minor")) is not int or row["amount_minor"] < 0:
                return False
            if not isinstance(row.get("currency"), str):
                return False
        if row["kind"] == "payout":
            if not isinstance(row.get("covers_record_ids"), list) or not isinstance(row.get("destination"), str):
                return False
            if row.get("status") == "settled" or row.get("settled_at") is not None:
                _time(row["settled_at"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


def _content(row: dict) -> str:
    """Record content without its transport event ID, for comparing copies of one revision."""
    return _canonical({key: value for key, value in row.items() if key not in ("event_id", "source_kind", "_body")})


def _resolve(rows: dict[str, dict], identity, version) -> tuple[dict, set, set]:
    """Active row per identity from a set of distinct bodies, independent of arrival order.

    ``version(row)`` yields (key, fingerprint) pairs. An identity is quarantined when one key
    carries two fingerprints: the same event ID with different bytes, or the same revision with
    different content. It is withheld, never settled by whichever copy came first. The same
    revision redelivered under a new event ID with identical content is a duplicate. Returns the
    active rows, the quarantined identities and the superseded (identity, revision) pairs.
    """
    prints: dict[tuple, set[str]] = {}
    members: dict[tuple, set[str]] = {}
    for body, row in rows.items():
        for key, fingerprint in version(row):
            prints.setdefault(key, set()).add(fingerprint)
            members.setdefault(key, set()).add(body)
    quarantined = {identity(rows[body]) for key, group in prints.items() if len(group) > 1 for body in members[key]}
    active: dict = {}
    for body in sorted(rows):
        row, key = rows[body], identity(rows[body])
        if key not in quarantined and (key not in active or row["revision"] > active[key]["revision"]):
            active[key] = row
    superseded = {(identity(row), row["revision"]) for row in rows.values()
                  if identity(row) in active and row["revision"] < active[identity(row)]["revision"]}
    return active, quarantined, superseded


class _Index:
    """Visible and retrieved records for one subject at one decision time.

    Every distinct body is kept, and the active revision is computed from the set, so the order
    of deliveries and lookups cannot change it. Merchant identity is (issuer, record_id). It is
    quarantined (withheld from matching) when one (issuer, event_id) or one
    (issuer, record_id, revision) carries different bytes, whichever arrived first. The same
    record ID from two issuers is withheld as a collision. Identical bytes are duplicates. Bank
    rows follow the same rule by (txn_id, revision), and imports by (record_id, revision).
    """

    def __init__(self, subject: str, decision_time: str):
        self.subject = subject
        self.at = _time(decision_time)
        self.rows: dict[str, dict] = {}            # canonical body -> contract row
        self.import_rows: dict[str, dict] = {}
        self.import_docs: dict[str, dict] = {}
        self.bank_rows: dict[str, dict] = {}
        self.rejected: set[tuple[str, str]] = set()
        self.observed_inputs = 0
        self._state: dict | None = None

    def state(self) -> dict:
        if self._state is None:
            merchant = _resolve(self.rows, lambda r: (r["issuer_id"], r["record_id"]),
                                lambda r: [(("event", r["issuer_id"], r["event_id"]), r["_body"]),
                                           (("revision", r["issuer_id"], r["record_id"], r["revision"]), _content(r))])
            imports = _resolve(self.import_rows, lambda r: r["record_id"],
                               lambda r: [((r["record_id"], r["revision"]), _content(r))])
            bank = _resolve(self.bank_rows, lambda r: r["txn_id"], lambda r: [((r["txn_id"], r["revision"]), _canonical(r))])
            self._state = {"merchant": merchant, "imports": imports, "bank": bank}
        return self._state

    @property
    def active(self) -> dict[tuple[str, str], dict]:
        return self.state()["merchant"][0]

    @property
    def quarantined_ids(self) -> set[str]:
        return {record_id for _, record_id in self.state()["merchant"][1]}

    @property
    def quarantined_rows(self) -> list[dict]:
        quarantined = self.state()["merchant"][1]
        return [row for _, row in sorted(self.rows.items()) if (row["issuer_id"], row["record_id"]) in quarantined]

    @property
    def bank(self) -> dict[str, dict]:
        return self.state()["bank"][0]

    def add_merchant(self, raw: dict, *, received_at: str | None = None) -> tuple[SourceRef | None, str]:
        self.observed_inputs += 1
        if not isinstance(raw, dict):
            return None, "malformed"
        try:
            row = to_contract(raw)
        except (KeyError, TypeError, AttributeError):
            row = {}
        source = f"merchant:{raw.get('record_id')}"
        if not _valid_merchant(row):
            self.rejected.add((source, "malformed"))
            return None, "malformed"
        if row["subject_ref"] != self.subject:
            self.rejected.add((source, "foreign_subject"))
            return None, "foreign_subject"
        try:
            late = _time(row["occurred_at"]) > self.at or bool(received_at and _time(received_at) > self.at)
        except ValueError:
            self.rejected.add((source, "malformed"))
            return None, "malformed"
        if late:
            self.rejected.add((source, "after_decision_time"))
            return None, "after_decision_time"
        body = _canonical(raw)
        if body in self.rows:
            return None, "duplicate"
        self.rows[body] = {**row, "source_kind": "merchant", "_body": body}
        self._state = None
        if (row["issuer_id"], row["record_id"]) in self.state()["merchant"][1]:
            return None, "quarantined"
        return SourceRef(source, row["revision"], "merchant_record"), "accepted"

    def add_import(self, record: dict) -> None:
        self.observed_inputs += 1
        if not isinstance(record, dict) or record.get("subject_ref") != self.subject:
            return
        body = _canonical(record)
        if any(isinstance(record.get(key), str) for key in ("text", "message")):
            self.import_docs[body] = record
        try:
            timed = all(_time(record[key]) is not None for key in ("occurred_at", "settled_at")
                        if record.get(key) is not None)
        except ValueError:
            timed = False
        if timed and _candidate_import(record):
            # Whatever trust or issuer the import names, it stays self-supplied.
            self.import_rows[body] = {**record, "source_kind": "import"}
            self._state = None

    def add_bank(self, row: dict) -> None:
        self.observed_inputs += 1
        try:
            valid = (isinstance(row.get("txn_id"), str) and type(row.get("revision")) is int
                     and type(row.get("amount_minor")) is int and isinstance(row.get("currency"), str)
                     and _time(row["booked_at"]) is not None)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            self.rejected.add((f"bank:{row.get('txn_id') if isinstance(row, dict) else None}", "malformed"))
            return
        if _time(row["booked_at"]) > self.at:
            self.rejected.add((f"bank:{row['txn_id']}", "after_decision_time"))
            return
        self.bank_rows[_canonical(row)] = row
        self._state = None

    def pool(self) -> tuple[dict[str, dict], set[str]]:
        """Record-ID keyed pool for the shared settlement rules, plus withheld colliding IDs."""
        issuers: dict[str, set[str]] = {}
        for issuer, record_id in self.active:
            issuers.setdefault(record_id, set()).add(issuer)
        collisions = {record_id for record_id, names in issuers.items() if len(names) > 1}
        withheld = collisions | self.quarantined_ids
        merchant = {record_id: row for (_, record_id), row in sorted(self.active.items())
                    if record_id not in collisions}
        imports = {key: row for key, row in sorted(self.state()["imports"][0].items()) if key not in withheld}
        return {**imports, **merchant}, collisions

    def merchant_documents(self) -> list[tuple[str, dict]]:
        docs = [(f"merchant:{row['record_id']}", row) for _, row in sorted(self.active.items())
                if any(isinstance(row.get(key), str) for key in ("text", "message"))]
        return docs + [(f"import:{row.get('record_id')}", row) for _, row in sorted(self.import_docs.items())]

    def exclusions(self, collisions: set[str]) -> tuple[tuple[str, str], ...]:
        merchant, imports, bank = (self.state()[key] for key in ("merchant", "imports", "bank"))
        rows = set(self.rejected)
        rows |= {(f"merchant:{record_id}", "conflicting_identity") for _, record_id in merchant[1]}
        rows |= {(f"import:{record_id}", "conflicting_identity") for record_id in imports[1]}
        rows |= {(f"bank:{txn_id}", "conflicting_identity") for txn_id in bank[1]}
        rows |= {(f"merchant:{record_id}@{revision}", "superseded_revision") for (_, record_id), revision in merchant[2]}
        rows |= {(f"merchant:{record_id}", "identity_collision") for record_id in collisions}
        return tuple(sorted(rows))


def _ref(row: dict) -> SourceRef:
    if row.get("source_kind") == "import":
        return SourceRef(f"import:{row['record_id']}", row["revision"], "self_supplied")
    return SourceRef(f"merchant:{row['record_id']}", row["revision"], "merchant_record")


def _bank_ref(row: dict) -> SourceRef:
    return SourceRef(f"bank:{row['txn_id']}", row["revision"], "bank_record")


# --------------------------------------------------------------------------- text clues

def _amounts(text: str) -> list[int]:
    return [int(re.sub(r"[.,]", "", whole)) * 100 + int(cents) for whole, cents in MONEY.findall(text)]


def _dates(text: str, year: int) -> set[str]:
    found = {f"{y}-{m}-{d}" for y, m, d in ISO_DATE.findall(text)}
    found |= {f"{y}-{int(m):02d}-{int(d):02d}" for d, m, y in DOTTED_DATE.findall(text)
              if 1 <= int(m) <= 12 and 1 <= int(d) <= 31}
    for d, month, y in WORD_DATE.findall(text):
        found.add(f"{y or year}-{MONTHS.index(month.lower()) + 1:02d}-{int(d):02d}")
    return found


def _tokens(text: str) -> list[str]:
    return [token for token in TOKEN.findall(text) if any(ch.isdigit() for ch in token)]


@dataclass
class _Clues:
    references: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    cover_records: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @staticmethod
    def _add(target: dict, key: str, source: str) -> None:
        target[key] = tuple(sorted({*target.get(key, ()), source}))


def _in_window(credit: dict, when: str) -> bool:
    booked = _time(credit["booked_at"])
    return booked - WINDOW <= _time(when) <= booked


def _tied_releases(credit: dict, index: _Index) -> list[dict]:
    return [row for _, row in sorted(index.active.items())
            if row["kind"] == "balance_release" and row["amount_minor"] == credit["amount_minor"]
            and row["currency"] == credit["currency"] and _in_window(credit, row["occurred_at"])]


def _clues(credit: dict, index: _Index, pool: dict) -> _Clues:
    """Collect lookup keys from documents tied to this credit by amount, reference or release.

    A line is tied when it states the credit amount or names the credit's own reference. A
    whole document is tied when it is a same-amount balance release in the settlement window,
    or when the credit amount is the only amount it states.
    """
    clues = _Clues()
    known_ids = {row.get("release_reference") for row in index.active.values()} - {None}
    known_ids |= {row["record_id"] for row in index.active.values()}
    own = {credit.get("payment_reference")} - {None}
    releases = {f"merchant:{row['record_id']}" for row in _tied_releases(credit, index)}
    orders = {str(row["order_number"]): row["record_id"] for row in pool.values()
              if row.get("kind") == "sale" and row.get("order_number") is not None}
    booked = _time(credit["booked_at"])
    for source, row in index.merchant_documents():
        text = row.get("text") if isinstance(row.get("text"), str) else row.get("message")
        if not isinstance(text, str):
            continue
        lines = text.splitlines() or [text]
        amounts = _amounts(text)
        whole = source in releases or (amounts and set(amounts) == {credit["amount_minor"]})
        tied = [line for line in lines
                if credit["amount_minor"] in _amounts(line) or any(ref in line for ref in own)]
        fragments = [text] if whole else tied
        try:
            year = _time(row.get("occurred_at") or row.get("received_at") or credit["booked_at"]).year
        except (TypeError, ValueError):
            year = booked.year
        for fragment in fragments:
            for token in _tokens(fragment):
                if token not in known_ids:
                    _Clues._add(clues.references, token, source)
            for day in sorted(_dates(fragment, year)):
                try:
                    stamp = _time(f"{day}T00:00:00Z")
                except ValueError:
                    continue
                if (booked - WINDOW).date() <= stamp.date() <= booked.date():
                    _Clues._add(clues.dates, day, source)
            for number in ORDER.findall(fragment):
                if number in orders:
                    _Clues._add(clues.cover_records, orders[number], source)
    for release in _tied_releases(credit, index):
        for record_id in sorted(release.get("covers_record_ids") or ()):
            if isinstance(record_id, str):
                _Clues._add(clues.cover_records, record_id, f"merchant:{release['record_id']}")
    return clues


# --------------------------------------------------------------------------- checks

@dataclass
class _CreditState:
    txn_id: str
    credit: dict | None
    link: dict
    focus: list[dict]
    focus_basis: str | None
    missing: list[str]
    checks: list[Check]
    residual: Residual | None
    reference_conflict: bool
    identity_failure: bool


def _settled_ok(row: dict, credit: dict) -> bool:
    return row.get("status") == "settled" and bool(row.get("settled_at")) and _in_window(credit, row["settled_at"])


def _reference_bound(credit: dict, pool: dict, reference: str | None) -> list[dict]:
    if not reference:
        return []
    rows = [row for row in pool.values() if row.get("kind") == "payout" and reference in
            (row.get("payout_reference"), row.get("withdrawal_id"), row.get("settlement_batch_id"))]
    batches = {row.get("settlement_batch_id") for row in rows} - {None}
    rows += [row for row in pool.values() if row.get("kind") == "payout"
             and row.get("settlement_batch_id") in batches and row not in rows]
    return sorted(rows, key=lambda row: row["record_id"])


def _evaluate(txn_id: str, index: _Index, pool: dict, collisions: set[str], holder: dict) -> _CreditState:
    credit = index.bank.get(txn_id)
    prefix = f"{txn_id}/"
    if credit is None:
        conflicting = txn_id in index.state()["bank"][1]
        check = Check(prefix + "credit.observed", "credit.observed", FAIL if conflicting else MISSING, "observed",
                      "bank rows with the same revision and different bytes were withheld" if conflicting
                      else "no visible bank row for the requested transaction")
        return _CreditState(txn_id, None, {"state": "unmatched", "payouts": []}, [], None, [], [check],
                            None, False, conflicting)
    bank = _bank_ref(credit)
    checks = [Check(prefix + "credit.observed", "credit.observed", PASS, "observed",
                    f"bank row {credit['txn_id']} revision {credit['revision']}", (bank,)),
              Check(prefix + "credit.inflow", "credit.inflow", PASS if credit["amount_minor"] > 0 else FAIL,
                    "observed", f"{credit['amount_minor']} {credit['currency']} booked {credit['booked_at']}", (bank,))]
    link = match_credit(credit, pool)
    own = credit.get("payment_reference")
    cited = holder.get("payout_reference") if holder.get("txn_id") == txn_id else None
    if link["state"] == "linked":
        focus, basis = list(link["payouts"]), "linked_candidate"
    elif _reference_bound(credit, pool, own):
        focus, basis = _reference_bound(credit, pool, own), "credit_reference"
    elif _reference_bound(credit, pool, cited):
        focus, basis = _reference_bound(credit, pool, cited), "holder_reference"
    else:
        focus, basis = [], None
    refs = tuple(_ref(row) for row in focus)
    names = ", ".join(row["record_id"] for row in focus)

    def add(name, status, basis_kind, detail, refs_=refs):
        checks.append(Check(prefix + name, name, status, basis_kind, detail, refs_))

    identity_failure = False
    missing: list[str] = []
    residual = None
    named = {own, cited} - {None}
    clashing = sorted({row["record_id"] for row in index.quarantined_rows if row["kind"] == "payout" and named &
                       {row.get("payout_reference"), row.get("withdrawal_id"), row.get("settlement_batch_id")}})
    if clashing:
        identity_failure = True
        add("settlement.identity", FAIL, "observed",
            "payout copies with conflicting bytes were withheld: " + ", ".join(clashing), ())
    if not focus:
        add("settlement.record", MISSING, "observed", "no settlement payout retrieved for this credit", ())
    else:
        provenance = "asserted" if any(row.get("source_kind") == "import" for row in focus) else "observed"
        add("settlement.record", PASS, provenance, f"{basis}: {names}")
        add("settlement.destination", PASS if all(row["destination"] == "bank" for row in focus) else FAIL,
            provenance, "destination " + ", ".join(sorted({row["destination"] for row in focus})))
        if own:
            matches = any(own in (row.get("payout_reference"), row.get("withdrawal_id"),
                                  row.get("settlement_batch_id")) for row in focus)
            add("settlement.reference", PASS if matches else FAIL, provenance,
                f"credit reference {own} {'matches' if matches else 'does not match'} {names}")
        statuses = sorted({str(row.get("status")) for row in focus})
        add("settlement.status", PASS if statuses == ["settled"] else FAIL, provenance, "status " + ", ".join(statuses))
        if all(row.get("settled_at") for row in focus):
            ok = all(_in_window(credit, row["settled_at"]) for row in focus)
            add("settlement.timing", PASS if ok else FAIL, provenance,
                f"settled_at {', '.join(row['settled_at'] for row in focus)}; booked {credit['booked_at']}; "
                f"window {WINDOW.days} days before booking")
        else:
            add("settlement.timing", MISSING, provenance, "no settled_at on the focus payout")
        total = sum(row["amount_minor"] for row in focus)
        add("settlement.amount", PASS if total == credit["amount_minor"] else FAIL, "computed",
            f"payout total {total} vs credit {credit['amount_minor']}")
        currencies = {row["currency"] for row in focus}
        add("settlement.currency", PASS if currencies == {credit["currency"]} else FAIL, provenance,
            "currency " + ", ".join(sorted(currencies)))
        issuers = {row.get("issuer_id") for row in focus}
        if len(issuers) != 1:
            identity_failure = True
        add("settlement.issuer", PASS if len(issuers) == 1 else FAIL, provenance,
            "issuer " + ", ".join(sorted(str(name) for name in issuers)))

        covered = list(dict.fromkeys(record_id for row in focus for record_id in row["covers_record_ids"]))
        present = [pool[record_id] for record_id in covered
                   if record_id in pool and pool[record_id].get("kind") in BUCKETS]
        missing = [record_id for record_id in covered if record_id not in pool or pool[record_id].get("kind") not in BUCKETS]
        withheld = [record_id for record_id in covered if record_id in collisions or record_id in index.quarantined_ids]
        if withheld:
            identity_failure = True
            add("components.identity", FAIL, "observed",
                "record IDs with conflicting bytes or more than one issuer were withheld: "
                + ", ".join(withheld[:6]), ())
        add("components.present", PASS if not missing and covered else MISSING, "observed",
            f"{len(present)} of {len(covered)} covered records present"
            + (f"; missing {', '.join(missing[:6])}" if missing else ""), tuple(_ref(row) for row in present))
        (issuer,) = issuers if len(issuers) == 1 else (None,)
        foreign = [row["record_id"] for row in present
                   if row["currency"] != credit["currency"]
                   or (row.get("source_kind") != "import" and row.get("issuer_id") != issuer)]
        if present:
            if foreign:
                identity_failure = True
            add("components.consistent", FAIL if foreign else PASS, "observed",
                "components share the payout issuer and credit currency" if not foreign
                else "issuer or currency differs: " + ", ".join(foreign[:6]), ())
        if covered and not missing and not foreign:
            # Only a linked candidate is a settlement; a pending, failed, late or mismatched payout
            # still gets its arithmetic shown, labelled so it cannot read as support.
            residual = _residual("settlement" if basis == "linked_candidate" else "unlinked_payout",
                                 credit, present)

    if residual is None and not focus:
        for release in _tied_releases(credit, index):
            covered = [record_id for record_id in release.get("covers_record_ids") or () if isinstance(record_id, str)]
            rows = [pool.get(record_id) for record_id in covered]
            if covered and all(row is not None and row.get("kind") in BUCKETS
                               and row["currency"] == credit["currency"] for row in rows):
                residual = _residual("balance_release", credit, rows)
                checks.append(Check(prefix + "release.not_settlement", "release.not_settlement", MISSING, "observed",
                                    f"{release['record_id']} released funds to the platform balance; "
                                    "a bank settlement is still required", (_ref(release),)))
                break
    if residual is None:
        add("arithmetic.residual", MISSING, "computed", "components incomplete; no residual computed", ())
    else:
        add("arithmetic.residual", PASS if residual.residual_minor == 0 else FAIL, "computed",
            f"{residual.basis}: credit {residual.credit_minor} - (sales {residual.sales_minor} - refunds "
            f"{residual.refunds_minor} - fees {residual.fees_minor}) = {residual.residual_minor}", ())
    unique = {"linked": PASS, "ambiguous": FAIL}.get(link["state"], MISSING)
    add("settlement.unique", unique, "computed",
        f"{link['state']} among visible and retrieved candidates"
        + ("" if own else "; the credit carries no reference, so uniqueness rests on amount and timing"), ())
    if holder.get("txn_id") == txn_id:
        stated = holder.get("stated_minor") or {}
        if residual is not None and residual.basis == "settlement":
            computed = {"sales": residual.sales_minor, "refunds": residual.refunds_minor,
                        "fees": residual.fees_minor, "net": residual.net_minor}
            add("holder.breakdown", PASS if stated == computed else FAIL, "asserted",
                "holder's stated breakdown " + ("equals" if stated == computed else "differs from")
                + " the recomputed components", ())
        else:
            add("holder.breakdown", MISSING, "asserted", "no settlement components to compare with", ())
        if cited:
            rows = _reference_bound(credit, pool, cited)
            if not rows:
                status, detail = MISSING, f"holder cites {cited}; no record retrieved"
            elif all(_settled_ok(row, credit) for row in rows):
                status, detail = PASS, f"holder cites {cited}; settled within the window"
            else:
                status, detail = FAIL, f"holder cites {cited}; status " + ", ".join(
                    sorted(f"{row.get('status')}@{row.get('settled_at')}" for row in rows))
            add("holder.reference", status, "asserted", detail, tuple(_ref(row) for row in rows))
    conflict = bool(own) and basis == "credit_reference" and link["state"] != "linked"
    return _CreditState(txn_id, credit, link, focus, basis, missing, checks, residual, conflict, identity_failure)


def _residual(basis: str, credit: dict, rows: list[dict]) -> Residual:
    totals = dict.fromkeys(BUCKETS.values(), 0)
    for row in rows:
        totals[BUCKETS[row["kind"]]] += row["amount_minor"]
    net = totals["sales"] - totals["refunds"] - totals["fees"]
    return Residual(basis, credit["currency"], credit["amount_minor"], totals["sales"], totals["refunds"],
                    totals["fees"], net, credit["amount_minor"] - net, len(rows))


def _diagnose(state: _CreditState, pool: dict, index: _Index) -> str | None:
    """Most specific team gap code the checks justify, in the pinned rule's precedence."""
    credit = state.credit
    if credit is None:
        return "settlement_not_found"
    status = {check.name: check.status for check in state.checks}
    if state.link["state"] == "ambiguous":
        return "ambiguous_candidates"
    if state.link["state"] == "linked":
        if state.missing or state.identity_failure:
            return "missing_cited_record"
        return "amount_mismatch" if status.get("arithmetic.residual") == FAIL else None
    if state.focus:
        statuses = {row.get("status") for row in state.focus}
        if "failed" in statuses:
            return "settlement_failed"
        if "pending" in statuses:
            return "settlement_pending_at_booking"
        if any(row.get("settled_at") and _time(row["settled_at"]) > _time(credit["booked_at"]) for row in state.focus):
            return "settlement_after_credit"
        if status.get("settlement.amount") == FAIL:
            return "amount_mismatch"
        return "settlement_not_found"
    if _tied_releases(credit, index):
        return "balance_release_not_settlement"
    if not any(row.get("kind") in (*BUCKETS, "payout", "balance_release") for row in pool.values()):
        return "no_merchant_records"
    return "settlement_not_found"


def _resolved(state: _CreditState) -> bool:
    if state.credit is None or state.link["state"] == "ambiguous" or state.reference_conflict:
        return True
    return state.link["state"] == "linked" and not state.missing


# --------------------------------------------------------------------------- planner

def _plan(state: _CreditState, index: _Index, pool: dict, holder: dict, tried: set[str],
          done: set[tuple[str, str]]) -> tuple[str | None, list[tuple[str, str, str, tuple[str, ...]]]]:
    """Next tier of (key, value, reason, justified_by) lookups for one unresolved credit."""
    credit = state.credit
    if credit is None:
        return None, []
    bank = f"bank:{credit['txn_id']}"
    own = credit.get("payment_reference")
    if own and "refresh" not in tried:
        # The credit's own reference is always fetched once, even when a pushed payout already
        # links, so a correction served only by the merchant endpoint is not missed.
        tried.add("refresh")
        if ("payout_reference", own) not in done:
            return "exact_reference", [("payout_reference", own, "bank credit carries this payment reference",
                                        (bank,))]
    if _resolved(state):
        return None, []
    if state.link["state"] == "linked":
        return "missing_component", [
            ("record_id", record_id, "component cited by the linked payout is not in the passport",
             tuple(f"merchant:{row['record_id']}" for row in state.focus))
            for record_id in sorted(state.missing) if ("record_id", record_id) not in done
            and record_id not in index.quarantined_ids | index.pool()[1]]   # a refetch cannot settle a conflict
    clues = None
    for tier in TIERS:
        if tier in tried:
            continue
        tried.add(tier)
        actions: list[tuple[str, str, str, tuple[str, ...]]] = []
        if tier == "exact_reference":
            for token in _tokens(credit.get("description") or ""):
                actions.append(("payout_reference", token, "bank description names this reference", (bank,)))
            if holder.get("txn_id") == credit["txn_id"] and isinstance(holder.get("payout_reference"), str):
                actions.append(("payout_reference", holder["payout_reference"],
                                "holder's claim cites this reference (unverified assertion)", ("holder_claim",)))
        else:
            clues = clues or _clues(credit, index, pool)
            if tier == "document_reference":
                actions = [("payout_reference", token, "a document tied to this credit names this reference",
                            sources) for token, sources in sorted(clues.references.items())]
            elif tier == "covering_record":
                # One call: a component named by a tied document, preferring an order a document
                # names explicitly, then a sale. Skipped when a known payout already covers it.
                covered = {record_id for row in pool.values() if row.get("kind") == "payout"
                           for record_id in row.get("covers_record_ids") or ()}
                releases = {f"merchant:{row['record_id']}" for row in _tied_releases(credit, index)}

                def rank(item):
                    record_id, sources = item
                    named = any(source not in releases for source in sources)
                    return (not named, pool.get(record_id, {}).get("kind") != "sale", record_id)

                for record_id, sources in sorted(clues.cover_records.items(), key=rank):
                    if record_id in pool and record_id not in covered and ("covers_record_id", record_id) not in done:
                        actions = [("covers_record_id", record_id,
                                    "a tied document names this component and no known payout covers it", sources)]
                        break
            else:
                actions = [("payout_date", day, "a tied document dates the payout inside the settlement window",
                            sources) for day, sources in sorted(clues.dates.items(), reverse=True)][:MAX_DATE_LOOKUPS]
        actions = [action for action in dict((a[:2], a) for a in actions).values() if action[:2] not in done]
        if actions:
            return tier, actions
    return None, []


# --------------------------------------------------------------------------- engine

def _visible_digest(view: dict, rfc: dict) -> str:
    payload = {key: view.get(key) for key in ("subject_ref", "merchant_deliveries", "imports",
                                              "bank_deliveries", "holder_claim")}
    payload["rfc"] = {key: rfc.get(key) for key in ("request_id", "audience", "issued_at", "txn_ids", "period")}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def run_engine(view: dict, find_evidence: Callable[..., list[dict]], *, budget: int = DEFAULT_BUDGET,
               cache: LookupCache | None = None, source_version: str | None = None) -> EvidenceReport:
    """Evaluate one request view, retrieve within ``budget`` calls, and return the report.

    ``view`` is ``request_context``'s point-in-time input with exactly one RFC; its
    ``merchant_lookup`` list is never read. ``find_evidence`` takes exactly one lookup key.
    ``cache`` results are reused across calls only under the same non-empty ``source_version``.
    """
    if type(budget) is not int or budget < 0:
        raise ValueError("budget must be a non-negative integer")
    [rfc] = view["rfcs"]
    subject = view["subject_ref"]
    if rfc.get("subject_ref") != subject:
        raise ValueError("RFC subject differs from the view subject")
    decision_time = rfc["issued_at"]
    index = _Index(subject, decision_time)
    for entry in view.get("merchant_deliveries", ()):
        if entry.get("received_at", "") <= decision_time:
            index.add_merchant(entry.get("event"), received_at=entry.get("received_at"))
    for entry in view.get("imports", ()):
        if entry.get("received_at", "") <= decision_time:
            index.add_import(entry.get("record"))
    for entry in view.get("bank_deliveries", ()):
        if entry.get("received_at", "") <= decision_time:
            index.add_bank(entry.get("row") or {})
    holder = view.get("holder_claim") or {}
    holder = holder if isinstance(holder, dict) else {}
    digest = _visible_digest(view, rfc)
    cache = cache if cache is not None else LookupCache()
    cache.bind(hashlib.sha256(_canonical([subject, rfc["request_id"], rfc.get("audience"), decision_time,
                                          digest, source_version]).encode()).hexdigest()
               if isinstance(source_version, str) and source_version else None)
    hits_before = cache.hits
    txn_ids = list(dict.fromkeys(rfc.get("txn_ids") or ()))

    def evaluate() -> dict[str, _CreditState]:
        pool, collisions = index.pool()
        return {txn_id: _evaluate(txn_id, index, pool, collisions, holder) for txn_id in txn_ids}

    states = evaluate()
    trace = [Transition(0, (), (), tuple((c.check_id, None, c.status) for s in states.values() for c in s.checks),
                        tuple((txn_id, None, s.link["state"]) for txn_id, s in states.items()))]
    actions: list[LookupAction] = []
    done: set[tuple[str, str]] = set()
    tried: dict[str, set[str]] = {txn_id: set() for txn_id in txn_ids}
    failed: set[str] = set()
    calls = 0
    while True:
        pool, _ = index.pool()
        batch = []
        for txn_id in txn_ids:
            tier, planned = _plan(states[txn_id], index, pool, holder, tried[txn_id], done)
            batch += [(txn_id, tier, *action) for action in planned]
        if not batch:
            break
        step_ids, new_refs = [], []
        for txn_id, tier, key, value, reason, sources in batch:
            if (key, value) in done:
                continue
            done.add((key, value))
            action_id = f"L{len(actions) + 1}"
            step_ids.append(action_id)
            cached = cache.get((key, value))
            if cached is not None:
                state, (outcome, events) = "cached", cached
            elif calls >= budget:
                actions.append(LookupAction(action_id, txn_id, key, value, tier, reason, sources,
                                            "skipped_budget", None))
                failed.add(txn_id)
                continue
            else:
                calls += 1
                state, outcome, events = "executed", *_call(find_evidence, key, value)
                cache.put((key, value), outcome, list(events))
            returned, rejected = [], 0
            for event in events:
                ref, status = index.add_merchant(event)
                if ref is not None:
                    returned.append(ref)
                elif status not in ("duplicate", "stale_revision"):
                    rejected += 1
            if outcome in ("refused", "error"):
                failed.add(txn_id)
            new_refs += returned
            actions.append(LookupAction(action_id, txn_id, key, value, tier, reason, sources, state, outcome,
                                        tuple(returned), rejected))
        before = {c.check_id: c.status for s in states.values() for c in s.checks}
        links_before = {txn_id: s.link["state"] for txn_id, s in states.items()}
        states = evaluate()
        after = {c.check_id: c.status for s in states.values() for c in s.checks}
        changes = tuple((key, before.get(key), status) for key, status in after.items() if before.get(key) != status)
        trace.append(Transition(len(trace), tuple(step_ids), tuple(new_refs), changes,
                                tuple((txn_id, links_before[txn_id], s.link["state"]) for txn_id, s in states.items()
                                      if links_before[txn_id] != s.link["state"])))
        if calls >= budget and any(action.state == "skipped_budget" for action in actions[-len(step_ids):]):
            break   # budget spent: stop proposing further tiers

    pool, collisions = index.pool()
    evidence = {"rfc": rfc, "bank": {key: row for key, row in index.bank.items() if key in txn_ids},
                "merchant": pool, "holder_claim": holder if holder.get("txn_id") in txn_ids else None}
    draft = _rules(evidence)
    revisions = {**{f"bank:{key}": _bank_ref(row) for key, row in index.bank.items()},
                 **{_ref(row).source_id: _ref(row) for row in pool.values()}}
    holds = {txn_id: tuple(reason for reason, on in (("identity_check_failed", s.identity_failure),
                                                     ("retrieval_incomplete", txn_id in failed)) if on)
             for txn_id, s in states.items()}
    answers = []
    for question in rfc["question_ids"]:
        row = draft["answers"][question]
        reasons = tuple(dict.fromkeys(reason for txn_id in txn_ids for reason in holds[txn_id]))
        if row["status"] == "supported" and any(ref["source_id"].startswith("import:") for ref in row["evidence_refs"]):
            # A holder's own export can explain a credit for review, but never supports it.
            reasons += ("self_supplied_evidence",)
        if reasons and row["status"] == "supported":
            answers.append(Answer(question, "unresolved", row["gap"] or "settlement_not_found", "{}", (), reasons))
            continue
        refs = tuple(revisions.get(ref["source_id"], SourceRef(ref["source_id"], ref["revision"], "unknown"))
                     for ref in row["evidence_refs"])
        answers.append(Answer(question, row["status"], row["gap"], _canonical(row["facts"]), refs, reasons))
    credits = tuple(CreditReport(
        txn_id, s.link["state"], tuple(s.link.get("payout_record_ids", ())), tuple(s.checks), s.residual,
        tuple(dict.fromkeys(c.name for c in s.checks if c.status != PASS)), txn_id not in failed,
        _diagnose(s, pool, index),
    ) for txn_id, s in states.items())
    dependencies = tuple(sorted({ref for answer in answers for ref in answer.evidence_refs},
                                key=lambda ref: (ref.source_id, ref.revision)))
    return EvidenceReport(
        rfc["request_id"], rfc.get("audience"), subject, decision_time,
        "txn_ids" if txn_ids else "period", credits, tuple(answers), tuple(actions), tuple(trace),
        dependencies, index.exclusions(collisions), budget, calls, cache.hits - hits_before,
        index.observed_inputs, digest,
    )


def _call(find_evidence: Callable, key: str, value: str) -> tuple[str, list[dict]]:
    try:
        events = find_evidence(**{key: value})
    except PermissionError:
        return "refused", []
    except Exception:  # noqa: BLE001 - only the typed outcome is kept, never the payload
        return "error", []
    if not isinstance(events, list):
        return "error", []
    return ("ok" if events else "not_found"), [event for event in events if isinstance(event, dict)]


def evidence_report(case: dict, request_id: str, *, budget: int = DEFAULT_BUDGET,
                    cache: LookupCache | None = None, source_version: str | None = None) -> EvidenceReport:
    """Prepare one request's point-in-time view from a case without gold and run the engine."""
    number = case.get("case_number")
    if "gold" in case:
        raise ValueError("the evidence engine takes a case without gold")
    boundary = lockbox_from()
    if type(number) is int and type(boundary) is int and number >= boundary and not is_open():
        raise PermissionError("sealed cases require an authorized lockbox run")
    view, find_evidence = request_context(case, request_id)
    return run_engine(view, find_evidence, budget=budget, cache=cache, source_version=source_version)


def diff_reports(before: EvidenceReport, after: EvidenceReport) -> dict:
    """What changed between two reports for the same request: checks, answers and dependencies."""
    if (before.request_id, before.subject_ref) != (after.request_id, after.subject_ref):
        raise ValueError("reports describe different requests")
    old, new = before.checks(), after.checks()
    deps_old = {ref.source_id: ref.revision for ref in before.dependencies}
    deps_new = {ref.source_id: ref.revision for ref in after.dependencies}
    return {
        "checks": {key: [old.get(key), new.get(key)] for key in sorted(old.keys() | new.keys())
                   if old.get(key) != new.get(key)},
        "answers": {a.question_id: [a.status, b.status] for a, b in zip(before.answers, after.answers)
                    if (a.status, a.gap) != (b.status, b.gap)},
        "dependencies": {key: [deps_old.get(key), deps_new.get(key)] for key in sorted(deps_old.keys() | deps_new.keys())
                         if deps_old.get(key) != deps_new.get(key)},
        "visible_inputs_changed": before.visible_digest != after.visible_digest,
    }


__all__ = ["Answer", "Check", "CreditReport", "DEFAULT_BUDGET", "EvidenceReport", "LookupAction", "LookupCache",
           "Residual", "SourceRef", "Transition", "diff_reports", "evidence_report", "run_engine"]
