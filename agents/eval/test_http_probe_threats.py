"""Check threat verdicts and capture boundaries against synthetic HTTP responses."""

import json
import os
import tempfile
from base64 import b64encode
from pathlib import Path

import httpx
import pytest

from agents.eval import api, http_probe_runtime as runtime, http_probe_threats as threats
from agents.execution.api import probe_ids


@pytest.fixture
def run(monkeypatch):
    client = httpx.Client

    def attempt(probe_id, respond, *, routes, fixture):
        monkeypatch.setattr(runtime.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw,
        ))
        name = probe_id.replace("-", "").lower()
        return api.attempt(probe_id, {"app": "https://alibi.invalid", "routes": routes,
                                      "fixtures": {name: fixture}},
                           {"holder": "synthetic-holder", "agent": "synthetic-agent"})

    return attempt


@pytest.mark.parametrize("probe_id", probe_ids())
def test_every_catalogue_id_has_a_handler_and_unconfigured_attempt_is_not_run(probe_id):
    assert callable(runtime._handler_for(probe_id))
    result = api.attempt(probe_id, {}, {})
    assert result["outcome"] == "not_run"
    assert result["exchanges"] == []
    assert result.get("reason")


@pytest.mark.parametrize(("tool_outcome", "audit_rows", "outcome"), [
    ("refused", True, "blocked"), ("refused", False, "not_run"), ("allowed", True, "not_blocked"),
])
def test_injection_needs_proof_of_the_actual_tool_refusal(run, tool_outcome, audit_rows, outcome):
    call = {"target": "foreign-account", "outcome": tool_outcome, "code": "out_of_scope"}

    def respond(request):
        payload = {
            "/run": {"run_id": "one"},
            "/trace/one": {"input": "injection landed", "tool_calls": [call]},
            "/artifact/one": {"result": "legitimate evidence"},
            "/audit/one": {"entries": [call] if audit_rows else [], "example": "out_of_scope"},
        }[request.url.path]
        return httpx.Response(200, json=payload)

    result = run("SP-38", respond, routes={"agent_run": "/run", "agent_trace": "/trace/{run_id}",
                                            "agent_artifact": "/artifact/{run_id}", "agent_audit": "/audit/{run_id}"},
                 fixture={"run_body": {"receipt": "injection landed"}, "landed_marker": "injection landed",
                          "forbidden_target": "foreign-account", "forbidden": ["private-package"],
                          "allowed_marker": "legitimate evidence", "refusal_codes": ["out_of_scope"]})
    assert result["outcome"] == outcome
    assert all("response_body" not in exchange for exchange in result["exchanges"][1:])


@pytest.mark.parametrize(("payload", "outcome"), [
    ({"status": "unresolved", "reason_code": "budget_exhausted", "attempts": 2}, "blocked"),
    ({"status": "completed", "example": "budget_exhausted", "attempts": 2}, "not_blocked"),
    ({"status": "unresolved", "reason_code": "budget_exhausted", "attempts": 3}, "not_blocked"),
    ({"status": "unresolved", "example": "budget_exhausted", "attempts": 2}, "not_run"),
])
def test_budget_probe_needs_typed_reason_and_real_attempt_count(run, payload, outcome):
    def respond(request):
        return httpx.Response(200, json={"run_id": "one"} if request.method == "POST" else payload)

    result = run("SP-44", respond, routes={"agent_run": "/run", "agent_trace": "/trace/{run_id}"},
                 fixture={"run_body": {"fault": "exhaust"}, "reasons": ["budget_exhausted"], "attempt_budget": 2})
    assert result["outcome"] == outcome


def test_authority_probe_does_not_treat_one_attempt_as_four(run):
    def respond(_):
        pytest.fail("Incomplete authority probe must not make a request")

    result = run("SP-45", respond, routes={"tool_gateway": "/gateway"},
                 fixture={"attempts": {"sign": {"tool": "sign"}}, "refusal_codes": ["tool_not_allowed"]})
    assert result["outcome"] == "not_run"
    assert "four authority tool attempts" in result["reason"]


@pytest.mark.parametrize(("encoded_leak", "missing_trace", "outcome"), [
    (False, False, "blocked"), (True, False, "not_blocked"), (False, True, "not_run"),
])
def test_log_scan_checks_all_corpora_and_never_captures_their_contents(run, encoded_leak, missing_trace, outcome):
    secret = "synthetic secret + example"
    header = "synthetic private header"
    encoded = b64encode(secret.encode()).decode()

    def respond(request):
        corpus = request.url.path.strip("/")
        text = f"canary-{corpus}"
        if corpus == "logs" and encoded_leak:
            text += f" {encoded}"
        if corpus == "traces" and missing_trace:
            text = "stale trace"
        return httpx.Response(200, text=text, headers={"content-disposition": header})

    result = run("SP-46", respond, routes={key: f"/{key}" for key in ("audit", "logs", "traces")},
                 fixture={"canaries": {key: f"canary-{key}" for key in ("audit", "logs", "traces")},
                          "secret_patterns": [secret]})
    assert result["outcome"] == outcome
    for exchange in result["exchanges"]:
        assert exchange["status"] == 200
        assert "response_body" not in exchange and "content_disposition" not in exchange
    assert secret not in str(result) and encoded not in str(result) and header not in str(result)


