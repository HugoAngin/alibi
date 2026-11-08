"""alibi.agents.replay: record FakeClient answers, replay by sha and by role order, env switch."""

import json

import pytest

from alibi.agents import replay as rp
from alibi.llm.client import BudgetExceeded, CallBudget, SchemaValidationError, Unavailable, Usage
from alibi.llm.fake import FakeClient

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
CHALLENGER = "# Challenger\n\nYou are the Challenger. Passport number ABC123 is private."
CURATOR = "# Curator\n\nYou are the Curator."
USER = "claim c1 about txn_9001 for Maya Example"
MODEL = "gemini-4-argon"


def record_two(tmp_path, scripts=None):
    fake = FakeClient(scripts or {"challenger": {"verdict": "hold"}, "curator": {"verdict": "share"}})
    client = rp.make_client("case_1", "supported", live=fake, env={}, directory=tmp_path)
    r1 = client.generate(MODEL, CHALLENGER, USER, SCHEMA, CallBudget(3))
    r2 = client.generate("gemini-3.8-flash", CURATOR, USER, SCHEMA, CallBudget(3))
    return client, r1, r2


# --------------------------------------------------------------------------- paths


def test_replay_path_and_validation(tmp_path):
    assert rp.replay_path("case_1", "supported", tmp_path) == tmp_path / "case_1_supported.json"
    assert rp.replay_path("c", "v").parent == rp.REPLAY_DIR
    assert rp.REPLAY_DIR.name == "replays" and rp.REPLAY_DIR.parent.name == "fixtures"
    for bad in ("../x", "a/b", "a b", "", "x.json", "a\\b"):
        with pytest.raises(ValueError):
            rp.replay_path(bad, "supported", tmp_path)
        with pytest.raises(ValueError):
            rp.replay_path("case_1", bad, tmp_path)


def test_list_recordings(tmp_path):
    assert rp.list_recordings(tmp_path / "missing") == []
    record_two(tmp_path)
    (tmp_path / "notes.txt").write_text("x")
    assert rp.list_recordings(tmp_path) == [tmp_path / "case_1_supported.json"]


# --------------------------------------------------------------------------- recording


def test_recording_writes_file_without_prompt_text(tmp_path):
    client, r1, r2 = record_two(tmp_path)
    assert isinstance(client, rp.RecordingClient)
    assert client.replay is False and client.label == "live"
    assert r1.data == {"verdict": "hold"} and r2.data == {"verdict": "share"}

    path = tmp_path / "case_1_supported.json"
    assert client.path == path and path.is_file()
    assert not (tmp_path / "case_1_supported.json.tmp").exists()
    text = path.read_text(encoding="utf-8")
    assert "ABC123" not in text and "Maya Example" not in text and "You are the" not in text

    payload = json.loads(text)
    assert payload["schema"] == "alibi.replay.v1"
    assert payload["case"] == "case_1" and payload["variant"] == "supported"
    assert payload["models"] == sorted(["gemini-3.8-flash", MODEL])
    assert payload["recorded_at"].endswith("Z")
    calls = payload["calls"]
    assert [c["seq"] for c in calls] == [0, 1]
    assert calls[0]["role"] == "# Challenger" and calls[1]["role"] == "# Curator"
    assert calls[0]["prompt_sha256"] == r1.prompt_sha256 and calls[1]["prompt_sha256"] == r2.prompt_sha256
    assert calls[0]["data"] == {"verdict": "hold"}
    assert calls[0]["usage"] == {"prompt_tokens": 10, "output_tokens": 10, "thinking_tokens": 0, "total_tokens": 20}
    assert calls[0]["cached"] is False
    assert set(calls[0]) == {"seq", "model", "prompt_sha256", "role", "data", "usage", "cached", "recorded_at"}


def test_recording_saves_after_every_call(tmp_path):
    fake = FakeClient({"*": {"verdict": "ok"}})
    client = rp.RecordingClient(fake, tmp_path / "case_1_supported.json", case="case_1", variant="supported")
    client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    assert len(json.loads(client.path.read_text())["calls"]) == 1
    client.generate(MODEL, CHALLENGER, USER + " again", SCHEMA)
    assert len(json.loads(client.path.read_text())["calls"]) == 2


