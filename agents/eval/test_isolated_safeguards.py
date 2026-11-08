"""Real sandbox/process regressions using synthetic, locally archived adapters only."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import time

import pytest

from agents.eval import isolated_candidate as isolated

pytestmark = pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="requires macOS sandbox")


@pytest.fixture
def harness(tmp_path):
    repo = tmp_path / "synthetic-repo"
    (repo / "alibi").mkdir(parents=True)
    (repo / "alibi/__init__.py").write_text("")
    (repo / '.env').write_text('synthetic unrelated credential; must also be denied')
    private = tmp_path / "synthetic-private"
    private.mkdir()
    (private / "sentinel").write_text("synthetic only")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    credential = credentials / "key"
    credential.write_text("synthetic credential; never sent")
    capture = tmp_path / "captures" / "new"
    capture.mkdir(parents=True)
    old_capture = capture.parent / 'old'
    old_capture.mkdir()
    (old_capture / 'sentinel').write_text('synthetic prior predictions')
    lane = tmp_path / "lane-one"
    lane.mkdir()
    sibling = tmp_path / "lane-two"
    sibling.mkdir()
    (sibling / "sentinel").write_text("synthetic sibling case")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    case = dict(case_number=1, subject_ref="synthetic", merchant_deliveries=[],
                merchant_lookup={}, imports=[], bank_deliveries=[],
                rfcs=[{"request_id": "synthetic-request"}], holder_claim={}, privacy_policy=[])

    def run(body, *, mode="agents", deadline=None):
        adapter = "import json,os,pathlib,sys,subprocess,time\n\ndef run_case(case,mode):\n"
        adapter += textwrap.indent(textwrap.dedent(body), "    ")
        adapter += '''\n    return [{"case_number": case["case_number"], "request_id": "synthetic-request",
             "system": mode, "claims": {"source": __file__, "cap": os.getenv("ALIBI_LLM_CAP"),
             "key": os.getenv("GEMINI_API_KEY"), "replay": os.getenv("ALIBI_REPLAY"),
             "pythonpath": os.getenv("PYTHONPATH")}, "package": None,
             "cost": {"cost_usd_estimate": 0, "tokens": 12}}]\n'''
        (repo / "alibi/adapter.py").write_text(adapter)
        subprocess.run(["git", "add", "alibi"], cwd=repo, check=True)
        # A tree is sufficient for git archive; no commit or network operation is needed.
        candidate = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
        rows, metadata = isolated.run_isolated(
            case, mode, candidate=candidate, private_root=private, credential=credential,
            output=capture / "case", source_repo=repo, scratch_parent=lane,
            denied_scratch=(sibling,), capture_root=capture.parent, deadline=deadline)
        return rows, metadata, capture / "case"

    return run, dict(repo=repo, private=private, credential=credential, capture=capture,
                     lane=lane, sibling=sibling, witness=tmp_path / "witness")


def test_real_worker_denies_sibling_and_retains_only_its_own_source(harness, monkeypatch):
    run, paths = harness
    for key in ("ALIBI_LLM_CAP", "GEMINI_API_KEY", "ALIBI_REPLAY", "PYTHONPATH"):
        monkeypatch.setenv(key, "synthetic-inherited-value")
    forbidden_reads = [paths["sibling"] / "sentinel", paths["private"] / "sentinel", paths["credential"],
                       paths['repo'] / '.env', paths['capture'].parent / 'old' / 'sentinel']
    forbidden_writes = [paths["sibling"] / "new", paths["capture"] / "new", paths["repo"] / "new"]
    rows, metadata, output = run(f'''
for name in {list(map(str, forbidden_reads))!r}:
    try:
        pathlib.Path(name).read_text()
    except PermissionError:
        pass
    else:
        raise RuntimeError("protected read succeeded")
for name in {list(map(str, forbidden_writes))!r}:
    try:
        pathlib.Path(name).write_text("forbidden")
    except PermissionError:
        pass
    else:
        raise RuntimeError("protected write succeeded")
pathlib.Path(os.environ["ALIBI_DATA_DIR"], "own-write").write_text("allowed")
''', mode="rules")
    assert metadata["failures"] == []
    assert metadata["isolation_preflight"] == "passed"
    claims = rows[0]["claims"]
    assert all(claims[key] is None for key in ("cap", "key", "replay", "pythonpath"))
    assert not Path(claims["source"]).exists()
    assert list(paths["lane"].iterdir()) == []
    assert all(not path.exists() for path in forbidden_writes)
    assert json.loads((output / "raw.json").read_text())[0]["claims"] == claims


def test_malformed_usage_preserves_predictions_and_unknown_cost(harness):
    run, _ = harness
    rows, metadata, output = run('''
pathlib.Path(os.environ["ALIBI_DATA_DIR"], "llm_log.jsonl").write_text('{"unfinished":')
''')
    assert metadata["failures"] == []
    assert (output / "llm_log.jsonl").read_text() == '{"unfinished":'
    assert json.loads((output / "raw.json").read_text())[0]["cost"]["cost_usd_estimate"] == 0
    assert "cost_usd_estimate" not in rows[0]["cost"]
    usage = json.loads((output / "usage.json").read_text())
    assert "usage_error" in usage
    assert usage["totals"]["cost_usd_estimate"] is None
    assert usage["totals"]["tokens"] is None


@pytest.mark.parametrize("attempts,error,expected_retry", [(1, "status 429", False), (2, "status 503", True)])
def test_retry_signal_and_explicit_rate_limit_survive_capture(harness, monkeypatch, attempts, error, expected_retry):
    run, _ = harness
    monkeypatch.setenv("ALIBI_LLM_CAP", "1")
    rows, metadata, output = run(f'''
pathlib.Path(os.environ["ALIBI_DATA_DIR"], "llm_log.jsonl").write_text(json.dumps({{"attempts": {attempts}}}) + "\\n")
replay = pathlib.Path(__file__).parent / "fixtures/replays/case1_agents.json"
replay.parent.mkdir(parents=True)
replay.write_text(json.dumps({{"calls": [{{"error": "Unavailable", "message": {error!r}}}]}}))
''')
    assert metadata["rate_limited"] is True
    assert metadata["retry_based_throttle"] is expected_retry
    assert rows[0]["claims"]["cap"] is None
    assert (output / "replay.json").is_file()


def test_deadline_kills_process_group_and_retains_partial_evidence(harness):
    run, paths = harness
    witness = paths["witness"]
    descendant = f"import time,pathlib; time.sleep(3); pathlib.Path({str(witness)!r}).write_text('escaped')"
    started = time.monotonic()
    rows, metadata, output = run(f'''
pathlib.Path(os.environ["ALIBI_DATA_DIR"], "llm_log.jsonl").write_text('{{"partial":')
subprocess.Popen([sys.executable, "-c", {descendant!r}])
time.sleep(60)
''', deadline=datetime.now(timezone.utc) + timedelta(seconds=1.5))
    assert time.monotonic() - started < 4
    assert 0 < metadata["effective_timeout_seconds"] <= 1.5
    assert rows[0]["failure"]["type"] == "timeout"
    assert json.loads((output / "raw.json").read_text())[0]["failure"]["type"] == "timeout"
    assert (output / "llm_log.jsonl").read_text() == '{"partial":'
    assert "usage_error" in json.loads((output / "usage.json").read_text())
    assert list(paths["lane"].iterdir()) == []
    # This marker is outside the removed scratch tree: deletion cannot hide a leaked child.
    time.sleep(3.1)
    assert not witness.exists()


def test_expired_deadline_never_launches_candidate(harness, monkeypatch):
    run, paths = harness
    original_popen = isolated.subprocess.Popen
    candidate_launches = []

    def record_launch(command, *args, **kwargs):
        if isolated.BOOTSTRAP in command:
            candidate_launches.append(command)
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(isolated.subprocess, "Popen", record_launch)
    with pytest.raises(TimeoutError, match="deadline reached"):
        run("pass\n", deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert candidate_launches == []
    assert list(paths["lane"].iterdir()) == []
    assert not (paths["capture"] / "case/raw.json").exists()


def test_candidate_stderr_is_retained_privately_on_failure(harness):
    run, _ = harness
    rows, metadata, output = run('''
print("synthetic diagnostic", file=sys.stderr)
raise RuntimeError("synthetic adapter failure")
''', mode="rules")
    assert rows[0]["failure"]["type"] == "error"
    assert metadata["failures"] == ["error"]
    stderr = output / "worker.stderr.log"
    assert stderr.stat().st_mode & 0o777 == 0o600
    assert "synthetic diagnostic" in stderr.read_text()
    assert "RuntimeError: synthetic adapter failure" in stderr.read_text()
    assert (output / "raw.json").is_file()
    assert (output / "usage.json").is_file()
