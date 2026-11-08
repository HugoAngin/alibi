"""vision_check scoring, with a fake vision transport (no Gemini call)."""
from __future__ import annotations

import json

from agents.data import vision_check


def _answer(rec: dict, **over) -> str:
    kind = vision_check.KIND_ALIASES.get(rec["kind"], rec["kind"])
    body = {"issuer_name": rec["issuer"], "document_date": rec["document_date"], "reference": rec["reference"],
            "currency": rec["currency"], "total_minor": rec["total_minor"],
            "items": rec.get("items") or [], "kind": kind, "confidence": 0.9, "notes": ""}
    body.update(over)
    return json.dumps(body)


def test_perfect_reads_score_full_and_wrong_total_is_caught():
    t = vision_check.truth()
    answers = {t["rcpt-06"]["file"]: _answer(t["rcpt-06"]),
               t["rcpt-09"]["file"]: _answer(t["rcpt-09"], total_minor=4000),
               t["rcpt-10"]["file"]: _answer(t["rcpt-10"], issuer_name="MOCK MARKETPLACE LTD")}
    by_sha = {}

    def transport(model, system, image_bytes, mime, schema):
        import hashlib
        sha = hashlib.sha256(image_bytes).hexdigest()
        if not by_sha:
            for f, a in answers.items():
                by_sha[hashlib.sha256((vision_check.RECEIPTS / f).read_bytes()).hexdigest()] = a
        return by_sha[sha]

    out = vision_check.run(vision_check.DEFAULT_IDS, transport, lambda: None)
    rows = {r["id"]: r for r in out["rows"]}
    assert rows["rcpt-06"]["matched"] == rows["rcpt-06"]["of"]
    assert not rows["rcpt-09"]["fields"]["total_minor"]["match"]
    assert rows["rcpt-10"]["fields"]["issuer"]["match"]          # containment, case-folded
    assert all(r["source_trust"] == "self_supplied" and r["requires_confirmation"] for r in out["rows"])
    assert out["summary"]["calls"] == 3


def test_default_receipts_exist_and_cover_hard_layouts():
    t = vision_check.truth()
    assert {t[i]["distortion"] for i in vision_check.DEFAULT_IDS} >= {"lined_paper", "photo_at_angle"}
    assert t["rcpt-06"]["reference"] == "PO-0001-01"
