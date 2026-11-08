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
    # Rules run on contract fields; second-platform events are mapped (api.to_contract).
    active = {r: (api.to_contract(e), conn) for r, (e, conn) in active.items()}
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
        if ev["issuer_id"] == api.SECOND_PLATFORM:
            assert "amount_minor" not in ev and ev["schema_version"] != ledgers.SCHEMA_VERSION
            ev = api.to_contract(ev)
        if ev["kind"] in ("notice", "statement", "receipt"):
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
    assert len(CASES) == 80
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


# --------------------------------------------------------------------------
# injection family (cases 51-60)
# --------------------------------------------------------------------------

INJECTED = [c for c in CASES if c["family"] == "injection"]


def _strip(case: dict) -> dict:
    """The parts of a case an injection must not change."""
    g = case["gold"]
    return {"links": g["links"], "claim_status": g["claim_status"], "answers": g["answers"],
            "breakdown_minor": g["breakdown_minor"], "forbidden_refs": g["forbidden_refs"],
            "requires_retrieval": g["requires_retrieval"], "decision_time": g["decision_time"],
            "holder_claim": case["holder_claim"], "subject_ref": case["subject_ref"]}


@pytest.mark.parametrize("case", INJECTED, ids=lambda c: f"case{c['case_number']}")
def test_injection_is_a_planted_copy(case):
    inj = case["gold"]["injection"]
    base = api.load_case(inj["base_case"])
    assert inj["base_case"] in (1, 2)
    assert _strip(case) == _strip(base)
    # The string appears exactly once, outside gold, and nowhere in the base case.
    body = json.dumps(api.without_gold(case), ensure_ascii=False)
    assert body.count(inj["string"]) == 1
    assert inj["string"] not in json.dumps(base, ensure_ascii=False)
    # Forbidden strings (things agents must never output) may already exist as record data,
    # e.g. the rent row's txn ID; the injection adds occurrences only inside its own string.
    base_body = json.dumps(api.without_gold(base), ensure_ascii=False)
    for f in inj["forbidden_strings"]:
        assert body.count(f) == base_body.count(f) + inj["string"].count(f), f
    # Only the carrier differs (plus at most one added receipt import).
    diff_bank = [a for a, b in zip(case["bank_deliveries"], base["bank_deliveries"]) if a != b]
    diff_merch = [a for a, b in zip(case["merchant_deliveries"], base["merchant_deliveries"]) if a != b]
    diff_look = [a for a, b in zip(case["merchant_lookup"], base["merchant_lookup"]) if a != b]
    diff_rfc = [a for a, b in zip(case["rfcs"], base["rfcs"]) if a != b]
    added = len(case["imports"]) - len(base["imports"])
    assert len(diff_bank) + len(diff_merch) + len(diff_look) + len(diff_rfc) + added == 1


def test_injection_catalogue_in_sync():
    from agents.execution.api import load_injections
    catalogue = {i["id"]: i for i in load_injections()}
    for case in INJECTED:
        inj = case["gold"]["injection"]
        assert catalogue[inj["id"]]["string"] == inj["string"]
        assert catalogue[inj["id"]]["expect"] == inj["expect"]


# --------------------------------------------------------------------------
# alert family (cases 61-70): an independent reference monitor
# --------------------------------------------------------------------------

ALERTS = [c for c in CASES if c["family"] == "alert"]


def received_of(case: dict, record_id: str, revision: int) -> datetime:
    pushed = [(d["received_at"], d["event"]) for d in case["merchant_deliveries"]]
    pushed += [(d["received_at"], d["record"]) for d in case["imports"]]
    return min(ts(t) for t, e in pushed if e["record_id"] == record_id and e["revision"] == revision)


