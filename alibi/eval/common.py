"""Shared helpers for the evaluation package: refs, time, canonical JSON and case views.

Nothing here imports ``alibi.core`` or ``alibi.api``. Development cases come from
``agents.data.api``; sealed cases are the same objects read from JSONL by
``load_sealed_cases`` and get their gold from a separate evaluator-only file.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agents.data import api as data_api

EVAL_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = EVAL_DIR / "schemas"
REPO_ROOT = EVAL_DIR.parent.parent

Q_SOURCE = "q.source_of_funds"
Q_BREAKDOWN = "q.breakdown"
Q_PERIOD = "q.sales_refunds_in_period"
QUESTION_IDS = (Q_SOURCE, Q_BREAKDOWN, Q_PERIOD)
CLAIM_STATUSES = ("supported", "contradicted", "unresolved")
LINK_STATES = ("unmatched", "ambiguous", "linked")
GAP_CODES = ("settlement_not_found", "balance_release_not_settlement", "settlement_after_credit",
             "settlement_pending_at_booking", "settlement_failed", "ambiguous_candidates",
             "amount_mismatch", "missing_cited_record", "no_merchant_records")
FAILURE_TYPES = ("timeout", "error", "budget_exceeded", "invalid_output", "unavailable", "abstained")
REF_KINDS = ("bank", "merchant", "import")
SOURCE_TRUST = {"bank": "bank_observed", "merchant": "issuer_verified", "import": "self_supplied"}
_REF = re.compile(r"^(bank|merchant|import):([A-Za-z0-9][A-Za-z0-9_.-]*)$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")


class EvalError(Exception):
    """A case, prediction, gold file or manifest cannot be read."""


# --------------------------------------------------------------------------- refs and time


def parse_ref(value: Any) -> tuple[str, str]:
    """``bank:<txn_id>``, ``merchant:<record_id>`` or ``import:<record_id>`` -> (kind, id)."""
    if not isinstance(value, str):
        raise EvalError(f"evidence ref must be a string such as merchant:rec-0001-021, got {value!r}")
    found = _REF.match(value.strip())
    if not found:
        raise EvalError(f"bad evidence ref {value!r}: expected bank:<txn_id>, merchant:<record_id> "
                        f"or import:<record_id>")
    return found.group(1), found.group(2)


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF.match(value.strip()))


def parse_time(text: Any) -> datetime:
    """ISO-8601 with a timezone, returned in UTC."""
    if not isinstance(text, str):
        raise EvalError(f"not a timestamp: {text!r}")
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvalError(f"not a timestamp: {text!r}") from exc
    if value.tzinfo is None:
        raise EvalError(f"timestamp {text!r} has no timezone")
    return value.astimezone(timezone.utc)


def looks_like_time(text: Any) -> bool:
    return isinstance(text, str) and bool(_TIME.match(text.strip()))


def fmt_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- JSON


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, non-ASCII kept."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"missing file {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path}: invalid JSON: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")


def read_jsonl(path: Path) -> list[Any]:
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise EvalError(f"missing file {path}") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise EvalError(f"{path}:{number}: invalid JSON: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: Iterable[Any]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def load_schema(name: str) -> dict:
    return load_json(SCHEMA_DIR / f"{name}.schema.json")


# --------------------------------------------------------------------------- case views


def requests_of(case: dict) -> list[dict]:
    return list(case.get("rfcs") or [])


def request_of(case: dict, request_id: str) -> dict:
    for rfc in requests_of(case):
        if rfc.get("request_id") == request_id:
            return rfc
    raise EvalError(f"case {case.get('case_number')}: no request {request_id!r}")


def decision_time(case: dict) -> str:
    gold = case.get("gold") or {}
    if gold.get("decision_time"):
        return gold["decision_time"]
    return requests_of(case)[0]["issued_at"]


def case_label(case: dict) -> str:
    return f"case {case.get('case_number')} ({case.get('family')}/{case.get('variant')})"


def observable(case: dict, at: str) -> dict[str, list[dict]]:
    """Everything a system could have seen by ``at``, keyed by evidence ref.

    Pushed merchant events, holder imports and bank rows received by ``at``, plus lookup-only
    records available by then (a system must earn those through ``agents.data.api.lookup``).
    Each value lists every version received, in arrival order.
    """
    view = data_api.known_at(case, at, include_lookup=True)
    out: dict[str, list[dict]] = {}
    for d in view["merchant_deliveries"]:
        out.setdefault(f"merchant:{d['event']['record_id']}", []).append(d["event"])
    for d in view["merchant_lookup"]:
        out.setdefault(f"merchant:{d['event']['record_id']}", []).append(d["event"])
    for d in view["imports"]:
        out.setdefault(f"import:{d['record']['record_id']}", []).append(d["record"])
    for d in view["bank_deliveries"]:
        out.setdefault(f"bank:{d['row']['txn_id']}", []).append(d["row"])
    return out


def active_version(versions: list[dict]) -> dict:
    """The highest revision among the versions received; ties keep the first received."""
    best = versions[0]
    for version in versions[1:]:
        if version.get("revision", 1) > best.get("revision", 1):
            best = version
    return best


def sensitive_terms(case: dict) -> dict[str, str]:
    """Lower-cased strings that identify a forbidden record, mapped to the ref they expose.

    The ref's own ID, and the counterparty name, item or description of a forbidden row or
    record when it is at least six characters long (so generic words such as CARD or RENT
    are not matched).
    """
    gold = case.get("gold") or {}
    rfcs = requests_of(case)
    seen = observable(case, rfcs[-1]["issued_at"]) if rfcs else {}
    terms: dict[str, str] = {}
    for ref in gold.get("forbidden_refs", []):
        _kind, ident = parse_ref(ref)
        terms[ident.lower()] = ref
        for version in seen.get(ref, []):
            for key in ("counterparty_name", "item", "description"):
                value = version.get(key)
                if isinstance(value, str) and len(value.strip()) >= 6:
                    terms[value.strip().lower()] = ref
    return terms


def text_blob(value: Any) -> str:
    """All strings inside a JSON value, lower-cased and joined, for leak searches."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                parts.append(str(key))
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif node is not None and not isinstance(node, bool):
            parts.append(str(node))

    walk(value)
    return "\n".join(parts).lower()