# ----------------------------------------------------------------- SP-46 native local scan

native_only = pytest.mark.skipif(not threats._native_supported(), reason="native scan needs POSIX no-follow opens")
NATIVE_NAMES = {"audit": "audit.json", "logs": "logs.log", "traces": "traces.json"}
NATIVE_MARKERS = {label: f"sp46-{label}-current-0001" for label in NATIVE_NAMES}
SYNTHETIC_KEY = "synthetic-master-key-0000aaaa"
SYNTHETIC_TOKEN = "synthetic-bearer-bank-a-1111"
SYNTHETIC_RAW = '{"raw_delivery": "synthetic payload 0002", "amount": 216000}'
SYNTHETIC_PEM = "-----BEGIN " + "PRIVATE KEY-----\nSYNTHETIC\n-----END " + "PRIVATE KEY-----"


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def build(**texts):
        scratch = Path(tempfile.mkdtemp(prefix="alibi-sp46-"))
        for label, name in NATIVE_NAMES.items():
            (scratch / name).write_text(texts.get(label, f'{{"marker": "{NATIVE_MARKERS[label]}"}}\n'))
        return scratch, {"opt_in": threats.NATIVE_SP46_OPT_IN, "scratch_dir": str(scratch),
                         "captures": dict(NATIVE_NAMES), "canaries": dict(NATIVE_MARKERS),
                         "secret_patterns": [SYNTHETIC_KEY, SYNTHETIC_TOKEN],
                         "raw_payload_patterns": [SYNTHETIC_RAW]}

    return build


def _private_values(cfg):
    return [cfg["scratch_dir"], *NATIVE_NAMES.values(), *NATIVE_MARKERS.values(),
            SYNTHETIC_KEY, SYNTHETIC_TOKEN, SYNTHETIC_RAW, "SYNTHETIC"]


@native_only
def test_native_scan_passes_current_clean_captures_and_emits_only_labels(native):
    _, cfg = native()
    cfg["raw_payload_patterns"].append(f'{{"package_id": "{NATIVE_MARKERS["audit"]}", "body": "synthetic"}}')
    result = threats.native_log_scan(cfg)
    assert result["outcome"] == "blocked"
    assert set(result) == {"outcome", "reason", "corpora"}
    assert result["corpora"] == ["audit", "logs", "traces"]
    assert not any(value in json.dumps(result) for value in _private_values(cfg))


@native_only
@pytest.mark.parametrize(("label", "leak", "finding"), [
    ("logs", SYNTHETIC_KEY, "synthetic key value"),
    ("traces", b64encode(SYNTHETIC_TOKEN.encode()).decode().rstrip("="), "synthetic key value"),
    ("audit", json.dumps(SYNTHETIC_RAW)[1:-1], "synthetic raw payload"),
    ("logs", SYNTHETIC_PEM, "PEM private key block"),
])
def test_native_scan_flags_each_leak_class_by_corpus_label_only(native, label, leak, finding):
    _, cfg = native(**{label: f'{{"marker": "{NATIVE_MARKERS[label]}", "detail": "{leak}"}}'})
    result = threats.native_log_scan(cfg)
    assert result["outcome"] == "not_blocked"
    assert result["corpora"] == [label]
    assert finding in result["reason"]
    assert leak not in json.dumps(result)
    assert not any(value in json.dumps(result) for value in _private_values(cfg))


@native_only
def test_native_scan_never_judges_a_capture_without_its_current_marker(native):
    _, cfg = native(traces=f"stale run {SYNTHETIC_KEY}")
    result = threats.native_log_scan(cfg)
    assert result == {"outcome": "not_run", "corpora": ["traces"],
                      "reason": "traces: capture lacks its current benign marker"}


def _outside(scratch: Path) -> Path:
    path = scratch.parent / "outside.log"
    path.write_text(f"{NATIVE_MARKERS['logs']} {SYNTHETIC_KEY}")
    return path


def _replace(scratch: Path, make) -> None:
    (scratch / "logs.log").unlink()
    make(scratch / "logs.log")