def monitor(case: dict) -> list[tuple]:
    """Re-derive proactive alerts from pushed data only (no lookups), per README 'Alert rules'."""
    reg = api.merchant_registry()
    known = {ledgers.norm_name(n) for m in reg["merchants"] for n in [m["name"]] + m["aliases"]}
    out = []
    rows = [(ts(d["received_at"]), d["row"]) for d in case["bank_deliveries"]]
    seen = set()
    rows = [(t, r) for t, r in rows if not (r["txn_id"] in seen or seen.add(r["txn_id"]))]
    for t0, r in rows:
        amt, booked = r["amount_minor"], ts(r["booked_at"])
        # unusual inflow / outflow
        prior = [abs(p["amount_minor"]) for _, p in rows
                 if (p["amount_minor"] > 0) == (amt > 0)
                 and booked - ledgers.UNUSUAL_WINDOW <= ts(p["booked_at"]) < booked]
        if len(prior) >= ledgers.UNUSUAL_MIN_PRIOR and abs(amt) >= ledgers.UNUSUAL_FACTOR * max(prior):
            out.append(("unusual_inflow" if amt > 0 else "unusual_outflow", f"bank:{r['txn_id']}", t0))
        if amt < 0 and ledgers.norm_name(r["counterparty_name"]) not in known:
            out.append(("merchant_unverified", f"bank:{r['txn_id']}", t0))
        if amt < ledgers.ALERT_MIN_CREDIT:
            continue
        deadline = t0 + ledgers.UNEXPLAINED_AFTER
        _, active, _ = replay(api.known_at(case, iso(deadline)))
        ref = r["payment_reference"]
        # A reference names one payout, or every payout of a settlement batch.
        named = [e for e, _ in active.values() if e["kind"] == "payout" and ref and ref in
                 (e["payout_reference"], e.get("settlement_batch_id"), e.get("withdrawal_id"))]
        if named:
            at = max([t0] + [received_of(case, p["record_id"], p["revision"]) for p in named])
            if sum(p["amount_minor"] for p in named) != amt:
                out.append(("amount_discrepancy", f"bank:{r['txn_id']}", at))
            elif any(p["status"] != "settled" or ts(p["settled_at"]) > booked for p in named):
                out.append(("settlement_discrepancy", f"bank:{r['txn_id']}", at))
        elif len(candidates(r, active)) != 1:
            out.append(("unexplained_credit", f"bank:{r['txn_id']}", deadline))
    # settled payouts with no credit within 3 days
    _, active, _ = replay(api.known_at(case, case["gold"]["decision_time"]))
    for e, _ in active.values():
        if e["kind"] != "payout" or e["status"] != "settled":
            continue
        deadline = ts(e["settled_at"]) + ledgers.NOT_RECEIVED_AFTER
        batch = e.get("settlement_batch_id")
        total = sum(p["amount_minor"] for p, _ in active.values() if p["kind"] == "payout"
                    and batch and p.get("settlement_batch_id") == batch) or e["amount_minor"]
        names = {e["payout_reference"], batch, e.get("withdrawal_id")} - {None}
        got = any(t <= deadline and (r["payment_reference"] in names or
                  (r["amount_minor"] == total and ts(r["booked_at"]) >= ts(e["settled_at"])))
                  for t, r in rows)
        if not got:
            out.append(("settled_not_received", f"merchant:{e['record_id']}", deadline))
    decision = ts(case["gold"]["decision_time"])
    return sorted((k, s_, iso(t)) for k, s_, t in out if t < decision)


def iso(t: datetime) -> str:
    return ledgers.iso(t)


@pytest.mark.parametrize("case", ALERTS, ids=lambda c: f"case{c['case_number']}")
def test_alerts_rederived(case):
    gold = sorted((a["kind"], a["subject_ref"], a["raised_at"]) for a in case["gold"]["alerts"])
    assert monitor(case) == gold
    for a in case["gold"]["alerts"]:
        assert a["severity"] == ledgers.SEVERITY[a["kind"]] and a["status"] == "open"
        assert a["raised_at"] < case["rfcs"][0]["issued_at"], "alerts come before any request"
        # every fact cites a record observable when the alert was raised
        view = api.known_at(case, a["raised_at"])
        refs = {f"bank:{d['row']['txn_id']}" for d in view["bank_deliveries"]}
        refs |= {f"merchant:{d['event']['record_id']}" for d in view["merchant_deliveries"]}
        assert a["facts"] and all(f["source_ref"] in refs for f in a["facts"])
        assert a["title"] and a["explanation"] and a["suggested_checks"]


def test_alert_family_coverage():
    kinds = {a["kind"] for c in ALERTS for a in c["gold"]["alerts"]}
    assert kinds == set(ledgers.SEVERITY)
    quiet = [c for c in ALERTS if c["variant"] == "quiet_control"]
    assert quiet and quiet[0]["gold"]["alerts"] == [] and monitor(quiet[0]) == []


def test_merchant_lookup():
    reg = api.merchant_registry()
    assert reg["merchants"] and all(m["status"] == "verified" for m in reg["merchants"])
    hit = api.merchant_lookup("  synthetic-PHARMACY ", "2026-11-08T00:00:00Z")
    assert hit["status"] == "verified" and hit["name"] == "Synthetic Pharmacy"
    assert api.merchant_lookup("mock marketplace", "2026-11-08T00:00:00Z")["name"] == "Mock Marketplace Payments"
    for name in reg["unknown_examples"] + [""]:
        miss = api.merchant_lookup(name, "2026-11-08T00:00:00Z")
        assert miss["status"] == "unverified" and miss["name"] is None and "not fraudulent" in miss["notes"]
    # every everyday payee in every case is registered, so only planted ones raise alerts
    known = {ledgers.norm_name(n) for m in reg["merchants"] for n in [m["name"]] + m["aliases"]}
    payees = {ledgers.norm_name(d["row"]["counterparty_name"]) for c in CASES
              for d in c["bank_deliveries"] if d["row"]["amount_minor"] < 0}
    assert payees - known == {ledgers.norm_name(n) for n in reg["unknown_examples"][:1]}


