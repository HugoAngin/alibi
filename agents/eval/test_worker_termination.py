"""Synthetic process regressions for worker termination; no app or model calls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agents.eval import isolated_candidate as isolated


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS exited-process-group regression")
def test_paused_coordinator_retains_timeout_and_evidence(tmp_path):
    # Reuse the synthetic git-tree adapter fixture, never the app candidate.
    coordinator = r'''
import json,sys
from pathlib import Path
from agents.eval import isolated_candidate as isolated
from agents.eval.test_isolated_safeguards import harness
run,paths=harness.__wrapped__(Path(sys.argv[1]))
isolated.LIMITS['rules']=.3
original=isolated.subprocess.Popen
def watch(command,*args,**kwargs):
    child=original(command,*args,**kwargs)
    if isolated.BOOTSTRAP in command:
        print('ready',flush=True)
    return child
isolated.subprocess.Popen=watch
rows,metadata,output=run(''' + repr('''
pathlib.Path(os.environ["ALIBI_DATA_DIR"], "llm_log.jsonl").write_text('{"partial":')
print("synthetic worker stderr evidence",file=sys.stderr,flush=True)
time.sleep(.1)
''') + r''',mode='rules')
assert rows[0]['failure']['type']=='timeout'
assert json.loads((output/'raw.json').read_text())[0]['failure']['type']=='timeout'
assert (output/'llm_log.jsonl').read_text()=='{"partial":'
assert 'synthetic worker stderr evidence' in (output/'worker.stderr.log').read_text()
assert list(paths['lane'].iterdir())==[]
print(json.dumps({'failure':rows[0]['failure']['type'],'preflight':metadata['isolation_preflight']}),flush=True)
'''
    proc = subprocess.Popen([sys.executable, "-c", coordinator, str(tmp_path)],
                            cwd=isolated.REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(.04)  # let communicate enter its timed wait
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(.65)  # child exits and the original timeout elapses while parent is stopped
        os.kill(proc.pid, signal.SIGCONT)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert json.loads(stdout) == {"failure": "timeout", "preflight": "passed"}
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGCONT)
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_termination_kills_descendant_even_after_group_leader_exits(tmp_path):
    witness = tmp_path / "descendant-survived"
    descendant = f"import pathlib,time; time.sleep(1.5); pathlib.Path({str(witness)!r}).write_text('alive')"
    leader = ("import subprocess,sys; "
              f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],"
              "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(child.pid,flush=True)")
    child = subprocess.Popen([sys.executable, "-c", leader], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        assert int(child.stdout.readline()) > 0
        assert child.wait(timeout=5) == 0
        isolated._terminate_group(child)
        time.sleep(1.7)
        assert not witness.exists()
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.stdout.close()
        child.wait(timeout=5)


def test_genuine_permission_failure_for_running_child_propagates(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    original_killpg = os.killpg

    def denied(group, sig):
        assert group == child.pid and sig == signal.SIGKILL
        raise PermissionError(1, "synthetic denial")

    try:
        monkeypatch.setattr(isolated.os, "killpg", denied)
        with pytest.raises(PermissionError, match="synthetic denial"):
            isolated._terminate_group(child)
        assert child.poll() is None
    finally:
        original_killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def test_permission_failure_is_ignored_only_for_confirmed_exited_child(monkeypatch):
    class Exited:
        pid = 123456

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return 0

    child = Exited()
    attempts = []

    def exited_group(group, sig):
        attempts.append((group, sig))
        raise PermissionError(1, "synthetic exited-group macOS response")

    monkeypatch.setattr(isolated.os, "killpg", exited_group)
    isolated._terminate_group(child)
    assert attempts == [(child.pid, signal.SIGKILL)]
    assert child.polls >= 2


def test_stdout_held_by_detached_descendant_has_bounded_drain():
    # A new-session descendant survives killpg and keeps the inherited stdout open.
    descendant = "import time; time.sleep(60)"
    leader = ("import subprocess,sys,time; "
              f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}],start_new_session=True); "
              "print(child.pid,flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", leader], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    descendant_pid = None
    try:
        descendant_pid = int(child.stdout.readline())
        isolated._terminate_group(child)
        started = time.monotonic()
        isolated._drain_terminated_worker(child, timeout=.15)
        assert time.monotonic() - started < 1
        assert child.returncode == -signal.SIGKILL
        assert child.stdout.closed
        # Verify that timeout was caused by a surviving process, not an empty pipe.
        os.kill(descendant_pid, 0)
    finally:
        if descendant_pid is not None:
            os.killpg(descendant_pid, signal.SIGKILL)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        child.stdout.close()


def test_bounded_drain_does_not_hide_an_unreaped_direct_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdout=subprocess.PIPE, start_new_session=True)
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            isolated._drain_terminated_worker(child, timeout=.1)
        assert time.monotonic() - started < 1
        assert child.stdout.closed and child.poll() is None
    finally:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
