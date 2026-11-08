"""Public, pre-freeze input contract check for holder imports, before any expensive run.

Each case's ``imports`` go one by one, in case order, through the candidate's own door,
``alibi.connectors.self_supplied.import_record``, into a throwaway in-memory ``Store`` with an
ephemeral key, at the import's own ``received_at`` and under the case's ``subject_ref``. So the
case-shaped versus core-shaped split, the case-kind mapping, the core defaults (issuer, revision,
``self_`` ids, holder subject) and ``self_supplied`` trust tagging are the candidate's, not ours.
Nothing is coerced, dropped or retried, and no model is called.

Output names stages, typed codes, positions and schema field names only. Input values, sender
keys, ids and raw exception text are never reported. The case passed in is not changed.

    uv run --frozen python -m agents.eval.input_contract            # public cases 1-80
    uv run --frozen python -m agents.eval.input_contract --case 39
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

import alibi
from alibi.connectors.case_source import IDENTITY_FIELDS, IMPORT_META, MappingError, parse_time
from alibi.connectors.self_supplied import import_record, is_case_shaped
from alibi.core.ingest import _KNOWN_FIELDS, IngestError
from alibi.core.store import SELF_SUPPLIED_PREFIX, Store

__all__ = ["validate_case_contract", "validate_public_cases", "candidate_identity"]

PUBLIC_CASES = range(1, 81)
SELF_SUPPLIED = "self_supplied"
DEFAULTED = ("subject_ref", "issuer_id", "revision", "record_id", "event_id")
EXTRA = "<extra field>"
_FIELDS = frozenset(_KNOWN_FIELDS) | IDENTITY_FIELDS | frozenset(IMPORT_META)
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}|\d{1,6}")

# Native refusal reason -> (code, fields). Patterns match the candidate's fixed wording; the
# reason itself never leaves this module.
_CASE_RULES = (
    (re.compile(r"has an event_id that is not an identifier"), "case_event_id_not_identifier", ("event_id",)),
    (re.compile(r"has a kind that is not a case kind"), "case_kind_unsupported", ("kind",)),
    (re.compile(r"contract kind .* does not agree"), "case_contract_kind_mismatch", ("kind",)),
    (re.compile(r"is missing (\w+)"), "case_field_missing", None),
    (re.compile(r"does not fit the core schema: (.*)$"), "case_core_schema_invalid", None),
    (re.compile(r"(\w+) (?:must be|is not) (?:timezone-aware|an ISO-8601)"), "case_time_invalid", None),
)
_CORE_RULES = (
    (re.compile(r"failed schema validation: (.*)$"), "core_schema_invalid", None),
    (re.compile(r"names a subject other than the holder's"), "foreign_subject", ("subject_ref",)),
    (re.compile(r"^subject_ref must be an identifier"), "holder_subject_not_identifier", ("subject_ref",)),
    (re.compile(r"is not strict JSON"), "not_strict_json", ()),
    (re.compile(r"is nested too deeply"), "nested_too_deeply", ()),
    (re.compile(r"must be a JSON object"), "not_object", ()),
    (re.compile(r"cannot be canonicalised"), "not_canonical_json", ()),
    (re.compile(r"import wrapper may hold only"), "wrapper_invalid", ("event",)),
    (re.compile(r"^rejected by the passport"), "store_conflict", ()),
)
_PROBLEMS = (("Field required", "missing"), ("Extra inputs are not permitted", "not_permitted"),
             ("Input should be", "invalid_value"))


def _field(path: str) -> str:
    """A dotted location with sender-chosen parts masked, as the core does."""
    parts = path.strip().split(".")
    return ".".join(p if _PATH.fullmatch(p) and (p.isdigit() or p in _FIELDS) else EXTRA
                    for p in parts) or "value"


def _schema_fields(where: str) -> tuple[list[str], list[str]]:
    fields, problems = [], []
    for part in where.split("; "):
        loc, _, msg = part.partition(": ")
        name = _field(loc)
        kind = next((code for prefix, code in _PROBLEMS if msg.startswith(prefix)), "invalid")
        if name not in fields:
            fields.append(name)
        problems.append(f"{name}:{kind}")
    return fields, problems


def _classify(reason: str) -> dict[str, Any]:
    """Typed code and field names for a native ``IngestError`` reason, never its text."""
    case_path = reason.startswith("import refused:")
    for pattern, code, fields in (_CASE_RULES if case_path else _CORE_RULES):
        match = pattern.search(reason)
        if not match:
            continue
        out: dict[str, Any] = {"code": code}
        if fields is not None:
            out["fields"] = list(fields)
        elif code.endswith("schema_invalid"):
            out["fields"], out["problems"] = _schema_fields(match.group(1))
        else:
            out["fields"] = [_field(match.group(1))]
        return out
    return {"code": "case_mapping_refused" if case_path else "import_refused_unclassified",
            "fields": []}


def _issue(index: int, stage: str, code: str, shape: str | None = None, **extra) -> dict:
    return {"index": index, "stage": stage, "code": code, "shape": shape,
            "fields": extra.pop("fields", []), **extra}


def _shape(record: Any) -> str:
    if not isinstance(record, dict):
        return "not_object"
    return "case_shaped" if is_case_shaped(record) else "core_shaped"


def _check_import(store: Store, index: int, entry: Any, subject: str) -> tuple[dict | None, dict | None]:
    """(accepted row, issue) for one import entry; exactly one is not None."""
    if not isinstance(entry, dict):
        return None, _issue(index, "entry", "entry_not_object")
    if "record" not in entry:
        return None, _issue(index, "entry", "record_missing", fields=["record"])
    try:
        at = parse_time(entry.get("received_at"), "received_at")
    except MappingError:
        return None, _issue(index, "entry", "received_at_invalid", fields=["received_at"])
    record = copy.deepcopy(entry["record"])
    shape = _shape(record)
    try:
        put = import_record(store, record, subject_ref=subject, now=at)
    except IngestError as exc:
        return None, _issue(index, "native_import", shape=shape, **_classify(exc.reason))
    except Exception as exc:  # a crash the native door did not type: class name only
        return None, _issue(index, "native_import", "native_exception", shape,
                            exception_class=type(exc).__name__)
    stored = put.record
    trust = getattr(stored.source_trust, "value", stored.source_trust)
    if trust != SELF_SUPPLIED or not str(stored.record_id).startswith(SELF_SUPPLIED_PREFIX):
        return None, _issue(index, "trust", "not_tagged_self_supplied", shape,
                            fields=["source_trust", "record_id"])
    if stored.subject_ref != subject:
        return None, _issue(index, "trust", "stored_subject_mismatch", shape, fields=["subject_ref"])
    row = {"index": index, "shape": shape, "status": put.status, "trust": SELF_SUPPLIED}
    if shape == "core_shaped":
        row["defaulted"] = [name for name in DEFAULTED if record.get(name) is None]
    return row, None


def validate_case_contract(case: Any) -> dict[str, Any]:
    """Run one case's holder imports through the candidate's native import, without changing
    ``case``. Returns a safe aggregate: counts, accepted rows and typed issues."""
    number = case.get("case_number") if isinstance(case, dict) else None
    report: dict[str, Any] = {"case_number": number if isinstance(number, int) else None,
                              "imports": 0, "accepted": [], "issues": []}
    if not isinstance(case, dict):
        report["issues"].append(_issue(-1, "case", "case_not_object"))
    elif not isinstance(case.get("subject_ref"), str) or not case["subject_ref"]:
        report["issues"].append(_issue(-1, "case", "case_subject_missing", fields=["subject_ref"]))
    elif not isinstance(case.get("imports", []), list):
        report["issues"].append(_issue(-1, "case", "imports_not_list", fields=["imports"]))
    else:
        imports = copy.deepcopy(case.get("imports", []))
        report["imports"] = len(imports)
        with Store(":memory:", master_key=Fernet.generate_key()) as store:
            for index, entry in enumerate(imports):
                row, issue = _check_import(store, index, entry, case["subject_ref"])
                report["accepted" if row else "issues"].append(row or issue)
    report["valid"] = not report["issues"]
    return report


def candidate_identity() -> dict[str, Any]:
    """The git commit of the imported ``alibi`` package, read-only, and whether it is clean."""
    root = Path(alibi.__file__).resolve().parent
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None
    sha = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--", ".")
    return {"git_sha": sha if sha and re.fullmatch(r"[0-9a-f]{40}", sha) else None,
            "alibi_tree_clean": None if status is None else status == ""}


def validate_public_cases(numbers=PUBLIC_CASES) -> dict[str, Any]:
    """Validate visible public development cases (1-80 only) and aggregate the issues."""
    from agents.data.api import load_case, without_gold

    reports, hidden = [], []
    for number in numbers:
        if number not in PUBLIC_CASES:
            raise ValueError("only public cases 1-80 are checked")
        try:
            case = without_gold(load_case(number))
        except KeyError:
            hidden.append(number)
            continue
        reports.append(validate_case_contract(case))
    codes: dict[str, int] = {}
    for report in reports:
        for issue in report["issues"]:
            codes[issue["code"]] = codes.get(issue["code"], 0) + 1
    failing = [r for r in reports if not r["valid"]]
    return {"kind": "public input contract check; native import, no model, values omitted",
            "candidate": candidate_identity(), "validator": "alibi.connectors.self_supplied.import_record",
            "cases_checked": len(reports), "cases_not_visible": hidden,
            "imports_checked": sum(r["imports"] for r in reports),
            "imports_accepted": sum(len(r["accepted"]) for r in reports),
            "issue_codes": dict(sorted(codes.items())),
            "failing_cases": [{"case_number": r["case_number"], "issues": r["issues"]} for r in failing],
            "valid": not failing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check public holder imports against the "
                                                 "candidate's native import contract.")
    parser.add_argument("--case", type=int, action="append", choices=PUBLIC_CASES,
                        metavar="N", help="public case number 1-80 (repeatable); default all")
    args = parser.parse_args(argv)
    summary = validate_public_cases(args.case or PUBLIC_CASES)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
