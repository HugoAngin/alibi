"""The live demo end to end through the director (alibi/demo_director.py), over real sockets on
8290-8293 (skipped when a port is busy): withdraw flags the payout, Bank A's request reaches Maya,
her package is accepted by the bank (one clock: never "issued in the future"), and reset puts
every service back to the start. Nothing here touches ``data/alibi/``."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTS = (8290, 8291, 8292, 8293)


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.fixture()
def stack(tmp_path):
    busy = [p for p in PORTS if not _port_free(p)]
    if busy:
        pytest.skip(f"ports in use: {busy}")
    env = {k: v for k, v in os.environ.items()
           if k not in ("GEMINI_API_KEY", "ALIBI_REPLAY", "ALIBI_DEMO_TOKEN", "ALIBI_HOLDER_TOKEN")}
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, "-m", "alibi.demo", "--timeout", "120", "--case", "1", "--reset",
           "--app-port", "8290", "--merchant-port", "8291", "--bank-port", "8292",
           "--director-port", "8293", "--root", str(tmp_path / "demo"),
           "--keys", str(tmp_path / "keys"), "--tokens", str(tmp_path / "tokens.json")]
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 90
    line = ""
    while '"director"' not in line:
        if proc.poll() is not None or time.monotonic() >= deadline:
            proc.kill()
            pytest.fail("the runner printed no URL line")
        line = proc.stdout.readline()
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    try:
        yield tokens
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_withdraw_request_send_reset(stack):
    tokens = stack
    app, bank, director = "http://127.0.0.1:8290", "http://127.0.0.1:8292", "http://127.0.0.1:8293"
    d = {"Authorization": f"Bearer {tokens['admin']}"}
    j = {"Accept": "application/json"}
    with httpx.Client(timeout=60) as c:
        assert c.get(f"{director}/demo/state").status_code == 401
        start = c.get(f"{director}/demo/state", headers=d).json()
        assert start["stage"] == "ready"
        refused = c.post(f"{director}/demo/request", headers=d, json={})
        assert refused.status_code == 409 and refused.json()["code"] == "not_flagged_yet"

        flagged = c.post(f"{director}/demo/withdraw", headers=d).json()
        assert flagged["stage"] == "flagged" and flagged["version"] != start["version"]
        alerts = [a.get("kind") for a in c.get(f"{app}/passport", headers=j).json()["alerts"]]
        assert "unexplained_credit" in alerts

        issued = c.post(f"{director}/demo/request", headers=d, json={"recipient": "bank_a"})
        assert issued.status_code == 201 and issued.json()["forwarded"]["status"] == "accepted"

        assert c.post(f"{app}/requests/rfc-0001-01/run", headers=j).status_code == 200
        approval = c.get(f"{app}/requests/rfc-0001-01", headers=j).json()["review"]["approval"]
        form = {k: approval[k] for k in ("digest", "audience", "policy_version")}
        assert c.post(f"{app}/requests/rfc-0001-01/approve", headers=j, data=form).status_code == 200
        sent = c.post(f"{app}/requests/rfc-0001-01/dispatch", headers=j)
        assert sent.status_code == 200, sent.text
        assert sent.json()["outcome"] == "success"

        received = c.get(f"{director}/demo/state", headers=d).json()
        assert received["stage"] == "received" and received["package_id"] == sent.json()["package_id"]
        inbox = c.get(f"{bank}/inbox", headers={"Authorization": f"Bearer {tokens['bank_a']}"}).json()
        assert [p["status"] for p in inbox["packages"]] == ["verified"]
        # Reading screens never changes the version, so pages do not reload in a loop.
        assert c.get(f"{director}/demo/state", headers=d).json()["version"] == received["version"]

        reset = c.post(f"{director}/demo/reset", headers=d).json()
        assert reset["stage"] == "ready" and reset["version"] == start["version"]
        inbox = c.get(f"{bank}/inbox", headers={"Authorization": f"Bearer {tokens['bank_a']}"}).json()
        assert inbox["packages"] == [] and inbox["requests_sent"] == []
        assert c.get(f"{app}/passport", headers=j).json()["alerts"] == []
