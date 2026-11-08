"""Execute a single case and system in a separate, gold-blind process."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
MODES = ("agents", "one_pass", "rules")
CASE_FIELDS = frozenset({"case_number", "subject_ref", "merchant_deliveries", "merchant_lookup", "imports",
                         "bank_deliveries", "rfcs", "holder_claim", "privacy_policy"})
_BOOTSTRAP = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('agents.eval.sealed_worker',run_name='__main__')"


def _contains_gold(value) -> bool:
    if isinstance(value, dict):
        return any(key.casefold() == "gold" or _contains_gold(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_gold(item) for item in value)
    return False


def case_payload(case: dict) -> dict:
    """Expose authored case fields without evaluator labels, answers or other metadata."""
    payload = {key: value for key, value in case.items() if key in CASE_FIELDS}
    if not (CASE_FIELDS - {"privacy_policy"}) <= payload.keys() or _contains_gold(payload):
        raise ValueError("adapter input lacks case fields or contains nested gold")
    return payload


def _isolation(paths: tuple[Path, ...]) -> list[str]:
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        raise RuntimeError("a sealed worker needs the verified macOS sandbox")
    profile = "(version 1)(allow default)" + "".join(
        f"(deny file-read* file-write* (subpath {json.dumps(str(path.resolve()), ensure_ascii=False)}))"
        for path in paths
    )
    return [sandbox, "-p", profile]


def verify_file_isolation(paths: tuple[Path, ...], gold: Path) -> None:
    """Open a descriptor only if the sandbox failed. Never read the gold contents."""
    if not gold.is_file():
        raise FileNotFoundError("gold is required to test the isolation boundary")
    check = """import sys
try:
    with open(sys.argv[1], 'rb'):
        pass
except PermissionError:
    sys.exit(0)
sys.exit(1)
"""
    result = subprocess.run([*_isolation(paths), sys.executable, "-c", check, str(gold.resolve())],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise RuntimeError("the adapter sandbox could not prove the gold file was denied")


def failure_rows(case: dict, mode: str, failure: str, detail: str) -> list[dict]:
    return [{"case_number": case["case_number"], "request_id": rfc["request_id"], "system": mode,
             "claims": {}, "package": None, "failure": {"type": failure, "detail": detail}}
            for rfc in case["rfcs"]]


def _complete_rows(result, case: dict, mode: str) -> list[dict]:
    if isinstance(result, dict):
        result = result.get("predictions", [result])
    expected = {rfc["request_id"] for rfc in case["rfcs"]}
    rows = {}
    if not isinstance(result, list):
        raise ValueError("adapter must return prediction objects")
    for row in result:
        request_id = row.get("request_id") if isinstance(row, dict) else None
        if (not isinstance(request_id, str) or request_id not in expected or request_id in rows
                or type(row.get("case_number")) is not int or row["case_number"] != case["case_number"]):
            raise ValueError("adapter emitted a duplicate or mismatched case/request ID")
        rows[request_id] = row
    return [rows.get(row["request_id"], row) for row in
            failure_rows(case, mode, "invalid_output", "adapter omitted this request")]


def run_case_worker(case: dict, mode: str, *, dry_run: bool, timeout: float,
                    blocked: tuple[Path, ...] = ()) -> list[dict]:
    if mode not in MODES:
        raise ValueError("unknown adapter mode")
    if not dry_run and not blocked:
        raise ValueError("a live adapter needs explicit denied paths")
    prefix = _isolation(blocked) if not dry_run or blocked else []
    payload = json.dumps(case_payload(case), allow_nan=False)
    names = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
    if not dry_run:
        names += ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ALIBI_LLM_CAP", "LOCKBOX_OPEN")
    env = {name: os.environ[name] for name in names if name in os.environ}
    with TemporaryDirectory(prefix="alibi-adapter-") as work:
        env.update(ALIBI_DATA_DIR=work, TMPDIR=work, PYTHONDONTWRITEBYTECODE="1")
        command = [*prefix, sys.executable, "-c", _BOOTSTRAP, str(REPO), mode, "dry" if dry_run else "live"]
        with subprocess.Popen(command, cwd=work, env=env, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
            try:
                stdout, _ = child.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate()
                return failure_rows(case, mode, "timeout", "case adapter exceeded its time limit")
            if child.returncode:
                return failure_rows(case, mode, "error", "case adapter exited before returning predictions")
    try:
        return _complete_rows(json.loads(stdout), case, mode)
    except ValueError:
        return failure_rows(case, mode, "invalid_output", "case adapter emitted invalid or mismatched predictions")


def _dry_predictions(case: dict, mode: str) -> list[dict]:
    from agents.eval.baselines import predict_baseline, propose_baseline
    from alibi.llm.client import CallBudget, RawResponse

    if mode == "agents":
        return failure_rows(case, mode, "unavailable", "public rehearsal does not call the app")
    if mode == "rules":
        return predict_baseline(case, "rules")

    drafts = {rfc["request_id"]: propose_baseline(case, rfc["request_id"], "rules")["pre_policy_proposal"]
              for rfc in case["rfcs"]}

    def offline_transport(_model, _system, user, _schema, _thinking):
        request_id = json.loads(user)["rfc"]["request_id"]
        return RawResponse(json.dumps(drafts[request_id]))

    return predict_baseline(case, "one_pass", transport=offline_transport, budget=CallBudget(len(drafts)))


def main() -> None:
    mode, execution = sys.argv[1:]
    case = case_payload(json.load(sys.stdin))
    with redirect_stdout(sys.stderr):
        if execution == "dry":
            rows = _dry_predictions(case, mode)
        else:
            from alibi.adapter import run_case

            rows = run_case(case, mode=mode)
    print(json.dumps(rows, allow_nan=False))


if __name__ == "__main__":
    main()
