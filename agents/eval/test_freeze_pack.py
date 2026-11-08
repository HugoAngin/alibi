"""Check that official gold projection preserves the private per-request checks."""

from pathlib import Path

import pytest

from agents.eval.freeze_pack import prepare_pack
from alibi.eval import common, seal


def _inputs(root: Path):
    def request(request_id, audience):
        return {"request_id": request_id, "audience": audience, "issued_at": "2026-10-01T00:00:00Z",
                "question_ids": ["q.source_of_funds"]}

    first, second, attack = request("a", "bank_a"), request("b", "bank_b"), request("c", "bank_a")
    cases = [{"case_number": 1000, "rfcs": [first, second]}, {"case_number": 1001, "rfcs": [attack]}]
    gold = []
    for case in cases:
        for rfc in case["rfcs"]:
            req = rfc["request_id"]
            gold.append({"case_number": case["case_number"], "request_id": req, "audience": rfc["audience"],
                         "decision_time": rfc["issued_at"], "forbidden_refs": ["import:one", f"import:only-{req}"],
                         "forbidden_values": [f"private-{req}"], "answers": {"q.source_of_funds": {
                             "status": "supported", "gap": None, "facts": {},
                             "evidence_sets": [[{"source_id": "merchant:record", "revision": 2}]],
                         }}})
    steps = [{"case_number": 1001, "request_id": "c", "step_id": "probe", "probe_id": "SP-01"}]
    paths = [root / f"in-{name}.jsonl" for name in ("sealed", "adversarial", "gold", "steps")]
    for path, rows in zip(paths, ([cases[0]], [cases[1]], gold, steps)):
        common.write_jsonl(path, rows)
    return paths


def test_prepared_pack_verifies_with_official_manifest(tmp_path):
    paths = _inputs(tmp_path)
    output = tmp_path / "private-pack"
    assert prepare_pack(*paths, output) == {"sealed_cases": 1, "adversarial_cases": 1, "requests": 3, "probe_steps": 1}
    gold = common.read_gold_file(output / "gold.jsonl")
    assert gold[1000]["forbidden_refs"] == ["import:one"]
    assert gold[1000]["answers"]["b"]["q.source_of_funds"]["evidence_sets"] == [["merchant:record"]]
    check = gold[1000]["request_checks"]["b"]
    assert check["forbidden_values"] == ["private-b"]
    assert check["answers"]["q.source_of_funds"]["evidence_sets"][0][0]["revision"] == 2
    assert gold[1001]["probe_steps"][0]["probe_id"] == "SP-01"
    assert all("gold" not in row for path in (output / "cases").glob("*.jsonl") for row in common.read_jsonl(path))
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "gold.jsonl").stat().st_mode & 0o777) == 0o600

    manifest = tmp_path / "manifest.json"
    seal.write_json(manifest, seal.build_manifest(output / "cases", output / "gold.jsonl", purpose="dev"))
    errors, _ = seal.verify_manifest(manifest)
    assert errors == []


def test_bad_gold_binding_does_not_create_an_output_pack(tmp_path):
    paths = _inputs(tmp_path)
    gold = common.read_jsonl(paths[2])
    gold[0]["audience"] = "bank_b"
    common.write_jsonl(paths[2], gold)
    output = tmp_path / "private-pack"
    with pytest.raises(ValueError, match="recipient"):
        prepare_pack(*paths, output)
    assert not output.exists()
