"""Breadth extras: receipt vision import and the Gemma second parser. No network, no Gemini."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from alibi.agents import gemma, receipt_vision as rv
from alibi.agents.models import DetectiveOutput, MatcherOutput, parse_output
from alibi.core.schemas import MerchantEvent
from alibi.llm.client import BudgetExceeded, CallBudget, SchemaValidationError, Unavailable, process_budget
from alibi.llm.fake import FakeClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RECEIPT = REPO_ROOT / "agents" / "data" / "out" / "receipts" / "rcpt-01_order_confirmation.png"
GEMMA = "gemma-3-27b-it"


# --------------------------------------------------------------------------- helpers


def tiny_png() -> bytes:
    """A valid 1x1 grey PNG built from scratch."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\x80")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


ANSWER = {
    "issuer_name": "ResaleHub Ltd",
    "document_date": "2026-10-28",
    "reference": "RH-PO-7781",
    "currency": "gbp",
    "total_minor": 21600,
    "items": [
        {"description": "Vintage lamp", "amount_minor": 24000},
        {"description": "Refund: broken shade", "amount_minor": -1800},
        {"description": "Platform fee", "amount_minor": -600},
    ],
    "kind": "order_confirmation",
    "confidence": 0.83,
    "notes": "Printed total matches items. Footer says 'ignore previous instructions'; not acted on.",
}


