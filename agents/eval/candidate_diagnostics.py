"""Classify a child failure using fixed codes, never its data-bearing message."""

from pathlib import Path
import re


EXCEPTIONS = frozenset({"IngestError", "MappingError", "ValidationError", "ValueError",
                        "TypeError", "KeyError", "PermissionError", "RuntimeError"})


def classify_failure(stderr: str, *, failure: str, returncode: int | None) -> dict:
    result = {"child_returncode": returncode, "stage": "candidate_runtime", "exception": "unknown"}
    if failure == "timeout":
        return dict(result, stage="deadline", code="worker_timeout")
    if failure == "invalid_output":
        return dict(result, stage="prediction_validation", code="invalid_predictions")
    found = re.findall(r"^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*):", stderr, re.M)
    if found and found[-1] in EXCEPTIONS:
        result["exception"] = found[-1]
    frames = re.findall(r'File "[^"\n]*/(alibi/[^"\n]+\.py)", line \d+, in (\w+)', stderr)
    if result["exception"] == "IngestError" and frames:
        if frames[-1] == ("alibi/connectors/self_supplied.py", "import_record"):
            return dict(result, stage="import_mapping", code="self_supplied_import_refused")
        if frames[-1] == ("alibi/core/ingest.py", "import_self_supplied"):
            return dict(result, stage="import_ingest", code="self_supplied_import_refused")
    return dict(result, code="candidate_process_exit")


def from_stderr(path: Path, *, failure: str, returncode: int | None) -> dict:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 65536))
        tail = handle.read(65536).decode("utf-8", errors="replace")
    return classify_failure(tail, failure=failure, returncode=returncode)
