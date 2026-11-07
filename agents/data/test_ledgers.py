"""Checks the development ledgers: determinism, contracts, and gold re-derived independently.

    uv run --frozen python -m pytest agents/data

The reference replay re-derives each case's gold from what is observable at its decision
time (deliveries, imports and lookups available by the first RFC), using the rules in
agents/data/README.md "Gold rules", so a slip in the generator shows up here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from agents.data import api, ledgers

CASES = api.load_cases()
KEY = {"sale": "sales", "refund": "refunds", "fee": "fees"}


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ingest(entries: list[dict], key: str, ident: str, connector: str, active: dict | None):
    """Idempotency by connector and ID; stale revisions never replace the active one."""
    seen: dict[str, str] = {}
    out = []
    for i, d in enumerate(entries):
        obj = d[key]
        raw = json.dumps(obj, sort_keys=True)
        if obj[ident] in seen:
            outcome = "duplicate_ignored" if seen[obj[ident]] == raw else "rejected_conflict"
        else:
            seen[obj[ident]] = raw
            outcome = "accepted"
            if active is not None:
                cur = active.get(obj["record_id"])
                if cur is not None and obj["revision"] <= cur[0]["revision"]:
                    outcome = "stale_revision_ignored"
                else:
                    active[obj["record_id"]] = (obj, connector)
        out.append((connector, i, outcome))
    return out


def replay(view: dict):
    active: dict[str, tuple[dict, str]] = {}
    outcomes = ingest(view["merchant_deliveries"], "event", "event_id", "merchant", active)
    outcomes += ingest(view["imports"], "record", "event_id", "import", active)
    for d in view.get("merchant_lookup", []):
        ev = d["event"]
        if ev["record_id"] not in active or ev["revision"] > active[ev["record_id"]][0]["revision"]:
            active[ev["record_id"]] = (ev, "merchant")
    outcomes += ingest(view["bank_deliveries"], "row", "txn_id", "bank", None)
    bank = {}
    for d in view["bank_deliveries"]:
        bank.setdefault(d["row"]["txn_id"], d["row"])
    return outcomes, active, bank


def candidates(credit: dict, active: dict) -> list[list[dict]]:
    booked = ts(credit["booked_at"])
    payouts = [e for e, _ in active.values() if e["kind"] == "payout" and e["destination"] == "bank"
               and e["status"] == "settled"]
    batches: dict[str, list[dict]] = {}
    groups = []
    for p in payouts:
        if p.get("settlement_batch_id"):
            batches.setdefault(p["settlement_batch_id"], []).append(p)
        else:
            groups.append((p["payout_reference"], [p]))
    groups += list(batches.items())
    out = []
    for ref, group in groups:
        last = max(ts(p["settled_at"]) for p in group)
        if (sum(p["amount_minor"] for p in group) == credit["amount_minor"]
                and last <= booked <= last + timedelta(days=ledgers.SETTLEMENT_WINDOW_DAYS)
                and (credit["payment_reference"] is None or credit["payment_reference"] == ref
                     or credit["payment_reference"] in {p.get("withdrawal_id") for p in group})):
            out.append(group)
    return out


def ref(record_id: str, active: dict) -> str:
    return f"{'import' if active[record_id][1] == 'import' else 'merchant'}:{record_id}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_ingest_outcomes(case):
    outcomes, _, _ = replay(case)
    assert [(g["connector"], g["index"], g["outcome"]) for g in case["gold"]["ingest"]] == outcomes


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_gold_rederived_at_decision_time(case):
    gold = case["gold"]
    assert gold["decision_time"] == case["rfcs"][0]["issued_at"]
    _, active, bank = replay(api.known_at(case, gold["decision_time"], include_lookup=True))
    claim = case["holder_claim"]
    credit = bank[claim["txn_id"]]
    cands = candidates(credit, active)
    state = {0: "unmatched", 1: "linked"}.get(len(cands), "ambiguous")
    [link] = gold["links"]
    assert link["state"] == state
    breakdown, covered = None, []
    if state == "linked":
        payouts = cands[0]
        assert sorted(p["record_id"] for p in payouts) == sorted(link["payout_record_ids"])
        covered = [r for p in payouts for r in p["covers_record_ids"]]
        if all(r in active for r in covered):
            breakdown = {"sales": 0, "refunds": 0, "fees": 0}
            for r in covered:
                breakdown[KEY[active[r][0]["kind"]]] += active[r][0]["amount_minor"]
            breakdown["net"] = breakdown["sales"] - breakdown["refunds"] - breakdown["fees"]
            assert breakdown["net"] == credit["amount_minor"]
    assert gold["breakdown_minor"] == breakdown
    if breakdown is not None:
        expect = "supported" if claim["stated_minor"] == breakdown else "contradicted"
    else:
        cited = [e for e, _ in active.values() if e["kind"] == "payout"
                 and claim["payout_reference"] and e["payout_reference"] == claim["payout_reference"]]
        conflicts = any(e["status"] != "settled" or ts(e["settled_at"]) > ts(credit["booked_at"])
                        for e in cited)
        expect = "contradicted" if conflicts else "unresolved"
    assert gold["claim_status"] == expect

    # Per-question answers for the first request.
    ans = gold["answers"][case["rfcs"][0]["request_id"]]
    src, brk = ans[ledgers.Q_SOURCE], ans[ledgers.Q_BREAKDOWN]
    txn = f"bank:{credit['txn_id']}"
    if state == "linked":
        po = [ref(p["record_id"], active) for p in cands[0]]
        assert src["status"] == "supported" and src["evidence_sets"] == [[txn] + po]
    else:
        assert src["status"] == "unresolved" and src["evidence_sets"] == [[]] and src["gap"]
    if breakdown is not None:
        assert brk["status"] == expect
        assert sorted(brk["evidence_sets"][0]) == sorted([txn] + po + [ref(r, active) for r in covered])
    else:
        assert brk["status"] == "unresolved" and brk["gap"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_refs_trust_and_retrieval(case):
    gold = case["gold"]
    _, active, bank = replay(api.known_at(case, case["rfcs"][-1]["issued_at"], include_lookup=True))
    lookup_ids = {d["event"]["record_id"] for d in case["merchant_lookup"]}
    used = set()
    for answers in gold["answers"].values():
        for q in answers.values():
            for s in q["evidence_sets"]:
                used |= set(s)
    assert not used & set(gold["forbidden_refs"]), "forbidden item in an evidence set"
    for r in used | set(gold["forbidden_refs"]):
        kind, ident = r.split(":", 1)
        assert ident in (bank if kind == "bank" else active), r
        expect = {"bank": "bank_observed", "import": "self_supplied", "merchant": "issuer_verified"}[kind]
        assert gold["source_trust"][r] == expect
    assert gold["requires_retrieval"] == sorted(r.split(":", 1)[1] for r in used
                                                if r.split(":", 1)[1] in lookup_ids)
    for r in gold["forbidden_refs"]:
        kind, ident = r.split(":", 1)
        if kind == "bank":
            assert bank[ident]["amount_minor"] < 0
        else:
            assert active[ident][0]["kind"] == "purchase"


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"case{c['case_number']}")
def test_contract_shapes(case):
    assert 1 <= case["case_number"] < 1000 and case["label"].startswith("SYNTHETIC")
    events = [(d["received_at"], d["event"]) for d in case["merchant_deliveries"]] + \
             [(d["available_from"], d["event"]) for d in case["merchant_lookup"]] + \
             [(d["received_at"], d["record"]) for d in case["imports"]]
    for received, ev in events:
        for k in ("schema_version", "event_id", "record_id", "revision", "issuer_id",
                  "subject_ref", "kind", "occurred_at"):
            assert k in ev
        if ev["kind"] in ("notice", "statement"):
            assert "amount_minor" not in ev and ev["text"]
        else:
            assert isinstance(ev["amount_minor"], int) and ev["amount_minor"] > 0
            assert ev["currency"] == "GBP"
        assert ev["occurred_at"].endswith("Z")
        assert ts(received) >= ts(ev["occurred_at"])
        if ev["kind"] == "payout" and ev["status"] == "settled":
            assert ts(ev["settled_at"]) >= ts(ev["occurred_at"])
    for d in case["bank_deliveries"]:
        row = d["row"]
        assert isinstance(row["amount_minor"], int) and row["currency"] == "GBP"
        assert ts(d["received_at"]) >= ts(row["booked_at"])
    for r in case["rfcs"]:
        assert r["audience"] in ("bank_a", "bank_b") and ts(r["expires_at"]) > ts(r["issued_at"])
        assert [q["question_id"] for q in r["questions"]] == r["question_ids"]
        assert bool(r["txn_ids"]) != bool(r["period"])


def test_demo_pair():
    control, pending = api.load_case(1), api.load_case(2)
    for case in (control, pending):
        kinds = [d["event"]["kind"] for d in case["merchant_deliveries"]]
        assert "balance_release" in kinds and "payout" not in kinds, "payout must need a lookup"
        assert [r["audience"] for r in case["rfcs"]] == ["bank_a", "bank_b"]
        period = case["gold"]["answers"][case["rfcs"][1]["request_id"]][ledgers.Q_PERIOD]
        assert period["facts"] == {"sales_minor": 240000, "refunds_minor": 18000,
                                   "sale_count": 8, "refund_count": 2}
    assert control["gold"]["breakdown_minor"] == {"sales": 240000, "refunds": 18000,
                                                  "fees": 6000, "net": 216000}
    assert control["gold"]["claim_status"] == "supported"
    assert pending["gold"]["claim_status"] == "contradicted"
    assert pending["gold"]["answers"]["rfc-0002-01"][ledgers.Q_SOURCE]["status"] == "unresolved"
    # The settlement is reachable only through lookup().
    at = control["rfcs"][0]["issued_at"]
    assert api.lookup(control, at, payout_reference="PO-0001-01")[0]["status"] == "settled"
    assert api.known_at(control, at)["merchant_lookup"] == []


def test_period_answer_recomputed():
    case = api.canonical_case()
    rfc = case["rfcs"][1]
    _, active, _ = replay(api.known_at(case, rfc["issued_at"], include_lookup=True))
    lo, hi = ts(rfc["period"]["from"]), ts(rfc["period"]["to"])
    inside = [e for e, _ in active.values() if e["kind"] in ("sale", "refund")
              and lo <= ts(e["occurred_at"]) < hi]
    facts = case["gold"]["answers"][rfc["request_id"]][ledgers.Q_PERIOD]["facts"]
    assert facts["sales_minor"] == sum(e["amount_minor"] for e in inside if e["kind"] == "sale")
    assert facts["refunds_minor"] == sum(e["amount_minor"] for e in inside if e["kind"] == "refund")


def test_committed_json_matches_generator():
    built = ledgers.build_cases()
    assert [c["case_number"] for c in built] == list(range(1, len(built) + 1))
    assert built == CASES, "out/ is stale: run uv run --frozen python -m agents.data.ledgers"


def test_coverage():
    assert len(CASES) == 50
    assert {c["family"] for c in CASES} == set(api.FAMILIES)
    assert {c["gold"]["claim_status"] for c in CASES} == {"supported", "contradicted", "unresolved"}
    assert {l["state"] for c in CASES for l in c["gold"]["links"]} == {"linked", "unmatched", "ambiguous"}
    assert any(c["imports"] for c in CASES) and any(c["gold"]["requires_retrieval"] for c in CASES)


def test_api_guards():
    assert "gold" not in api.without_gold(CASES[0]) and "gold" in CASES[0]
    with pytest.raises(KeyError):
        api.load_case(1000)
    with pytest.raises(ValueError):
        api.load_cases("nope")


# --------------------------------------------------------------------------
# hidden_reference family (cases 41-50)
# --------------------------------------------------------------------------

HIDDEN = [c for c in CASES if c["family"] == "hidden_reference"]
PO_REF = re.compile(r"PO-\d{4}-\d{2}")


def naive_regex_probe(case: dict) -> str:
    """Not the team's baseline: the simplest retrieval that solved all 40 original cases.
    Pull every canonical PO reference out of pushed or imported text and bank descriptions,
    look each one up, then apply the pinned candidate rule. Returns the predicted link state."""
    at = case["rfcs"][0]["issued_at"]
    view = api.known_at(case, at)
    texts = [json.dumps(d["event"]) for d in view["merchant_deliveries"]]
    texts += [json.dumps(d["record"]) for d in view["imports"]]
    texts += [d["row"]["description"] for d in view["bank_deliveries"]]
    refs = sorted({r for t in texts for r in PO_REF.findall(t)})
    view["merchant_lookup"] = [{"available_from": at, "event": ev} for r in refs
                               for ev in api.lookup(case, at, payout_reference=r)]
    _, active, bank = replay(view)
    cands = candidates(bank[case["holder_claim"]["txn_id"]], active)
    return {0: "unmatched", 1: "linked"}.get(len(cands), "ambiguous")


def test_original_forty_unchanged_and_regex_solvable():
    """Lead's finding: regex retrieval gets every original case's link right."""
    original = [c for c in CASES if c["case_number"] <= 40]
    assert len(original) == 40
    assert all(naive_regex_probe(c) == c["gold"]["links"][0]["state"] for c in original)


def test_hidden_reference_is_harder_than_regex():
    right = [naive_regex_probe(c) == c["gold"]["links"][0]["state"] for c in HIDDEN]
    assert len(HIDDEN) >= 8
    assert 0 < sum(right) < len(HIDDEN), f"regex probe got {sum(right)}/{len(HIDDEN)}"


@pytest.mark.parametrize("case", HIDDEN, ids=lambda c: f"case{c['case_number']}")
def test_hidden_reference_retrieval_path(case):
    gold = case["gold"]
    at = gold["decision_time"]
    [link] = gold["links"]
    assert link["state"] == "linked" and gold["requires_retrieval"] == link["payout_record_ids"]
    # The deciding payout is not visible without retrieval ...
    pushed = api.known_at(case, at)
    assert all(d["event"]["record_id"] not in link["payout_record_ids"] for d in pushed["merchant_deliveries"])
    # ... the documented path retrieves it ...
    found = {ev["record_id"] for step in gold["retrieval_path"] for ev in api.lookup(case, at, **step)}
    assert set(link["payout_record_ids"]) <= found
    # ... and the clue that points there was observable at decision time.
    visible = {f"merchant:{d['event']['record_id']}" for d in pushed["merchant_deliveries"]}
    visible |= {f"import:{d['record']['record_id']}" for d in pushed["imports"]}
    assert set(gold["clue_refs"]) <= visible
    # The canonical reference never appears verbatim where the path does not need it.
    ref = next(ev for ev in api.lookup(case, at, record_id=link["payout_record_ids"][0]))["payout_reference"]
    if case["variant"] in ("orders_and_weekday", "date_only", "email_layout", "csv_layout"):
        blob = json.dumps(pushed)
        assert ref not in blob


def test_lookup_keys():
    case = HIDDEN[0]
    at = case["gold"]["decision_time"]
    with pytest.raises(ValueError):
        api.lookup(case, at)
    with pytest.raises(ValueError):
        api.lookup(case, at, payout_reference="x", record_id="y")
    assert api.lookup(case, at, payout_reference="PO-9999-99") == []