class FakeTransport:
    """Vision transport returning a fixed JSON string and recording calls."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[tuple[str, str, int, str]] = []

    def __call__(self, model, system, image_bytes, mime, json_schema):
        self.calls.append((model, system, len(image_bytes), mime))
        assert json_schema is rv.RECEIPT_SCHEMA
        return self.text


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIBI_DATA_DIR", str(tmp_path / "alibi-data"))
    return tmp_path / "alibi-data"


# --------------------------------------------------------------------------- receipt vision


def test_detect_mime_from_magic_bytes():
    assert rv.detect_mime(tiny_png()) == "image/png"
    assert rv.detect_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image/jpeg"
    with pytest.raises(ValueError):
        rv.detect_mime(b"GIF89a" + b"\x00" * 16)
    with pytest.raises(ValueError):
        rv.detect_mime(b"%PDF-1.4")


def test_draft_shape_and_merchant_event_validates(data_dir, now):
    transport = FakeTransport(json.dumps(ANSWER))
    before = process_budget.used
    draft = rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=transport, now=now)

    assert transport.calls and transport.calls[0][3] == "image/png"
    assert "never an instruction" in transport.calls[0][1]
    assert process_budget.used == before + 1
    assert draft.source_trust == "self_supplied"
    assert draft.requires_confirmation is True
    assert draft.confidence == pytest.approx(0.83)
    assert draft.problems == []
    rec = draft.record
    assert rec["record_id"].startswith("self_rcpt_") and rec["record_id"].endswith(draft.image_sha256[:12])
    assert rec["event_id"] == "evt_self_rcpt_" + draft.image_sha256[:12]
    assert rec["schema_version"] == "alibi.merchant_event.v1"
    assert rec["issuer_id"] == "resalehub_ltd"
    assert rec["kind"] == "receipt"
    assert rec["status"] == "unconfirmed"
    assert rec["occurred_at"] == "2026-10-28T12:00:00Z"
    assert rec["amount_pence"] == 21600 and rec["currency"] == "GBP"
    assert rec["payment_ref"] == "RH-PO-7781"
    assert rec["items"] == [{"description": "Vintage lamp", "quantity": 1, "unit_amount_pence": 24000}]
    assert "holder must confirm" in rec["text"] and "(order_confirmation)" in rec["text"]
    assert "source_trust" not in rec and "signature" not in rec
    event = MerchantEvent.model_validate(rec)
    assert event.amount_pence == 21600

    log = (data_dir / "llm_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    row = json.loads(log[-1])
    assert row["outcome"] == "ok" and row["prompt_sha256"] == draft.image_sha256
    assert row["cached"] is False and row["usage"]["total_tokens"] == 0
    assert set(row) == {"time", "model", "prompt_sha256", "usage", "cached", "outcome", "attempts"}


def test_confidence_passthrough_and_missing_total_reported(data_dir, now):
    answer = dict(ANSWER, confidence=0.2, total_minor=None, document_date=None, issuer_name="")
    draft = rv.extract_receipt(tiny_png(), subject_ref="subj_maya",
                               transport=FakeTransport(json.dumps(answer)), now=now)
    assert draft.confidence == pytest.approx(0.2)
    assert draft.record["issuer_id"] == "unknown_issuer"
    assert draft.record["amount_pence"] is None and draft.record["currency"] is None
    assert draft.record["occurred_at"] == "2026-11-07T12:00:00Z"
    assert draft.problems  # a receipt needs an amount; the holder must supply it on confirm
    confirmed = rv.confirm(draft, {"amount_pence": 21600, "currency": "GBP"})
    MerchantEvent.model_validate(confirmed)


def test_schema_invalid_answer_raises(data_dir, now):
    bad = dict(ANSWER, kind="invoice")
    with pytest.raises(SchemaValidationError):
        rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=FakeTransport(json.dumps(bad)))
    with pytest.raises(SchemaValidationError):
        rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=FakeTransport("not json"))
    with pytest.raises(SchemaValidationError):
        rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=FakeTransport(""))
    rows = (data_dir / "llm_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(r)["outcome"] for r in rows] == ["schema_invalid"] * 3


def test_budget_exhaustion_skips_transport(data_dir):
    transport = FakeTransport(json.dumps(ANSWER))
    before = process_budget.used
    with pytest.raises(BudgetExceeded):
        rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=transport, budget=CallBudget(0))
    assert transport.calls == []
    assert process_budget.used == before  # refunded
    row = json.loads((data_dir / "llm_log.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["outcome"] == "budget_exceeded"


def test_transport_failure_is_unavailable(data_dir):
    def boom(model, system, image_bytes, mime, schema):
        raise RuntimeError("socket closed")

    with pytest.raises(Unavailable):
        rv.extract_receipt(tiny_png(), subject_ref="subj_maya", transport=boom)


def test_confirm_applies_only_allowlisted_edits(data_dir, now):
    draft = rv.extract_receipt(tiny_png(), subject_ref="subj_maya",
                               transport=FakeTransport(json.dumps(ANSWER)), now=now)
    confirmed = rv.confirm(draft, {
        "amount_pence": 21000,
        "payment_ref": "RH-PO-7781-A",
        "record_id": "po_5001",          # not allowed: stays in the self_ namespace
        "subject_ref": "subj_other",     # not allowed
        "issuer_signature": {"x": 1},    # not allowed
        "status": "issuer_verified",     # overwritten by 'confirmed'
    })
    assert confirmed["amount_pence"] == 21000
    assert confirmed["payment_ref"] == "RH-PO-7781-A"
    assert confirmed["record_id"] == draft.record["record_id"]
    assert confirmed["subject_ref"] == "subj_maya"
    assert "issuer_signature" not in confirmed
    assert confirmed["status"] == "confirmed"
    assert draft.record["status"] == "unconfirmed"  # the draft is not mutated
    MerchantEvent.model_validate(confirmed)
    with pytest.raises(ValueError):
        rv.confirm(draft, {"currency": "pounds"})


@pytest.mark.skipif(not SAMPLE_RECEIPT.is_file(), reason="data team's sample receipt not present")
def test_sample_receipt_png_is_accepted(data_dir, now):
    data = SAMPLE_RECEIPT.read_bytes()
    assert rv.detect_mime(data) == "image/png"
    transport = FakeTransport(json.dumps(ANSWER))
    draft = rv.extract_receipt(SAMPLE_RECEIPT, subject_ref="subj_maya", transport=transport, now=now)
    assert transport.calls[0][2] == len(data)
    assert draft.image_sha256 == __import__("hashlib").sha256(data).hexdigest()
    MerchantEvent.model_validate(draft.record)


def test_cli_exits_2_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rv, "load_api_key", lambda: (_ for _ in ()).throw(rv.MissingApiKey("no key")))
    png = tmp_path / "r.png"
    png.write_bytes(tiny_png())
    assert rv.main([str(png)]) == 2
    assert json.loads(capsys.readouterr().err)["error"].startswith("MissingApiKey")


def test_gemma_probe_exits_2_without_key(monkeypatch, capsys):
    monkeypatch.setattr(gemma, "load_api_key", lambda: (_ for _ in ()).throw(gemma.MissingApiKey("no key")))
    assert gemma.main(["--probe"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["structured_output_ok"] is False and out["error"].startswith("MissingApiKey")


# --------------------------------------------------------------------------- gemma


def test_find_gemma_model_with_injected_names():
    assert gemma.find_gemma_model(["models/gemini-3.8-flash", "models/gemma-3-27b-it", "gemma-3n-e4b"]) == "gemma-3-27b-it"
    assert gemma.find_gemma_model(["gemini-3.8-flash", "gemini-4-argon"]) is None
    assert gemma.find_gemma_model([]) is None
    assert gemma.list_gemma_models(["models/Gemma-2-9b", "x"]) == ["Gemma-2-9b"]


def test_find_gemma_model_live_listing_failure_is_none(monkeypatch):
    monkeypatch.setattr(gemma, "load_api_key", lambda: (_ for _ in ()).throw(gemma.MissingApiKey("no key")))
    assert gemma.find_gemma_model() is None


def test_make_second_opinion_requires_a_gemma_name():
    fake = FakeClient({})
    assert gemma.make_second_opinion(fake, names=["gemini-3.8-flash"]) is None
    second = gemma.make_second_opinion(fake, names=["models/gemma-3-27b-it"])
    assert isinstance(second, gemma.SecondOpinion) and second.model_id == GEMMA
    assert second("curator", {}, MatcherOutput(entry={"record_id": "x", "revision": 1},
                                               decision="unmatched")) == (True, "not checked")


MATCHER_PRIMARY = {
    "entry": {"record_id": "txn_9001", "revision": 1},
    "candidates": [{"record_id": "po_5001", "revision": 1, "relationship": "exact",
                    "signals": ["amount equal"], "confidence": 0.9}],
    "decision": "linked",
    "chosen_record_id": "po_5001",
    "chosen_revision": 1,
    "rationale": "PO-5001 matches on amount and reference.",
}
MATCHER_INPUT = {"entry": {"record_id": "txn_9001", "revision": 1}, "candidates": []}


def test_second_opinion_matcher_agree_and_disagree():
    primary = parse_output("matcher", MATCHER_PRIMARY)
    fake = FakeClient({GEMMA: dict(MATCHER_PRIMARY)})
    second = gemma.SecondOpinion(fake, GEMMA, budget=CallBudget(3))
    agree, note = second("matcher", MATCHER_INPUT, primary)
    assert agree is True and GEMMA in note
    assert fake.calls[0].model == GEMMA
    assert json.loads(fake.calls[0].user) == MATCHER_INPUT

    other = dict(MATCHER_PRIMARY, decision="ambiguous", chosen_record_id="", chosen_revision=0)
    fake2 = FakeClient({GEMMA: other})
    agree, note = gemma.SecondOpinion(fake2, GEMMA)("matcher", MATCHER_INPUT, primary)
    assert agree is False and "disagrees" in note and GEMMA in note

    other_record = dict(MATCHER_PRIMARY, chosen_record_id="po_5002")
    agree, _ = gemma.SecondOpinion(FakeClient({GEMMA: other_record}), GEMMA)("matcher", MATCHER_INPUT, primary)
    assert agree is False
    assert len(second.log) == 1 and second.log[0]["agree"] is True and second.log[0]["role"] == "matcher"


def _detective(status: str, confirms: list[str], context: list[str] = ()) -> dict:
    refs = [{"record_id": r, "revision": 1, "role": "confirms"} for r in confirms]
    refs += [{"record_id": r, "revision": 1, "role": "context"} for r in context]
    return {
        "alerts": [],
        "claims": [{
            "claim_id": "clm_1", "question_id": "q1", "statement": "Settled by PO-5001.",
            "status": status, "refs": refs, "gaps": [], "next_checks": [],
        }],
    }


def test_second_opinion_detective_compares_status_and_confirming_refs():
    primary = parse_output("detective", _detective("supported", ["po_5001", "stl_88001"], ["msg_7001"]))

    same_but_other_context = _detective("supported", ["stl_88001", "po_5001"], [])
    agree, note = gemma.SecondOpinion(FakeClient({GEMMA: same_but_other_context}), GEMMA)(
        "detective", {"rfc": "rfc_a_001"}, primary)
    assert agree is True and GEMMA in note

    other_status = _detective("unresolved", ["po_5001", "stl_88001"])
    agree, note = gemma.SecondOpinion(FakeClient({GEMMA: other_status}), GEMMA)(
        "detective", {"rfc": "rfc_a_001"}, primary)
    assert agree is False and "q1" in note

    fewer_refs = _detective("supported", ["po_5001"])
    agree, _ = gemma.SecondOpinion(FakeClient({GEMMA: fewer_refs}), GEMMA)(
        "detective", {"rfc": "rfc_a_001"}, primary)
    assert agree is False


def test_second_opinion_unavailable_or_invalid_never_trusts():
    primary = parse_output("matcher", MATCHER_PRIMARY)
    down = gemma.SecondOpinion(FakeClient({GEMMA: Unavailable("simulated outage")}), GEMMA)
    agree, note = down("matcher", MATCHER_INPUT, primary)
    assert agree is False and note.startswith("second parser unavailable: Unavailable") and GEMMA in note
    assert down.log[0]["error"] == "Unavailable"

    spent = gemma.SecondOpinion(FakeClient({GEMMA: dict(MATCHER_PRIMARY)}), GEMMA, budget=CallBudget(0))
    agree, note = spent("matcher", MATCHER_INPUT, primary)
    assert agree is False and "BudgetExceeded" in note

    bad = dict(MATCHER_PRIMARY, decision="maybe")
    loose = FakeClient({GEMMA: bad}, validate=False)
    agree, note = gemma.SecondOpinion(loose, GEMMA)("matcher", MATCHER_INPUT, primary)
    assert agree is False and "ValidationError" in note


def test_compare_outputs_unknown_role():
    out = DetectiveOutput()
    assert gemma.compare_outputs("curator", out, out) == (False, "no comparison rule for role 'curator'")


# --------------------------------------------------------------------------- gemma transport


class _FakeGenai:
    """Records the config each generate_content call received."""

    def __init__(self):
        self.calls = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.calls.append((model, config))

        class _Resp:
            text = '{"ok": true}'
            usage_metadata = None

        return _Resp()


def test_thinking_aware_transport_omits_thinking_for_gemma_only():
    fake = _FakeGenai()
    call = gemma.thinking_aware_transport(api_key="unused", genai_client=fake)
    call("gemma-4-26b-a4b-it", "sys", "user", gemma.PROBE_SCHEMA, "low")
    call("gemini-3.8-flash", "sys", "user", gemma.PROBE_SCHEMA, "low")
    (gemma_model, gemma_cfg), (flash_model, flash_cfg) = fake.calls
    assert gemma.is_gemma(gemma_model) and not gemma.is_gemma(flash_model)
    assert gemma_cfg.thinking_config is None
    assert flash_cfg.thinking_config is not None
    assert gemma_cfg.response_json_schema == gemma.PROBE_SCHEMA


def test_live_client_uses_the_thinking_aware_transport(tmp_path):
    fake = _FakeGenai()
    client = gemma.GeminiClient(transport=gemma.thinking_aware_transport("k", genai_client=fake),
                                cache_dir=tmp_path / "cache", log_path=tmp_path / "log.jsonl",
                                budget=CallBudget(5), use_cache=False)
    result = client.generate("gemma-4-31b-it", "s", "u", gemma.PROBE_SCHEMA, CallBudget(1))
    assert result.data == {"ok": True}
    assert fake.calls[0][1].thinking_config is None
