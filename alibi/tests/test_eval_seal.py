"""Sealing: write and verify round-trip, and a changed byte in cases, gold or config is detected.
Expected lengths are derived from the loaded case set."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.data import api as data_api
from alibi.eval import jsonschema_lite, seal
from alibi.eval.common import EvalError, load_json, load_schema, write_jsonl


@pytest.fixture
def sealed(tmp_path: Path) -> dict:
    cases = data_api.load_cases()
    root = tmp_path / "sealed"
    write_jsonl(root / "part_a.jsonl", [data_api.without_gold(c) for c in cases[:20]])
    write_jsonl(root / "part_b.jsonl", [data_api.without_gold(c) for c in cases[20:]])
    gold = tmp_path / "gold.jsonl"
    write_jsonl(gold, [{"case_number": c["case_number"], "gold": c["gold"]} for c in cases])
    config = tmp_path / "model_config.json"
    config.write_text(json.dumps({"routine": "gemini-3.8-flash", "frontier": "gemini-4-argon",
                                  "thinking": {"routine": "low", "frontier": "medium"}}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    return {"root": root, "gold": gold, "config": config, "manifest": manifest, "cases": cases}


def write(sealed: dict, capsys) -> dict:
    code = seal.main(["write", "--cases", str(sealed["root"]), "--gold", str(sealed["gold"]),
                      "--model-config", str(sealed["config"]), "--purpose", "dev", "--frozen-by", "test",
                      "--systems", "rules", "one_pass", "alibi", "--out", str(sealed["manifest"])])
    assert code == 0, capsys.readouterr()
    return load_json(sealed["manifest"])


def verify(sealed: dict, capsys) -> tuple[int, str]:
    code = seal.main(["verify", str(sealed["manifest"])])
    return code, capsys.readouterr().out


def test_write_and_verify_roundtrip(sealed, capsys):
    manifest = write(sealed, capsys)
    assert jsonschema_lite.errors(load_schema("manifest"), manifest) == []
    n = len(sealed["cases"])
    assert len(manifest["cases"]["entries"]) == n and len(manifest["gold"]["entries"]) == n
    assert [f["file"] for f in manifest["cases"]["files"]] == ["part_a.jsonl", "part_b.jsonl"]
    assert manifest["cases"]["entries"][0]["request_ids"] == ["rfc-0001-01", "rfc-0001-02"]
    assert manifest["scoring"]["rules_version"] == "alibi.eval.scoring.v2"
    assert manifest["scoring"]["sha256"] == seal.scoring_sha256()
    assert manifest["model_config"]["content"]["frontier"] == "gemini-4-argon"
    assert manifest["candidate"]["systems"] == ["rules", "one_pass", "alibi"]
    assert len(manifest["candidate"]["commit"]) == 40
    code, out = verify(sealed, capsys)
    assert code == 0 and "seal: verified" in out


def test_changed_byte_in_a_case_is_detected(sealed, capsys):
    write(sealed, capsys)
    path = sealed["root"] / "part_a.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["case_number"] == 1
    first["bank_deliveries"][-1]["row"]["amount_minor"] += 1
    lines[0] = json.dumps(first, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    code, out = verify(sealed, capsys)
    assert code == 1 and "case 1: content changed since sealing" in out
    assert "case 2" not in out


def test_changed_gold_missing_and_extra_cases_are_detected(sealed, capsys):
    write(sealed, capsys)
    gold_lines = sealed["gold"].read_text(encoding="utf-8").splitlines()
    entry = json.loads(gold_lines[1])
    entry["gold"]["claim_status"] = "supported"
    gold_lines[1] = json.dumps(entry, ensure_ascii=False)
    sealed["gold"].write_text("\n".join(gold_lines) + "\n", encoding="utf-8", newline="\n")
    part_b = sealed["root"] / "part_b.jsonl"
    rows = [json.loads(line) for line in part_b.read_text(encoding="utf-8").splitlines()]
    removed = rows.pop()
    extra = dict(removed, case_number=999)
    write_jsonl(part_b, rows + [extra])
    code, out = verify(sealed, capsys)
    assert code == 1
    assert "gold for case 2: changed since sealing" in out
    assert f"case {removed['case_number']}: not found" in out
    assert "cases present but not sealed: [999]" in out
    assert "gold file digest changed" in out


def test_line_endings_do_not_matter(sealed, capsys):
    write(sealed, capsys)
    for path in (sealed["root"] / "part_a.jsonl", sealed["gold"]):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    code, out = verify(sealed, capsys)
    assert code == 0 and "verified" in out


def test_model_config_change_is_detected(sealed, capsys):
    write(sealed, capsys)
    sealed["config"].write_text(json.dumps({"routine": "gemini-3.8-flash", "frontier": "other"}), encoding="utf-8")
    code, out = verify(sealed, capsys)
    assert code == 1 and "model configuration changed" in out


def test_scoring_change_is_detected(sealed, capsys, monkeypatch):
    write(sealed, capsys)
    monkeypatch.setattr(seal, "scoring_sha256", lambda: "0" * 64)
    code, out = verify(sealed, capsys)
    assert code == 1 and "scoring rules changed" in out


def test_set_digest_is_checked(sealed, capsys):
    manifest = write(sealed, capsys)
    manifest["set_sha256"] = "f" * 64
    sealed["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    code, out = verify(sealed, capsys)
    assert code == 1 and "set_sha256 does not match" in out


def test_write_refuses_gold_in_cases_and_mismatched_gold(sealed, capsys):
    with_gold = sealed["root"] / "part_c.jsonl"
    write_jsonl(with_gold, [dict(sealed["cases"][0], case_number=998)])
    with pytest.raises(EvalError, match="carries gold"):
        seal.build_manifest(sealed["root"], sealed["gold"])
    with_gold.unlink()
    last = sealed["cases"][-1]["case_number"]
    write_jsonl(sealed["gold"], [{"case_number": c["case_number"], "gold": c["gold"]}
                                 for c in sealed["cases"][:-1]])
    with pytest.raises(EvalError, match=f"cases without gold \\[{last}\\]"):
        seal.build_manifest(sealed["root"], sealed["gold"])


def test_gold_file_accepts_a_keyed_object(sealed, capsys):
    keyed = sealed["gold"].with_suffix(".json")
    keyed.write_text(json.dumps({str(c["case_number"]): c["gold"] for c in sealed["cases"]}),
                     encoding="utf-8")
    manifest = seal.build_manifest(sealed["root"], keyed, model_config=sealed["config"], purpose="dev")
    assert len(manifest["gold"]["entries"]) == len(sealed["cases"])
    assert manifest["gold"]["entries"][0]["sha256"] == seal.gold_entries(sealed["gold"])[1][0]["sha256"]