def test_recording_records_failure_and_reraises(tmp_path):
    fake = FakeClient({"challenger": Unavailable("simulated outage"), "curator": {"verdict": "share"}})
    client = rp.make_client("case_1", "contradiction", live=fake, env={}, directory=tmp_path)
    with pytest.raises(Unavailable):
        client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    client.generate(MODEL, CURATOR, USER, SCHEMA)
    calls = json.loads(client.path.read_text())["calls"]
    assert calls[0]["error"] == "Unavailable" and calls[0]["message"] == "simulated outage"
    assert "data" not in calls[0] and calls[0]["role"] == "# Challenger"
    assert "ABC123" not in client.path.read_text()
    assert calls[1]["data"] == {"verdict": "share"}


# --------------------------------------------------------------------------- replay


def test_replay_by_sha(tmp_path):
    _, r1, r2 = record_two(tmp_path)
    client = rp.make_client("case_1", "supported", env={"ALIBI_REPLAY": "1"}, directory=tmp_path)
    assert isinstance(client, rp.ReplayClient)
    assert client.replay is True and client.label == "replay: case_1_supported.json"
    # Ask in the opposite order: sha matching finds the right entry regardless of position.
    out2 = client.generate("gemini-3.8-flash", CURATOR, USER, SCHEMA, CallBudget(0))
    out1 = client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    assert out2.data == {"verdict": "share"} and out1.data == {"verdict": "hold"}
    assert out1.cached is True and out1.attempts == 0 and out1.model == MODEL
    assert out1.prompt_sha256 == r1.prompt_sha256 and out2.prompt_sha256 == r2.prompt_sha256
    assert out1.usage == Usage(prompt_tokens=10, output_tokens=10, total_tokens=20)
    assert client.served == 2 and client.misses == 0 and client.remaining == 0


def test_replay_returns_copies(tmp_path):
    record_two(tmp_path)
    client = rp.ReplayClient(tmp_path / "case_1_supported.json")
    out = client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    out.data["verdict"] = "mutated"
    assert client.calls[0]["data"] == {"verdict": "hold"}


def test_replay_by_role_order_when_prompt_differs(tmp_path):
    fake = FakeClient({"challenger": [{"verdict": "first"}, {"verdict": "second"}], "curator": {"verdict": "share"}})
    rec = rp.make_client("case_1", "supported", live=fake, env={}, directory=tmp_path)
    rec.generate(MODEL, CHALLENGER, USER, SCHEMA)
    rec.generate(MODEL, CURATOR, USER, SCHEMA)
    rec.generate(MODEL, CHALLENGER, USER + " round 2", SCHEMA)

    client = rp.ReplayClient(tmp_path / "case_1_supported.json")
    drifted = CHALLENGER + "\n\nNew instruction added after recording."
    a = client.generate(MODEL, drifted, "different user text", SCHEMA)
    b = client.generate(MODEL, drifted, "different user text", SCHEMA)
    c = client.generate(MODEL, CURATOR + " edited", "x", SCHEMA)
    assert a.data == {"verdict": "first"} and b.data == {"verdict": "second"} and c.data == {"verdict": "share"}
    assert client.served == 3


def test_replay_prefers_sha_over_role_order(tmp_path):
    fake = FakeClient({"challenger": [{"verdict": "first"}, {"verdict": "second"}]})
    rec = rp.make_client("case_1", "supported", live=fake, env={}, directory=tmp_path)
    rec.generate(MODEL, CHALLENGER, "round 1", SCHEMA)
    rec.generate(MODEL, CHALLENGER, "round 2", SCHEMA)
    client = rp.ReplayClient(tmp_path / "case_1_supported.json")
    assert client.generate(MODEL, CHALLENGER, "round 2", SCHEMA).data == {"verdict": "second"}
    assert client.generate(MODEL, CHALLENGER, "round 1", SCHEMA).data == {"verdict": "first"}