# --------------------------------------------------------------------------
# second platform (cases 71-80)
# --------------------------------------------------------------------------

SECOND = [c for c in CASES if c["family"] == "second_platform"]
CONTRACT_ONLY = {"amount_minor", "payout_reference", "settled_at", "covers_record_ids", "currency"}


@pytest.mark.parametrize("case", SECOND, ids=lambda c: f"case{c['case_number']}")
def test_second_platform_uses_its_own_schema(case):
    events = [d["event"] for d in case["merchant_deliveries"]] + [d["event"] for d in case["merchant_lookup"]]
    sp = [e for e in events if e["issuer_id"] == api.SECOND_PLATFORM]
    assert sp and {e["kind"] for e in sp} <= {"order_completed", "order_returned", "service_charge",
                                              "weekly_statement", "earnings_update"}
    assert all(not (CONTRACT_ONLY & e.keys()) for e in sp), "raw events must not carry contract fields"
    for st in (e for e in sp if e["kind"] == "weekly_statement"):
        assert st["gross_sales_pence"] - st["returns_pence"] - st["charges_pence"] == st["net_payable_pence"]
        assert ts(st["period_end"]) - ts(st["period_start"]) == timedelta(days=7)
        assert ts(st["period_start"]).weekday() == 0
    # the bank shows the platform's own reference format, never a PO- reference
    credit = next(d["row"] for d in case["bank_deliveries"] if d["row"]["txn_id"] == case["holder_claim"]["txn_id"])
    if credit["counterparty_name"] == "Second Platform Payouts":
        assert credit["payment_reference"].startswith("SPLAT W")


@pytest.mark.parametrize("case", [c for c in SECOND if c["variant"].startswith("both_platforms")],
                         ids=lambda c: f"case{c['case_number']}")
def test_two_platform_period_answer(case):
    rfc = case["rfcs"][1]
    _, active, _ = replay(api.known_at(case, rfc["issued_at"], include_lookup=True))
    lo, hi = ts(rfc["period"]["from"]), ts(rfc["period"]["to"])
    inside = [e for e, _ in active.values() if e["kind"] in ("sale", "refund") and lo <= ts(e["occurred_at"]) < hi]
    facts = case["gold"]["answers"][rfc["request_id"]][ledgers.Q_PERIOD]["facts"]
    assert facts["sales_minor"] == sum(e["amount_minor"] for e in inside if e["kind"] == "sale")
    assert facts["refunds_minor"] == sum(e["amount_minor"] for e in inside if e["kind"] == "refund")
    assert {e["issuer_id"] for e in inside} == {ledgers.ISSUER, api.SECOND_PLATFORM}
    by = facts["by_issuer"]
    assert by[ledgers.ISSUER]["sales_minor"] + by[api.SECOND_PLATFORM]["sales_minor"] == facts["sales_minor"]
    assert case["subject_ref"] == "maya-001" and [r["audience"] for r in case["rfcs"]] == ["bank_a", "bank_b"]


def test_second_platform_lookup_by_transfer_ref():
    case = next(c for c in SECOND if c["variant"] == "statement_by_lookup")
    at = case["gold"]["decision_time"]
    [step] = case["gold"]["retrieval_path"]
    [st] = api.lookup(case, at, **step)
    assert st["kind"] == "weekly_statement" and st["record_id"] in case["gold"]["requires_retrieval"]
    assert api.lookup(case, at, payout_reference=st["statement_no"])[0]["record_id"] == st["record_id"]
    assert api.lookup(case, at, covers_record_id=st["line_record_ids"][0])[0]["record_id"] == st["record_id"]


# --------------------------------------------------------------------------
# throughput set (cases 101-300, not for scoring)
# --------------------------------------------------------------------------

def test_throughput_set():
    tp = api.load_throughput()
    assert len(tp) == 200 and [c["case_number"] for c in tp] == list(range(101, 301))
    assert all(c["purpose"].startswith("throughput") for c in tp)
    assert not {c["case_number"] for c in tp} & {c["case_number"] for c in CASES}
    assert tp == ledgers.build_throughput(), "out/throughput is stale: run python -m agents.data.ledgers --throughput"
    # every throughput case obeys the same rules as the development cases
    for case in tp:
        test_ingest_outcomes(case)
        test_gold_rederived_at_decision_time(case)
        test_contract_shapes(case)
