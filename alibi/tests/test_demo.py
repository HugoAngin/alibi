"""The one-process demo runner: arguments, reset semantics, the environment the app is given,
the URL line without secrets, the bank's HTTP RFC forwarder, and one real-socket smoke run on
ports 8190-8192 (skipped when a port is busy). Nothing here touches ``data/alibi/``."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from alibi import demo, demo_services
from alibi.connectors.http import TOKEN_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE_PORTS = (8190, 8191, 8192)
FAKE_TOKENS = {"bank_a": "tokA-7f3b9c1d2e4a5b6c", "bank_b": "tokB-9a8b7c6d5e4f3a2b",
               "feed": "tokF-1a2b3c4d5e6f7a8b", "admin": "tokADM-0f9e8d7c6b5a4f3e"}


def _runner_args(tmp_path, *extra):
    return demo.parse_args(["--root", str(tmp_path / "demo"), "--keys", str(tmp_path / "keys"),
                            "--tokens", str(tmp_path / "tokens.json"), *extra])


# --------------------------------------------------------------------------- arguments


def test_parse_args_defaults_pin_the_demo_ports_and_clock():
    args = demo.parse_args([])
    assert args.command is None
    assert (args.case, args.as_of, args.timeout) == (1, "2026-10-25T00:00:00Z", None)
    assert (args.app_port, args.merchant_port, args.bank_port) == (8100, 8101, 8102)
    assert (args.reset, args.replay) == (False, False)
    assert args.host == "127.0.0.1"
    assert Path(args.root) == Path("data/alibi/demo")
    assert args.keys.endswith("keys") and args.tokens.endswith("bank_tokens.json")


def test_runner_flags_parse():
    args = demo.parse_args(["--timeout", "600", "--case", "2", "--reset", "--as-of",
                            "2026-10-28T07:30:00Z", "--replay"])
    assert args.command is None
    assert (args.timeout, args.case, args.reset, args.replay) == (600.0, 2, True, True)
    assert args.as_of == "2026-10-28T07:30:00Z"


def test_driver_subcommands_parse():
    advance = demo.parse_args(["advance", "--to", "2026-10-26T18:05:00Z"])
    assert advance.command == "advance" and advance.to == "2026-10-26T18:05:00Z"
    assert advance.app_url == "http://127.0.0.1:8100"
    assert advance.merchant_url == "http://127.0.0.1:8101" and advance.bank_url == "http://127.0.0.1:8102"
    rfc = demo.parse_args(["rfc", "bank_a", "--request-id", "rfc-0001-01"])
    assert (rfc.command, rfc.recipient, rfc.request_id) == ("rfc", "bank_a", "rfc-0001-01")
    assert demo.parse_args(["rfc", "bank_b"]).request_id is None
    assert demo.parse_args(["status"]).command == "status"
    with pytest.raises(SystemExit):
        demo.parse_args(["rfc", "bank_c"])
    with pytest.raises(SystemExit):
        demo.parse_args(["advance"])          # --to is required


@pytest.mark.parametrize("value", ["yesterday", "2026-10-25T00:00:00", "2026-10-25", ""])
def test_bad_as_of_is_refused_before_anything_is_created(tmp_path, value):
    with pytest.raises(SystemExit) as info:
        demo.serve(_runner_args(tmp_path, "--as-of", value, "--reset"))
    assert "invalid --as-of" in str(info.value) and "invalid_time" in str(info.value)
    assert not (tmp_path / "demo").exists() and not (tmp_path / "tokens.json").exists()
    assert not (tmp_path / "keys").exists()


# --------------------------------------------------------------------------- reset


def test_reset_removes_the_case_dir_and_the_bank_state_and_says_so(tmp_path):
    root = tmp_path / "demo"
    folder = demo.case_dir(root, 1)
    folder.mkdir(parents=True)
    (folder / "case_1.db").write_text("x")
    (folder / "case_1.state.json").write_text("{}")
    state = demo.bank_state_path(root, 1)
    assert state.parent == folder
    state.write_text("{}")
    other = demo.case_dir(root, 2)
    other.mkdir()
    (other / "case_2.db").write_text("y")

    result = demo.reset(root, 1)
    assert result["reset"] is True
    assert result["removed"] == [str(state), str(folder)]
    assert not folder.exists()
    assert (other / "case_2.db").exists()            # another case's state is untouched
    assert demo.reset(root, 1) == {"reset": True, "removed": []}


# --------------------------------------------------------------------------- resume


def test_resume_clock_uses_the_kept_bank_clock_when_later(tmp_path):
    root = tmp_path / "demo"
    as_of = "2026-10-25T00:00:00Z"
    assert demo.resume_clock(root, 1, as_of) == (as_of, None)          # fresh start
    state = demo.bank_state_path(root, 1)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"version": "alibi.bank_state.v1", "case_number": 1,
                                 "as_of": "2026-11-02T09:00:00Z"}))
    assert demo.resume_clock(root, 1, as_of) == ("2026-11-02T09:00:00Z", "2026-11-02T09:00:00Z")
    # A later --as-of wins; the kept clock is still reported.
    assert demo.resume_clock(root, 1, "2026-11-04T14:00:00Z") == ("2026-11-04T14:00:00Z", "2026-11-02T09:00:00Z")
    # Another case's state, or a damaged file, means a fresh start.
    assert demo.resume_clock(root, 2, as_of) == (as_of, None)
    state.write_text("{not json")
    assert demo.resume_clock(root, 1, as_of) == (as_of, None)


# --------------------------------------------------------------------------- app environment


def test_app_environment_names_the_services_and_carries_no_token(tmp_path):
    args = _runner_args(tmp_path, "--case", "1")
    env = demo.app_environment(args)
    assert env["ALIBI_MERCHANT_URL"] == "http://127.0.0.1:8101"
    assert env["ALIBI_BANK_URL"] == "http://127.0.0.1:8102"
    assert env["ALIBI_API_ORIGIN"] == "http://127.0.0.1:8100"
    assert env["ALIBI_CASE"] == "1"
    assert Path(env["ALIBI_DATA_DIR"]) == tmp_path / "demo" / "case_1"
    assert Path(env["ALIBI_KEY_DIR"]) == tmp_path / "keys"
    assert Path(env["ALIBI_TOKENS_FILE"]) == tmp_path / "tokens.json"
    assert "ALIBI_REPLAY" not in env
    assert not (set(env) & set(TOKEN_ENV.values()))     # credentials come from the file, not the env
    assert set(env) <= set(demo.APP_ENV_PUBLIC)


def test_app_environment_follows_ports_case_and_replay(tmp_path):
    args = _runner_args(tmp_path, "--case", "2", "--replay", "--app-port", "8190",
                        "--merchant-port", "8191", "--bank-port", "8192")
    env = demo.app_environment(args)
    assert env["ALIBI_REPLAY"] == "1" and env["ALIBI_CASE"] == "2"
    assert env["ALIBI_API_ORIGIN"] == "http://127.0.0.1:8190"
    assert env["ALIBI_MERCHANT_URL"] == "http://127.0.0.1:8191"
    assert env["ALIBI_BANK_URL"] == "http://127.0.0.1:8192"
    assert Path(env["ALIBI_DATA_DIR"]).name == "case_2"


# --------------------------------------------------------------------------- the URL line


def test_announce_names_credentials_but_never_their_values(tmp_path):
    args = _runner_args(tmp_path, "--replay", "--timeout", "600")
    args.as_of = "2026-10-25T00:00:00Z"
    seeds = {"merchant": {"ok": True, "pushed": [{"outcome": "inserted"}] * 19,
                          "counts": {"inserted": 19}},
             "bank": {"ok": True, "as_of": "2026-10-25T00:00:00Z", "recipients": ["bank_a", "bank_b"]},
             "feed": {"ok": True, "pages": 1, "inserted": 3}}
    line = demo.announce(args, {"app": 8100, "merchant": 8101, "bank": 8102}, seeds,
                         {"reset": True, "removed": []}, str(tmp_path / "tokens.json"), "connectors",
                         "2026-10-26T18:05:00Z")
    text = json.dumps(line)
    assert line["app"] == "http://127.0.0.1:8100" and line["holder"] == "http://127.0.0.1:8100/passport"
    assert line["merchant"] == "http://127.0.0.1:8101" and line["bank"] == "http://127.0.0.1:8102"
    assert line["consoles"]["bank_a"] == {"url": "http://127.0.0.1:8100/bank/bank_a/inbox",
                                          "token": "ALIBI_BANK_A_TOKEN or tokens file entry bank_a"}
    assert line["consoles"]["bank_b"]["token"] == "ALIBI_BANK_B_TOKEN or tokens file entry bank_b"
    assert line["tokens_file"] == str(tmp_path / "tokens.json")
    assert (line["case"], line["as_of"], line["replay"], line["timeout"]) == (1, "2026-10-25T00:00:00Z", True, 600.0)
    assert line["seeds"]["merchant_pushed"] == 19 and line["seeds"]["feed"]["inserted"] == 3
    assert line["passport_seed"] == "connectors" and line["resumed_from"] == "2026-10-26T18:05:00Z"
    assert line["reset"] == {"reset": True, "removed": []}
    for value in FAKE_TOKENS.values():
        assert value not in text
    # A failed seed is reported as data, not hidden.
    failed = demo.announce(args, {"app": 8100, "merchant": 8101, "bank": 8102},
                           {"merchant": {"ok": False, "status": 401, "code": "unauthenticated"},
                            "bank": {"ok": False, "status": None, "error": "unreachable (ConnectError)"},
                            "feed": {"ok": False, "status": 404, "code": "not_found"}},
                           None, "t.json")
    assert failed["seeds"]["merchant_pushed"] is None
    assert failed["seeds"]["merchant_outcomes"]["code"] == "unauthenticated"
    assert failed["seeds"]["feed"]["status"] == 404 and failed["reset"] == {"reset": False}
    assert failed["resumed_from"] is None


# --------------------------------------------------------------------------- RFC forwarder


def test_build_apps_wires_the_rfc_forwarder_when_asked(tmp_path):
    args = demo_services.parse_args([
        "--keys", str(tmp_path / "keys"), "--state", str(tmp_path / "bank" / "state.json"),
        "--tokens", str(tmp_path / "tokens.json"), "--no-seed",
        "--passport-url", "http://127.0.0.1:8190/connect/merchant/webhook",
        "--passport-rfc-url", "http://127.0.0.1:8190/connect/bank/rfc"])
    built = demo_services.build_apps(args)
    assert built["seeds"] == {}
    bank = built["bank"].state.bank
    assert bank.rfc_url == "http://127.0.0.1:8190/connect/bank/rfc" and bank.rfc_sink is not None
    plain = demo_services.build_apps(demo_services.parse_args([
        "--keys", str(tmp_path / "keys"), "--state", str(tmp_path / "bank" / "state2.json"),
        "--tokens", str(tmp_path / "tokens.json"), "--no-seed"]))
    assert plain["bank"].state.bank.rfc_sink is None


# --------------------------------------------------------------------------- real sockets


def _port_free(port: int) -> bool:
    with socket.socket() as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def test_smoke_three_servers_answer_and_the_process_exits_zero(tmp_path):
    """The real thing on ports 8190-8192 as a subprocess: ``--timeout 8`` after the URL line, the
    app's /health and both identities answer, no token value is printed, exit 0. Skipped when a
    port is busy, so a running demo never fails the suite."""
    busy = [p for p in SMOKE_PORTS if not _port_free(p)]
    if busy:
        pytest.skip(f"ports in use: {busy}")
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "ALIBI_REPLAY")}
    env["PYTHONUTF8"] = "1"
    cmd = [sys.executable, "-m", "alibi.demo", "--timeout", "8", "--case", "1", "--reset",
           "--app-port", "8190", "--merchant-port", "8191", "--bank-port", "8192",
           "--root", str(tmp_path / "demo"), "--keys", str(tmp_path / "keys"),
           "--tokens", str(tmp_path / "tokens.json")]
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    lines: list[str] = []

    def pump():
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()
    try:
        deadline = time.monotonic() + 90
        while not any('"holder"' in line for line in lines):
            if proc.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("the runner printed no URL line:\n" + "\n".join(lines))
            time.sleep(0.1)
        url_line = json.loads(next(line for line in lines if '"holder"' in line))
        assert url_line["app"] == "http://127.0.0.1:8190"
        assert url_line["merchant"] == "http://127.0.0.1:8191" and url_line["bank"] == "http://127.0.0.1:8192"
        assert url_line["reset"]["reset"] is True and url_line["case"] == 1
        with httpx.Client(timeout=10) as client:
            health = client.get("http://127.0.0.1:8190/health")
            assert health.status_code == 200
            assert health.json()["case"] == 1 and health.json()["bank_url"] == "http://127.0.0.1:8192"
            merchant = client.get("http://127.0.0.1:8191/")
            assert merchant.status_code == 200 and merchant.json()["service"] == "mock-marketplace"
            assert merchant.json()["as_of"] == "2026-10-25T00:00:00Z"
            bank = client.get("http://127.0.0.1:8192/")
            assert bank.status_code == 200 and bank.json()["recipients"] == ["bank_a", "bank_b"]
            assert bank.json()["as_of"] == "2026-10-25T00:00:00Z"
            # The unauthenticated admin route is still closed over real sockets.
            assert client.post("http://127.0.0.1:8191/admin/seed", params={"case": 1}).status_code == 401
        code = proc.wait(timeout=40)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    time.sleep(0.2)
    output = "\n".join(lines)
    assert code == 0, output
    assert json.loads(lines[-1]) == {"stopped": True, "failed": []}, output
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert set(tokens) == {"bank_a", "bank_b", "feed", "admin"}
    assert not any(value in output for value in tokens.values()), "a token value was printed"
    assert all(_port_free(p) for p in SMOKE_PORTS)
