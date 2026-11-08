"""Read three receipt PNGs with Gemini vision and score every field against the true fields.

    uv run --frozen python -m agents.data.vision_check            # 3 live Flash calls, needs GEMINI_API_KEY
    uv run --frozen python -m agents.data.vision_check --ids rcpt-01 rcpt-06

Uses alibi.agents.receipt_vision.extract_receipt with the real vision transport and a call budget
of exactly one call per receipt. Writes agents/data/out/vision_check.json (the extracted fields,
the true fields from out/receipts/receipts.json, and a per-field match) and prints a one-line JSON
summary last. The image hash and model are kept; the key is never printed. Exit 2 without a key.

Default receipts: rcpt-06 (the demo payout PO-0001-01, app screenshot), rcpt-09 (handwritten on
lined paper, no reference), rcpt-10 (the same payout as a printed letter photographed at an angle).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from agents.data.receipts import OUT as RECEIPTS

OUT = Path(__file__).resolve().parent / "out" / "vision_check.json"
DEFAULT_IDS = ("rcpt-06", "rcpt-09", "rcpt-10")
FIELDS = ("issuer", "document_date", "reference", "currency", "total_minor", "kind", "item_count")
# receipts.json names the handwritten kind "handwritten_receipt"; the vision schema says "handwritten".
KIND_ALIASES = {"handwritten_receipt": "handwritten", "weekly_statement": "other"}


def truth() -> dict[str, dict]:
    return {r["id"]: r for r in json.loads((RECEIPTS / "receipts.json").read_text())["receipts"]}


def _norm(value: Any) -> Any:
    return " ".join(value.lower().split()) if isinstance(value, str) else value


def compare(true: dict, got: dict) -> dict[str, dict]:
    """Per-field {true, got, match}. Issuer matches when one name contains the other (case-folded)."""
    want = {"issuer": true["issuer"], "document_date": true["document_date"], "reference": true["reference"],
            "currency": true["currency"], "total_minor": true["total_minor"],
            "kind": KIND_ALIASES.get(true["kind"], true["kind"]), "item_count": len(true.get("items") or [])}
    have = {"issuer": got.get("issuer_name"), "document_date": got.get("document_date"),
            "reference": got.get("reference"), "currency": got.get("currency"),
            "total_minor": got.get("total_minor"), "kind": got.get("kind"),
            "item_count": len(got.get("items") or [])}
    out = {}
    for f in FIELDS:
        w, h = _norm(want[f]), _norm(have[f])
        if f == "issuer" and isinstance(w, str) and isinstance(h, str) and h:
            ok = w in h or h in w
        else:
            ok = w == h
        out[f] = {"true": want[f], "got": have[f], "match": ok}
    return out


def run(ids: tuple[str, ...], transport: Callable, budget_factory: Callable[[], Any]) -> dict:
    from alibi.agents.receipt_vision import extract_receipt

    t = truth()
    rows = []
    for rid in ids:
        rec = t[rid]
        draft = extract_receipt(RECEIPTS / rec["file"], subject_ref="maya-001", transport=transport,
                                budget=budget_factory())
        fields = compare(rec, draft.fields)
        rows.append({"id": rid, "file": rec["file"], "layout": rec["layout"], "distortion": rec["distortion"],
                     "model": draft.model, "image_sha256": draft.image_sha256,
                     "confidence": draft.confidence, "source_trust": draft.source_trust,
                     "requires_confirmation": draft.requires_confirmation,
                     "record_status": draft.record.get("status"), "problems": draft.problems,
                     "notes": draft.fields.get("notes"), "fields": fields,
                     "matched": sum(v["match"] for v in fields.values()), "of": len(fields)})
    summary = {"receipts": len(rows), "fields_matched": sum(r["matched"] for r in rows),
               "fields_total": sum(r["of"] for r in rows),
               "all_correct": [r["id"] for r in rows if r["matched"] == r["of"]],
               "calls": len(rows)}
    return {"label": "SYNTHETIC receipts; live Gemini vision read vs data's true fields", "summary": summary,
            "rows": rows}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ids", nargs="+", default=list(DEFAULT_IDS))
    args = p.parse_args(argv)
    from alibi.llm.client import CallBudget, MissingApiKey, load_api_key
    from alibi.agents.receipt_vision import gemini_vision_transport

    try:
        key = load_api_key()
    except MissingApiKey as exc:
        print(json.dumps({"error": f"MissingApiKey: {exc}"}), file=sys.stderr)
        return 2
    result = run(tuple(args.ids), gemini_vision_transport(key), lambda: CallBudget(1, name="vision-check"))
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    for r in result["rows"]:
        print(f"{r['id']:8} {r['matched']}/{r['of']}  conf {r['confidence']:.2f}  "
              + ", ".join(f"{k}={v['got']!r}" for k, v in r["fields"].items() if not v["match"]))
    print(json.dumps(result["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