# --------------------------------------------------------------------------- loading


def load_dev_cases(families: Iterable[str] | None = None, include_demo: bool = True) -> list[dict]:
    """Development cases through ``agents.data.api.load_cases`` (the lockbox hides sealed ones)."""
    cases = data_api.load_cases()
    if families:
        wanted = set(families)
        unknown = wanted - set(data_api.FAMILIES)
        if unknown:
            raise EvalError(f"unknown families {sorted(unknown)}; expected {data_api.FAMILIES}")
        cases = [c for c in cases if c["family"] in wanted]
    if not include_demo:
        cases = [c for c in cases if c["family"] != "demo"]
    return cases


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvalError(f"{what}: case_number must be an integer, got {value!r}")
    return value


def read_gold_file(path: Path) -> dict[int, dict]:
    """Evaluator-only gold keyed by case number.

    Accepted shapes: JSONL with one ``{"case_number": n, "gold": {...}}`` per line (or the
    gold fields inline next to ``case_number``); a JSON list of the same objects; or a JSON
    object ``{"<case_number>": {...gold...}}``. The gold object has the fields described in
    ``agents/data/README.md`` and its ``answers`` are keyed by ``request_id``.
    """
    path = Path(path)
    rows: Any
    if path.suffix == ".jsonl":
        rows = read_jsonl(path)
    else:
        rows = load_json(path)
    out: dict[int, dict] = {}
    if isinstance(rows, dict) and "case_number" not in rows:
        items = []
        for key, value in rows.items():
            try:
                number = int(key)
            except ValueError as exc:
                raise EvalError(f"{path}: gold key {key!r} is not a case number") from exc
            items.append({"case_number": number, "gold": value})
        rows = items
    elif isinstance(rows, dict):
        rows = [rows]
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "case_number" not in row:
            raise EvalError(f"{path}: gold entry {index} lacks case_number")
        number = _as_int(row["case_number"], f"{path}: gold entry {index}")
        gold = row["gold"] if isinstance(row.get("gold"), dict) else {k: v for k, v in row.items()
                                                                        if k != "case_number"}
        if "answers" not in gold:
            raise EvalError(f"{path}: gold for case {number} has no answers")
        if number in out:
            raise EvalError(f"{path}: gold for case {number} appears twice")
        out[number] = gold
    return out


def check_lockbox(cases: Iterable[dict]) -> None:
    """Refuse case numbers in the lockbox unless eval opened it through tools/results.py."""
    from tools.lockbox import check

    numbers = [c["case_number"] for c in cases]
    if numbers:
        check(numbers)


def sealed_files(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise EvalError(f"no sealed cases at {path}")
    return sorted(p for p in path.glob("*.jsonl") if p.is_file())


def load_sealed_cases(path: Path, gold_path: Path | None = None, *,
                      require_gold: bool = True) -> list[dict]:
    """Sealed cases from one ``.jsonl`` file or a directory of them, gold attached.

    Each line is one case object in the team's case schema without ``gold``. Gold comes from
    the evaluator-only file (``read_gold_file``). Case numbers at or above the lockbox are
    refused unless eval opened it, so nobody scores the sealed set by accident.
    """
    cases: list[dict] = []
    seen: set[int] = set()
    for file in sealed_files(path):
        for index, row in enumerate(read_jsonl(file), 1):
            if not isinstance(row, dict) or "case_number" not in row:
                raise EvalError(f"{file}:{index}: not a case object with case_number")
            number = _as_int(row["case_number"], f"{file}:{index}")
            if number in seen:
                raise EvalError(f"{file}:{index}: case {number} appears twice")
            seen.add(number)
            for key in ("merchant_deliveries", "merchant_lookup", "imports", "bank_deliveries",
                        "rfcs", "holder_claim"):
                if key not in row:
                    raise EvalError(f"{file}:{index}: case {number} lacks {key}")
            cases.append(row)
    cases.sort(key=lambda c: c["case_number"])
    check_lockbox(cases)
    if gold_path is not None:
        gold = read_gold_file(gold_path)
        missing = [c["case_number"] for c in cases if c["case_number"] not in gold]
        if missing and require_gold:
            raise EvalError(f"no gold for cases {missing}")
        for case in cases:
            if case["case_number"] in gold:
                case["gold"] = gold[case["case_number"]]
    elif require_gold:
        missing = [c["case_number"] for c in cases if "gold" not in c]
        if missing:
            raise EvalError(f"sealed cases {missing} have no gold; pass --gold")
    return cases


def families_of(cases: Iterable[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for case in cases:
        out.setdefault(str(case.get("family")), []).append(case["case_number"])
    return out
