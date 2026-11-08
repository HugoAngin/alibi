"""Reproduce the macOS coordinator-pause race with public rules cases and no model calls."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
from queue import Queue
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
import time

from agents.eval.parallel_capture import schedule
from alibi.connectors.case_source import load_case_without_gold

CANDIDATE = "58bf0d4a199f5ad3833fa9bde751d31140219975"
REPO = Path(__file__).resolve().parents[2]
SOURCE = "agents/eval/isolated_candidate.py"
TIMEOUT = 0.25


def exercise(ref: str) -> None:
    """Use the requested evaluator worker code without changing the checkout."""
    source = subprocess.check_output(["git", "show", f"{ref}:{SOURCE}"], cwd=REPO, text=True)
    scope = {"__file__": str(REPO / SOURCE), "__name__": "public_timeout_exercise"}
    exec(compile(source, SOURCE, "exec"), scope)
    scope["LIMITS"]["rules"] = TIMEOUT
    signals = Counter()
    ready_lock = Lock()
    child_ready = False
    killpg, popen = os.killpg, subprocess.Popen
    with TemporaryDirectory(prefix="alibi-public-timeout-") as temp:
        root = Path(temp).resolve()
        private, outputs = root / "mock-private", root / "capture"
        private.mkdir()
        outputs.mkdir()
        credential = private / "empty.env"
        credential.touch(mode=0o600)
        lanes = [root / f"lane-{index}" for index in range(4)]
        queue = Queue()
        for lane in lanes:
            lane.mkdir()
            queue.put(lane)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=90)

        def signal_group(pid, signum):
            try:
                killpg(pid, signum)
            except OSError as exc:
                signals[errno.errorcode.get(exc.errno, "OSERROR")] += 1
                raise
            signals["success"] += 1

        def watch(command, *args, **kwargs):
            nonlocal child_ready
            worker = isinstance(command, list) and scope["BOOTSTRAP"] in command
            if worker and kwargs.get("stderr") == subprocess.DEVNULL:
                trace = root / (Path(kwargs["cwd"]).name + ".stderr")
                with trace.open("wb") as stream:
                    child = popen(command, *args, **{**kwargs, "stderr": stream})
            else:
                child = popen(command, *args, **kwargs)
            if worker:
                with ready_lock:
                    if not child_ready:
                        child_ready = True
                        print("READY", flush=True)
            return child

        def run_job(number):
            lane = queue.get()
            try:
                return scope["run_isolated"](
                    load_case_without_gold(number), "rules", candidate=CANDIDATE,
                    private_root=private, credential=credential, output=outputs / str(number),
                    scratch_parent=lane, denied_scratch=tuple(path for path in lanes if path != lane),
                    capture_root=outputs, deadline=deadline,
                )
            finally:
                queue.put(lane)

        os.killpg, subprocess.Popen = signal_group, watch
        try:
            record = schedule(range(1, 13), run_job, workers=4, deadline=deadline, stop_on_error=True)
        finally:
            os.killpg, subprocess.Popen = killpg, popen
        print(json.dumps({
            "candidate": CANDIDATE, "evaluator_ref": ref, "public_mode": "rules", "timeout_seconds": TIMEOUT,
            "signals": dict(signals), "total_started": record["metadata"]["attempted"],
            "infrastructure": [row["error"] for row in record["outcomes"] if row["error"]],
            "worker_failures": dict(Counter(
                failure for row in record["outcomes"] if row["status"] == "completed"
                for failure in row["result"][1]["failures"]
            )),
        }), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluator-ref", required=True, help="trusted local Git revision of evaluator code")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("requires macOS and sandbox-exec")
    os.umask(0o077)
    ref = subprocess.check_output(["git", "rev-parse", "--verify", args.evaluator_ref + "^{commit}"],
                                  cwd=REPO, text=True).strip()
    if args.child:
        exercise(ref)
        return
    command = [sys.executable, "-m", "agents.eval.public_timeout_repro", "--evaluator-ref", ref, "--child"]
    with subprocess.Popen(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as child:
        try:
            ready, _, _ = select.select([child.stdout], [], [], 25)
            if not ready or child.stdout.readline().strip() != "READY":
                raise RuntimeError("public test worker did not launch")
            time.sleep(0.075)
            os.kill(child.pid, signal.SIGSTOP)
            time.sleep(1.25)
            os.kill(child.pid, signal.SIGCONT)
            stdout, _ = child.communicate(timeout=110)
            if child.returncode:
                raise RuntimeError(f"public test exited with status {child.returncode}")
            print(json.dumps(json.loads(stdout)))
        finally:
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    main()