def test_replay_miss_when_exhausted(tmp_path):
    record_two(tmp_path)
    client = rp.ReplayClient(tmp_path / "case_1_supported.json")
    client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    with pytest.raises(rp.ReplayMiss, match="Challenger"):
        client.generate(MODEL, CHALLENGER, USER, SCHEMA)
    with pytest.raises(rp.ReplayMiss, match="Detective"):
        client.generate(MODEL, "# Detective\n\nfind things", USER, SCHEMA)
    assert issubclass(rp.ReplayMiss, rp.LLMError)
    assert client.served == 1 and client.misses == 2


def test_replay_of_recorded_failures(tmp_path):
    scripts = {
        "challenger": Unavailable("outage"),
        "curator": SchemaValidationError(["$.verdict: missing"]),
        "detective": BudgetExceeded("task budget spent"),
        "matcher": KeyError("something odd"),
    }
    rec = rp.make_client("case_2", "supported", live=FakeClient(scripts), env={}, directory=tmp_path)
    for role in ("Challenger", "Curator", "Detective", "Matcher"):
        with pytest.raises(Exception):
            rec.generate(MODEL, f"# {role}\n\nbody", USER, SCHEMA)
    assert [c["error"] for c in rec.entries] == ["Unavailable", "SchemaValidationError", "BudgetExceeded", "KeyError"]

    client = rp.ReplayClient(tmp_path / "case_2_supported.json")
    with pytest.raises(Unavailable, match="outage"):
        client.generate(MODEL, "# Challenger\n\nbody", USER, SCHEMA)
    with pytest.raises(SchemaValidationError, match="missing"):
        client.generate(MODEL, "# Curator\n\nbody", USER, SCHEMA)
    with pytest.raises(BudgetExceeded):
        client.generate(MODEL, "# Detective\n\nbody", USER, SCHEMA)
    with pytest.raises(Unavailable, match="KeyError"):
        client.generate(MODEL, "# Matcher\n\nbody", USER, SCHEMA)
    assert client.served == 4


# --------------------------------------------------------------------------- factory and labels


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
def test_make_client_honours_replay_env(tmp_path, value):
    record_two(tmp_path)
    client = rp.make_client("case_1", "supported", live=FakeClient(), env={"ALIBI_REPLAY": value}, directory=tmp_path)
    assert isinstance(client, rp.ReplayClient)


@pytest.mark.parametrize("env", [{}, {"ALIBI_REPLAY": "0"}, {"ALIBI_REPLAY": "no"}, {"ALIBI_REPLAY": ""}])
def test_make_client_live_when_env_off(tmp_path, env):
    fake = FakeClient()
    client = rp.make_client("case_1", "supported", live=fake, env=env, directory=tmp_path)
    assert isinstance(client, rp.RecordingClient) and client.live is fake
    assert not client.path.exists()  # nothing written until the first call


def test_make_client_replay_missing_file_names_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="case_9_ambiguous.json"):
        rp.make_client("case_9", "ambiguous", env={"ALIBI_REPLAY": "1"}, directory=tmp_path)


def test_make_client_rejects_bad_identifiers(tmp_path):
    with pytest.raises(ValueError):
        rp.make_client("../etc", "supported", live=FakeClient(), env={}, directory=tmp_path)


def test_replay_client_rejects_other_schema(tmp_path):
    path = tmp_path / "case_1_supported.json"
    path.write_text(json.dumps({"schema": "other", "calls": []}))
    with pytest.raises(ValueError, match="schema"):
        rp.ReplayClient(path)


def test_labels(tmp_path):
    rec, _, _ = record_two(tmp_path)
    assert rp.replay_label(rec) == "live"
    rep = rp.ReplayClient(tmp_path / "case_1_supported.json")
    assert rp.replay_label(rep) == "REPLAY: served from recording case_1_supported.json"
    assert rp.replay_label(FakeClient()) == "live"


def test_role_of():
    assert rp.role_of(CHALLENGER) == "# Challenger"
    assert rp.role_of("\n\n  # Matcher  \nmore") == "# Matcher"
    assert rp.role_of("") == ""