@native_only
@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda s: _replace(s, lambda p: p.symlink_to(_outside(s))), "logs: capture is a symbolic link"),
    (lambda s: _replace(s, lambda p: os.link(_outside(s), p)), "logs: capture has another hard link"),
    (lambda s: _replace(s, os.mkfifo), "logs: capture is not a regular file"),
    (lambda s: _replace(s, lambda p: p.write_bytes(b"")), "logs: capture is empty or over the size bound"),
    (lambda s: _replace(s, lambda p: p.write_bytes(NATIVE_MARKERS["logs"].encode() + b"\0")),
     "logs: capture is not text"),
    (lambda s: _replace(s, lambda p: p.write_bytes(NATIVE_MARKERS["logs"].encode() + b"\xff")),
     "logs: capture is not UTF-8 text"),
    (lambda s: (s / "logs.log").chmod(0o666), "logs: capture must be owned and writable only by this user"),
    (lambda s: (s / "extra.log").write_text(SYNTHETIC_KEY), "scratch_dir must hold exactly the three configured captures"),
    (lambda s: s.chmod(0o750), "scratch_dir must be an owner-only directory of this user"),
])
def test_native_scan_refuses_unsafe_or_arbitrary_files_without_judging_them(native, mutate, reason):
    scratch, cfg = native()
    mutate(scratch)
    result = threats.native_log_scan(cfg)
    assert result["outcome"] == "not_run"
    assert reason in result["reason"]
    assert SYNTHETIC_KEY not in json.dumps(result)


@native_only
def test_native_scan_bounds_capture_size(native, monkeypatch):
    _, cfg = native(audit=NATIVE_MARKERS["audit"] + " " * 64)
    monkeypatch.setattr(threats, "NATIVE_CAPTURE_LIMIT", 48)
    assert threats.native_log_scan(cfg) == {"outcome": "not_run", "corpora": ["audit"],
                                            "reason": "audit: capture is empty or over the size bound"}


@native_only
def test_native_scan_needs_a_fresh_scratch_dir_directly_under_temp(native, tmp_path):
    real, cfg = native()
    link = tmp_path / "alibi-sp46-link"
    link.symlink_to(real, target_is_directory=True)
    (tmp_path / "nested").mkdir()
    deep = native()[0].rename(tmp_path / "nested" / "alibi-sp46-deep")
    other = native()[0].rename(tmp_path / "other-dir")
    for path, reason in [
        (link, "scratch_dir is a symbolic link"),
        (deep, "scratch_dir must sit directly under the OS temp directory"),
        (other, "scratch_dir must be a fresh alibi-sp46-* directory"),
        (f"{real}/", "scratch_dir must be an absolute normalised path"),
        (real.name, "scratch_dir must be an absolute normalised path"),
    ]:
        assert threats.native_log_scan({**cfg, "scratch_dir": str(path)}) == {
            "outcome": "not_run", "corpora": ["audit", "logs", "traces"], "reason": reason}


@native_only
@pytest.mark.parametrize("change", [
    lambda c: c.pop("opt_in"),
    lambda c: c.update(opt_in="yes"),
    lambda c: c.update(extra=True),
    lambda c: c["captures"].pop("traces"),
    lambda c: c["captures"].update(logs="../logs.log"),
    lambda c: c["captures"].update(logs=".env"),
    lambda c: c["captures"].update(audit="audit.db"),
    lambda c: c["captures"].update(traces=["traces.json"]),
    lambda c: c["canaries"].update(logs=NATIVE_MARKERS["audit"].upper()),
    lambda c: c["canaries"].update(logs="short"),
    lambda c: c["canaries"].update(logs=SYNTHETIC_KEY),
    lambda c: c["canaries"].update(logs=SYNTHETIC_KEY[:18]),
    lambda c: c.update(secret_patterns=[]),
    lambda c: c.update(secret_patterns=["too-short"]),
    lambda c: c.update(raw_payload_patterns=[SYNTHETIC_RAW, SYNTHETIC_RAW]),
])
def test_native_config_is_validated_before_any_file_is_opened(native, monkeypatch, change):
    _, cfg = native()
    change(cfg)
    monkeypatch.setattr(threats, "_native_open_scratch", lambda *_: pytest.fail("opened the scratch dir"))
    result = threats.native_log_scan(cfg)
    assert result["outcome"] == "not_run" and result["corpora"] == ["audit", "logs", "traces"]
    assert not any(value in result["reason"] for value in _private_values(cfg))


@native_only
def test_native_cli_takes_only_an_explicit_json_config_and_prints_one_result(native, tmp_path, capsys):
    _, cfg = native()
    config = tmp_path / "sp46-native.json"
    config.write_text(json.dumps({threats.NATIVE_SP46_KEY: cfg, "base_urls": {}}))
    threats.main(["--sp46-native", str(config)])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["outcome"] == "blocked"

    threats.main(["--sp46-native", str(tmp_path / ".env")])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and json.loads(out[0]) == {
        "outcome": "not_run", "corpora": ["audit", "logs", "traces"],
        "reason": "native SP-46 config must be a regular .json file"}
