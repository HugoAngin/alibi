"""The /rules parser chain (alibi.app.policy_chain): Gemini -> execution -> core, no network.

Gemini is exercised only through alibi.llm.fake.FakeClient; the conftest removes GEMINI_API_KEY
so the live layer is skipped unless a test injects a client or fakes the environment.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from agents.execution import policy_harness as harness
from agents.execution.api import load_policy_material, load_policy_rules
from alibi.app.policy_chain import ChainParse, gemini_enabled, gemini_skip_reason, parse_rule_text
from alibi.core.policy import RuleParse, compile_policy
from alibi.core.policy import parse_rule_text as core_parse
from alibi.core.schemas import AgeRule, DenyRule, GeneraliseRule, RecipientRule
from alibi.llm.client import Unavailable
from alibi.llm.fake import FakeClient

MATERIAL = load_policy_material()


def proposal(kind, target, value="", params=None, *, rule_id="g1", confidence=0.95, source_text="x"):
    return {"rule_id": rule_id, "kind": kind, "target": target, "value": value,
            "params": params or {}, "source_text": source_text, "confidence": confidence}


def answer(*rules, clarifications=(), summary="Summary."):
    return {"rules": list(rules), "clarifications": list(clarifications), "plain_summary": summary}


def gemini(text, script):
    """A FakeClient scripted for one text (the user JSON carries the text verbatim)."""
    return FakeClient({text: script})


# --------------------------------------------------------------------------- shape and wiring


def test_result_is_a_core_rule_parse_with_the_answering_layer():
    parsed = parse_rule_text("Don't show what I bought.")
    assert isinstance(parsed, ChainParse) and isinstance(parsed, RuleParse)
    assert parsed.status == "resolved" and parsed.source == "execution"
    assert parsed.tried == ["execution"] and "gemini" in parsed.skipped
    assert isinstance(parsed.rule, DenyRule) and [k.value for k in parsed.rule.kinds] == ["receipt"]
    assert parsed.rule.source_text == "Don't show what I bought."
    assert "purchase" in parsed.interpretation and "receipt" in parsed.interpretation
    assert compile_policy([parsed.rule]).rules  # the app can enforce it as is
    as_dict = parsed.as_dict()
    assert as_dict["source"] == "execution" and as_dict["rule"]["type"] == "deny"
    json.dumps(as_dict)


def test_lead_api_exposes_the_chain_and_keeps_every_other_export():
    """What alibi/tests/test_api.py::test_agents_lead_api_reexports_alibi_api must assert once
    its owner applies the diff in the policy track's report: every name is alibi.api's object
    except parse_rule_text, which is the chain."""
    import agents.lead.api as lead
    from alibi import api

    assert lead.parse_rule_text is parse_rule_text
    assert lead.parse_rule_text is not api.parse_rule_text
    assert list(lead.__all__) == list(api.__all__) and "parse_rule_text" in api.__all__
    for name in api.__all__:
        if name != "parse_rule_text":
            assert getattr(lead, name) is getattr(api, name), name


def test_execution_harness_adapter_reads_the_chain():
    parse = harness.rules_parser()
    out = parse("Nothing older than 90 days.", MATERIAL)
    assert out == {"rules": [{"kind": "age_limit", "target": "all", "value": "",
                              "params": {"max_age_days": 90}, "confidence": 0.95}], "clarifications": []}
    held = parse("share everything with everyone", MATERIAL)
    assert held["rules"] == [] and held["clarifications"][0]["question"].startswith("Policy rules can only restrict")


# --------------------------------------------------------------------------- layer order


def test_core_only_phrasing_falls_through_to_core():
    parsed = parse_rule_text("only share with Bank A")
    assert parsed.status == "resolved" and parsed.source == "core"
    assert parsed.tried == ["execution", "core"] and parsed.notes["execution"] == "compiled no rule"
    assert isinstance(parsed.rule, RecipientRule) and parsed.rule.allow == ["bank_a"]
    assert parsed.interpretation == core_parse("only share with Bank A").interpretation


def test_everyday_phrasings_resolve_through_execution():
    expect = {
        "Nothing older than 90 days.": (AgeRule, {"max_age_days": 90}),
        "Don't share my account number.": (DenyRule, {"fields": ["account_ref"]}),
        "Leave out the marketplace's messages.": (DenyRule, {"fields": ["text"]}),
        "Don't send my payment references to anyone.": (DenyRule, {"fields": ["payment_ref"]}),
        "dont share bank descriptions pls": (DenyRule, {"fields": ["description"]}),
        "Hide what the items were, just call them 'clothing item'.": (
            GeneraliseRule, {"fields": ["items.description"], "level": "category"}),
    }
    for text, (cls, attrs) in expect.items():
        parsed = parse_rule_text(text)
        assert parsed.status == "resolved" and parsed.source == "execution", text
        assert isinstance(parsed.rule, cls), text
        for key, value in attrs.items():
            assert getattr(parsed.rule, key) == value, (text, key)


def test_unresolved_everywhere_returns_the_core_reason_and_the_layers_tried():
    parsed = parse_rule_text("share everything with everyone")
    assert parsed.status == "unresolved" and parsed.rule is None and parsed.source == "core"
    assert parsed.reason.startswith("Policy rules can only restrict")
    assert parsed.tried == ["execution", "core"] and set(parsed.notes) == {"execution", "core"}
    assert parsed.interpretation == core_parse("share everything with everyone").interpretation


def test_not_expressible_in_core_is_held_with_a_plain_reason():
    parsed = parse_rule_text("Never share anything health related.")
    assert parsed.status == "unresolved" and parsed.rule is None
    assert parsed.notes["execution"].startswith("not expressible in core policy")
    assert "category 'health'" in parsed.reason and "cannot" in parsed.reason
    assert parsed.proposals and parsed.proposals[0]["target"] == "category"


def test_several_rules_in_one_text_are_held_not_truncated():
    parsed = parse_rule_text("Don't share my account number and never share my payment references.")
    assert parsed.status == "unresolved" and parsed.rule is None
    assert parsed.notes["execution"].startswith("compiled 2 rules")
    assert "Add each as its own rule" in parsed.reason
    assert len(parsed.proposals) == 2


def test_execution_clarification_question_reaches_the_holder():
    parsed = parse_rule_text("Keep it private.")
    assert parsed.status == "unresolved"
    assert parsed.reason.endswith("Which records or fields does this rule cover?")


def test_alert_preferences_are_not_disclosure_rules():
    parsed = parse_rule_text("Stop alerting me about big deposits.")
    assert parsed.status == "unresolved" and parsed.rule is None


def test_empty_and_non_string_text_are_unresolved():
    assert parse_rule_text("").status == "unresolved" and "empty" in parse_rule_text("   ").reason
    assert parse_rule_text(None).status == "unresolved"  # type: ignore[arg-type]


def test_rule_ids_are_deterministic_and_overridable():
    first = parse_rule_text("Don't show what I bought.")
    again = parse_rule_text("don't show what i bought")
    assert first.rule.rule_id == again.rule.rule_id
    assert first.rule.rule_id != parse_rule_text("Nothing older than 90 days.").rule.rule_id
    assert parse_rule_text("Nothing older than 90 days.", rule_id="r_age").rule.rule_id == "r_age"


# --------------------------------------------------------------------------- Gemini layer (FakeClient)


def test_gemini_answers_first_when_a_client_is_given():
    text = "Don't share my account number."
    client = gemini(text, answer(proposal("deny", "field", "account_ref")))
    parsed = parse_rule_text(text, client=client)
    assert parsed.status == "resolved" and parsed.source == "gemini" and parsed.tried == ["gemini"]
    assert parsed.rule == DenyRule(rule_id=parsed.rule.rule_id, source_text=text, fields=["account_ref"])
    assert len(client.calls) == 1 and parsed.skipped == {}


def test_gemini_clarification_falls_through_to_execution():
    text = "Nothing older than 90 days."
    client = gemini(text, answer(clarifications=[{"source_text": text, "question": "How many days?"}]))
    parsed = parse_rule_text(text, client=client)
    assert parsed.status == "resolved" and parsed.source == "execution"
    assert parsed.tried == ["gemini", "execution"]
    assert parsed.notes["gemini"] == "the model asked for clarification"
    assert isinstance(parsed.rule, AgeRule) and parsed.rule.max_age_days == 90


def test_gemini_low_confidence_or_outage_never_blocks_the_next_layer():
    text = "Nothing older than 90 days."
    low = gemini(text, answer(proposal("age_limit", "all", params={"max_age_days": 90}, confidence=0.5)))
    parsed = parse_rule_text(text, client=low)
    assert parsed.source == "execution" and "confidence" in parsed.notes["gemini"]
    down = gemini(text, Unavailable("simulated outage"))
    parsed = parse_rule_text(text, client=down)
    assert parsed.source == "execution" and parsed.notes["gemini"].startswith("Unavailable")


def test_gemini_rule_outside_core_falls_through_and_is_held():
    text = "Never share anything health related."
    client = gemini(text, answer(proposal("deny", "category", "health")))
    parsed = parse_rule_text(text, client=client)
    assert parsed.status == "unresolved" and parsed.tried == ["gemini", "execution", "core"]
    assert parsed.notes["gemini"].startswith("not expressible in core policy")


def test_gemini_cannot_widen_sharing():
    text = "Only Bank C can see my rent."
    # An invented recipient is dropped by the parser, so nothing is compiled.
    bad_recipient = gemini(text, answer(proposal("recipient_restrict", "category", "rent",
                                                 params={"recipients": ["bank_c"]})))
    parsed = parse_rule_text(text, client=bad_recipient)
    assert parsed.status == "unresolved" and "bank_c" in parsed.notes["gemini"]
    assert parsed.reason == core_parse(text).reason
    # Naming every configured recipient restricts nothing and is refused.
    everyone = gemini(text, answer(proposal("recipient_restrict", "all", params={"recipients": ["bank_a", "bank_b"]})))
    assert parse_rule_text(text, client=everyone).status == "unresolved"
    # A value outside the passport vocabulary is dropped, whatever the model's confidence.
    unknown = gemini(text, answer(proposal("deny", "field", "buyer_handle", confidence=1.0)))
    parsed = parse_rule_text(text, client=unknown)
    assert parsed.status == "unresolved" and "buyer_handle" in parsed.notes["gemini"]


def test_gemini_several_rules_are_held_like_executions():
    text = "Hide item names. Only Bank A sees item names."
    client = gemini(text, answer(proposal("deny", "field", "item", rule_id="a"),
                                 proposal("deny", "field", "account_ref", rule_id="b")))
    parsed = parse_rule_text(text, client=client)
    assert parsed.status == "unresolved" and parsed.notes["gemini"].startswith("compiled 2 rules")


# --------------------------------------------------------------------------- Gemini gating


def test_gemini_is_skipped_without_a_key_and_during_replay(monkeypatch):
    assert not gemini_enabled({})
    assert not gemini_enabled({"GEMINI_API_KEY": "   "})
    assert gemini_enabled({"GEMINI_API_KEY": "k"})
    assert not gemini_enabled({"GEMINI_API_KEY": "k", "ALIBI_REPLAY": "1"})
    assert not gemini_enabled({"GEMINI_API_KEY": "k", "ALIBI_REPLAY": "true"})
    assert gemini_enabled({"GEMINI_API_KEY": "k", "ALIBI_REPLAY": "0"})
    assert gemini_skip_reason({"GEMINI_API_KEY": "k"}) is None
    assert "ALIBI_REPLAY" in gemini_skip_reason({"GEMINI_API_KEY": "k", "ALIBI_REPLAY": "1"})
    assert "export" in gemini_skip_reason({})

    def boom(*args, **kwargs):
        raise AssertionError("GeminiClient must not be built without a key")

    monkeypatch.setattr("alibi.llm.client.GeminiClient", boom)
    parsed = parse_rule_text("Nothing older than 90 days.", use_gemini=True)
    assert "gemini" not in parsed.tried and "GEMINI_API_KEY" in parsed.skipped["gemini"]
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ALIBI_REPLAY", "1")
    parsed = parse_rule_text("Nothing older than 90 days.", use_gemini=True)
    assert "gemini" not in parsed.tried and parsed.source == "execution"
    assert "ALIBI_REPLAY" in parsed.skipped["gemini"], "use_gemini=True still never calls Gemini during replay"


def test_a_default_call_never_builds_a_client_even_with_an_exported_key(monkeypatch):
    """Library callers (agents.lead.api, execution's harness and tests) get the deterministic
    layers whatever the shell exports; only a caller that asks (the app) reaches Gemini."""
    import agents.lead.api as lead

    def boom(*args, **kwargs):
        raise AssertionError("a default parse_rule_text call must not build a GeminiClient")

    monkeypatch.setattr("alibi.llm.client.GeminiClient", boom)
    monkeypatch.setenv("GEMINI_API_KEY", "exported-in-the-shell")
    monkeypatch.delenv("ALIBI_REPLAY", raising=False)
    assert gemini_enabled()
    for call in (parse_rule_text, lead.parse_rule_text):
        parsed = call("Nothing older than 90 days.")
        assert parsed.source == "execution" and "gemini" not in parsed.tried
        assert parsed.skipped["gemini"].startswith("not requested")
    scored = harness.rules_parser()("Nothing older than 90 days.", MATERIAL)
    assert scored["rules"] and scored["rules"][0]["kind"] == "age_limit"


def test_a_key_only_in_dot_env_is_not_read_and_the_skip_reason_says_to_export_it(monkeypatch, tmp_path):
    """The chain's gate is the exported variable, not the repo-root .env that
    alibi.llm.client.load_api_key falls back to, so the decision never flips when another code
    path loads the file mid-run; when it skips for that reason the holder is told what to do."""
    from alibi.llm import config as llm_config

    (tmp_path / ".env").write_text("GEMINI_API_KEY=only-in-the-file\n", encoding="utf-8")
    monkeypatch.setattr(llm_config, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ALIBI_REPLAY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("GeminiClient must not be built from a .env-only key")

    monkeypatch.setattr("alibi.llm.client.GeminiClient", boom)
    parsed = parse_rule_text("Nothing older than 90 days.", use_gemini=True)
    assert parsed.source == "execution" and "gemini" not in parsed.tried
    assert "not exported" in parsed.skipped["gemini"] and "export the key" in parsed.skipped["gemini"]
    assert "GEMINI_API_KEY" not in os.environ, "the chain must not load the key as a side effect"


def test_gemini_runs_with_a_key_through_the_client_class(monkeypatch):
    text = "Don't share my account number."
    built = []

    def fake_client_class(*args, **kwargs):
        client = gemini(text, answer(proposal("deny", "field", "account_ref")))
        built.append(client)
        return client

    monkeypatch.setattr("alibi.llm.client.GeminiClient", fake_client_class)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("ALIBI_REPLAY", raising=False)
    parsed = parse_rule_text(text, use_gemini=True)
    assert parsed.source == "gemini" and len(built) == 1 and len(built[0].calls) == 1
    off = parse_rule_text(text, use_gemini=False)
    assert off.source == "execution" and off.skipped["gemini"] == "switched off" and len(built) == 1


def _app_client(tmp_path, **overrides):
    from fastapi.testclient import TestClient

    from alibi.app.config import settings_from_env
    from alibi.app.main import create_app

    values = {"case_number": 1, "data_dir": tmp_path / "app", "key_dir": tmp_path / "keys",
              "fixed_now": datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc), **overrides}
    return TestClient(create_app(settings_from_env({}, **values)))


def test_the_rules_route_asks_for_gemini_and_shows_why_it_did_not_run(monkeypatch, tmp_path):
    """POST /rules/interpret is the one caller that opts in; without an exported key the answer
    and the audit trail say the layer was skipped and why, and in replay it is switched off."""
    json_headers = {"accept": "application/json"}
    monkeypatch.setattr("alibi.llm.client.GeminiClient",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client in tests")))
    client = _app_client(tmp_path)
    with client:
        answer = client.post("/rules/interpret", data={"text": "don't share item names"}, headers=json_headers).json()
        assert answer["status"] == "resolved" and answer["source"] == "execution"
        assert answer["layers"] == ["execution"] and "not exported" in answer["skipped"]["gemini"]
        pending = client.get(f"/rules?interp={answer['rule_id']}", headers=json_headers).json()["interpretation"]
        assert pending["source"] == "execution" and pending["skipped"] == answer["skipped"]
        held = client.post("/rules/interpret", data={"text": "share everything with everyone"}, headers=json_headers).json()
        assert held["status"] == "held" and held["layers"] == ["execution", "core"]
        assert "not exported" in held["skipped"]["gemini"]
        assert client.get("/rules", headers=json_headers).json()["held_rules"][0]["skipped"] == held["skipped"]
        audit = client.get("/audit", headers=json_headers).json()
        details = json.dumps(audit)
        assert "skipped gemini" in details and "by the execution parser" in details
    replay = _app_client(tmp_path / "replay", replay=True)
    with replay:
        answer = replay.post("/rules/interpret", data={"text": "don't share item names"}, headers=json_headers).json()
        assert answer["status"] == "resolved" and answer["skipped"]["gemini"] == "switched off"


# --------------------------------------------------------------------------- safety over the fixtures


def test_chain_never_resolves_a_text_the_fixture_says_needs_clarifying():
    for case in load_policy_rules():
        parsed = parse_rule_text(case["text"])
        assert isinstance(parsed, ChainParse), case["id"]
        if case["outcome"] == "clarify" and not case["alternatives"]:
            assert parsed.status == "unresolved", (case["id"], parsed.rule)
        if "injection" in case["tags"]:
            assert parsed.status == "unresolved", case["id"]
        if parsed.status == "resolved":
            assert compile_policy([parsed.rule]).rules, case["id"]


def test_harness_scores_the_chain_without_errors_or_disclosure_leaks():
    """Every verdict execution's `rules` adapter flags as a leak must be a name-translation
    artefact of that adapter (core `text`, `payment_ref`, `items.description`, `receipt` read
    back as team names), never a rule that widens disclosure: the compiled core rule covers the
    expected restriction once names are translated, which is what the committed adapter in
    agents/lead/out/policy_harness_chain.py does."""
    from agents.lead.out import policy_harness_chain as chain_harness

    result = harness.evaluate(harness.rules_parser())
    metrics = result["metrics"]
    assert metrics["errors"] == 0 and metrics["correct"] > 18
    flagged = {row["id"] for row in result["rows"] if row["leak"]}
    assert flagged <= {"PR-01", "PR-05", "PR-09", "PR-12"}, flagged
    for case in load_policy_rules():
        if case["id"] not in flagged:
            continue
        parsed = parse_rule_text(case["text"])
        assert parsed.status == "resolved", case["id"]
        translated = chain_harness.core_rule_to_fixture(parsed.rule.model_dump(mode="json"))
        assert {r["value"] for r in case["expected_rules"]} <= {r["value"] for r in translated}, (case["id"], translated)


def test_committed_translating_adapter_reports_zero_leaks_and_is_reproducible(capsys):
    """agents/lead/out/policy_harness_chain.py is the committed command behind the recorded
    chain numbers: same scoring as execution's harness, team names restored, no Gemini."""
    from agents.lead.out import policy_harness_chain as chain_harness

    assert chain_harness.core_rule_to_fixture(
        {"type": "deny", "kinds": ["receipt"], "fields": []}) == [
        {"kind": "deny", "target": "record_kind", "value": "purchase", "params": {}}]
    assert chain_harness.core_rule_to_fixture(
        {"type": "deny", "kinds": [], "fields": ["text", "payment_ref"]}) == [
        {"kind": "deny", "target": "field", "value": "message", "params": {}},
        {"kind": "deny", "target": "field", "value": "payment_reference", "params": {}}]
    assert chain_harness.core_rule_to_fixture(
        {"type": "generalise", "kinds": [], "fields": ["items.description"], "level": "category"}) == [
        {"kind": "generalise", "target": "field", "value": "item", "params": {}}]

    fixture = chain_harness.main([])
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[-1]) == {f"chain_{k}": v for k, v in fixture.items() if k != "leak_ids"}
    assert (fixture["leaks"], fixture["invalid"], fixture["errors"], fixture["leak_ids"]) == (0, 0, 0, [])
    assert fixture["cases"] == 40 and fixture["correct"] >= 25
    holdout = chain_harness.main(["--holdout"])
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[-1]) == {f"chain_holdout_{k}": v for k, v in holdout.items() if k != "leak_ids"}
    assert (holdout["leaks"], holdout["invalid"], holdout["errors"]) == (0, 0, 0)
    assert holdout["cases"] == 12 and holdout["correct"] >= 6


def test_generalise_with_a_label_tells_the_holder_categories_are_shown_instead():
    """PR-05: core can only generalise item names to their category, not to the holder's flat
    label; the interpretation the holder confirms says so and names the label it cannot use."""
    parsed = parse_rule_text("Hide what the items were, just call them 'clothing item'.")
    assert isinstance(parsed.rule, GeneraliseRule) and parsed.rule.level == "category"
    assert "replaced by their categories" in parsed.interpretation
    assert "not by 'clothing item'" in parsed.interpretation
    assert "name withheld" in parsed.interpretation
