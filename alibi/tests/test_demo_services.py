"""The demo runner builds and seeds both services from one keystore, and serving stops on the
timeout. Ports 0 are ephemeral, so the smoke test never collides with a running demo."""

import io
import json

import pytest

from alibi import demo_services
from alibi.connectors.keys import MERCHANT_KID


def _args(tmp_path, *extra):
    return demo_services.parse_args([
        "--keys", str(tmp_path / "keys"), "--state", str(tmp_path / "bank" / "state.json"),
        "--tokens", str(tmp_path / "tokens.json"), *extra])


def test_build_apps_seeds_both_services(tmp_path, monkeypatch):
    for name in ("ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_FEED_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    built = demo_services.build_apps(_args(tmp_path, "--case", "1", "--as-of", "2026-11-04T14:00:00Z"))
    merchant_seed, bank_seed = built["seeds"]["merchant"], built["seeds"]["bank"]
    assert merchant_seed["case_number"] == 1 and merchant_seed["as_of"] == "2026-11-04T14:00:00Z"
    assert len(merchant_seed["pushed"]) == 20
    assert {p["outcome"] for p in merchant_seed["pushed"]} == {"not_delivered"}
    assert bank_seed["case_number"] == 1 and bank_seed["recipients"] == ["bank_a", "bank_b"]
    assert MERCHANT_KID in built["keystore"].registry().kids()
    assert (tmp_path / "tokens.json").exists() and built["tokens_path"] == str(tmp_path / "tokens.json")
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert set(tokens) == {"bank_a", "bank_b", "feed"}


def test_default_clock_is_the_first_rfc_time(tmp_path):
    built = demo_services.build_apps(_args(tmp_path))
    assert built["seeds"]["merchant"]["as_of"] == "2026-11-02T09:00:00Z"


def test_bad_as_of_is_refused_before_anything_starts(tmp_path):
    with pytest.raises(Exception):
        demo_services.build_apps(_args(tmp_path, "--as-of", "yesterday"))


def test_serve_runs_until_the_timeout_and_exits_cleanly(tmp_path):
    out = io.StringIO()
    code = demo_services.serve(_args(tmp_path, "--timeout", "1.5", "--merchant-port", "0",
                                     "--bank-port", "0"), out=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert code == 0
    assert lines[0]["case"] == 1 and lines[0]["merchant_pushed"] == 20
    assert lines[0]["tokens_file"] == str(tmp_path / "tokens.json")
    tokens = json.loads((tmp_path / "tokens.json").read_text())
    assert not any(value in out.getvalue() for value in tokens.values())   # never printed
    assert lines[-1] == {"stopped": True, "failed": []}
