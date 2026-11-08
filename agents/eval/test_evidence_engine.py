"""Evidence engine checks. Public cases are inputs only; gold is never passed to the engine.

Tests named ``test_synthetic_*`` mutate a public case or its lookup callback to build a labelled
synthetic scenario that the public set does not contain.
"""

from copy import deepcopy
import dataclasses
import json
import random

import pytest

from agents.data.api import canonical_case, load_case, load_cases, without_gold
from agents.eval.api import LookupCache, diff_reports, evidence_report, run_engine
from agents.eval.baselines import _prepare, _rules
from agents.eval.case_io import request_context


def _case(number: int) -> dict:
    return without_gold(load_case(number))


def _first(case: dict) -> str:
    return case["rfcs"][0]["request_id"]


def _run(case: dict, request_id: str | None = None, lookup=None, **options):
    view, find = request_context(case, request_id or _first(case))
    return run_engine(view, lookup(find) if lookup else find, **options)


def test_canonical_amounts_reconcile_then_targeted_lookup_confirms():
    report = evidence_report(without_gold(canonical_case()), "rfc-0001-01")
    [credit] = report.credits
    first, last = report.trace[0], report.trace[-1]
    initial = {check_id: after for check_id, _, after in first.changes}
    assert initial["txn-0001-004/settlement.record"] == "missing"
    assert initial["txn-0001-004/release.not_settlement"] == "missing"
    assert initial["txn-0001-004/arithmetic.residual"] == "pass"      # release basis: 216000 - 216000 = 0
    assert [(a.key, a.value, a.tier, a.outcome) for a in report.actions] == [
        ("payout_reference", "PO-0001-01", "exact_reference", "ok")]
    assert report.calls == 1 and last.action_ids == ("L1",)
    assert ("txn-0001-004/settlement.record", "missing", "pass") in last.changes
    assert ("txn-0001-004", "unmatched", "linked") in last.link_states
    assert credit.link_state == "linked" and credit.payout_record_ids == ("rec-0001-021",)
    assert credit.residual.basis == "settlement"
    assert (credit.residual.sales_minor, credit.residual.refunds_minor, credit.residual.fees_minor,
            credit.residual.net_minor, credit.residual.residual_minor) == (240000, 18000, 6000, 216000, 0)
    assert report.checks()["txn-0001-004/holder.breakdown"] == "pass"
    assert credit.diagnosed_gap is None
    source = report.answer("q.source_of_funds")
    assert source.status == "supported"
    assert [(ref.source_id, ref.revision, ref.provenance) for ref in source.evidence_refs] == [
        ("bank:txn-0001-004", 1, "bank_record"), ("merchant:rec-0001-021", 1, "merchant_record")]
    assert report.answer("q.breakdown").facts["breakdown_minor"] == {
        "sales": 240000, "refunds": 18000, "fees": 6000, "net": 216000}
    assert json.loads(json.dumps(report.to_dict()))["answers"]["q.breakdown"]["status"] == "supported"
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.calls = 0


def test_pending_payout_contradicts_holder_reference_and_supports_nothing():
    report = evidence_report(_case(2), "rfc-0002-01")
    checks = report.checks()
    assert checks["txn-0002-004/holder.reference"] == "fail"
    assert checks["txn-0002-004/settlement.status"] == "fail"
    assert checks["txn-0002-004/settlement.timing"] == "missing"
    # The pending payout's components reconcile, but that is arithmetic, not settlement.
    assert report.credits[0].residual.basis == "unlinked_payout"
    assert report.credits[0].residual.residual_minor == 0
    assert checks["txn-0002-004/holder.breakdown"] == "missing"
    assert report.credits[0].diagnosed_gap == "settlement_pending_at_booking"
    for answer in report.answers:
        assert (answer.status, answer.gap, answer.facts, answer.evidence_refs) == (
            "unresolved", "settlement_pending_at_booking", {}, ())
    assert report.calls == 1       # the covering lookup is skipped: a known payout already covers the release


def test_ambiguous_candidates_stay_ambiguous():
    report = evidence_report(_case(13), _first(_case(13)))
    [credit] = report.credits
    assert credit.link_state == "ambiguous"
    assert report.checks()[f"{credit.txn_id}/settlement.unique"] == "fail"
    assert all(answer.status == "unresolved" and answer.gap == "ambiguous_candidates" for answer in report.answers)


def test_missing_cited_component_is_fetched_by_id_then_stays_missing():
    report = evidence_report(_case(35), _first(_case(35)))
    [credit] = report.credits
    missing = [a for a in report.actions if a.tier == "missing_component"]
    assert [(a.key, a.outcome) for a in missing] == [("record_id", "not_found")]
    assert report.checks()[f"{credit.txn_id}/components.present"] == "missing"
    assert credit.diagnosed_gap == "missing_cited_record"
    assert report.answer("q.breakdown").status == "unresolved"


def test_synthetic_zero_residual_without_settlement_stays_missing():
    report = _run(_case(1), lookup=lambda find: lambda **key: [])
    [credit] = report.credits
    assert credit.residual.basis == "balance_release" and credit.residual.residual_minor == 0
    assert report.checks()["txn-0001-004/settlement.record"] == "missing"
    assert credit.diagnosed_gap == "balance_release_not_settlement"
    assert all(answer.status == "unresolved" and not answer.evidence_refs for answer in report.answers)


@pytest.mark.parametrize("number", [21, 22, 23, 24, 25, 26, 27, 29, 30, 31])
def test_duplicates_and_revisions_match_the_fixed_baseline_reading(number):
    case = _case(number)
    request_id = _first(case)
    evidence, _ = _prepare(case, request_id)
    expected = _rules(evidence)["answers"]
    report = evidence_report(case, request_id)
    for answer in report.answers:
        row = expected[answer.question_id]
        assert (answer.status, answer.gap, answer.facts) == (row["status"], row["gap"], row["facts"])
        assert [(r.source_id, r.revision) for r in answer.evidence_refs] == [
            (r["source_id"], r["revision"]) for r in row["evidence_refs"]]


def _swap_conflict(case: dict, record_id: str) -> dict:
    """Same case with the conflicting deliveries of one record in the opposite order."""
    swapped = deepcopy(case)
    rows = swapped["merchant_deliveries"]
    first, second = [i for i, entry in enumerate(rows) if entry["event"]["record_id"] == record_id]
    rows[first]["event"], rows[second]["event"] = rows[second]["event"], rows[first]["event"]
    return swapped


def test_conflicting_bytes_quarantine_the_identity_in_either_order():
    # Case 28 redelivers evt-0028-001 with a different amount. The fixed baseline keeps
    # whichever copy arrived first; the engine withholds the record in both orders.
    case = _case(28)
    reports = [evidence_report(variant, _first(case)) for variant in (case, _swap_conflict(case, "rec-0028-001"))]
    for report in reports:
        [credit] = report.credits
        assert ("merchant:rec-0028-001", "conflicting_identity") in report.exclusions
        assert report.checks()[f"{credit.txn_id}/components.identity"] == "fail"
        assert not [a for a in report.actions if a.value == "rec-0028-001"]     # no refetch of a conflict
        for answer in report.answers:
            assert answer.status == "unresolved" and answer.facts == {} and not answer.evidence_refs
        assert "identity_check_failed" in report.answer("q.source_of_funds").hold_reasons
    first, second = (report.to_dict() for report in reports)
    first.pop("visible_digest"), second.pop("visible_digest")
    assert first == second
    # The baseline's answer does depend on the order, which is what the quarantine removes.
    baseline = [_rules(_prepare(variant, _first(case))[0])["answers"]["q.breakdown"]["facts"]
                for variant in (case, _swap_conflict(case, "rec-0028-001"))]
    assert baseline[0] != baseline[1]


def test_synthetic_same_revision_different_event_conflict_is_quarantined_in_either_order():
    case = _case(1)
    sale = next(entry for entry in case["merchant_deliveries"] if entry["event"]["record_id"] == "rec-0001-001")
    rival = deepcopy(sale)
    rival["event"].update(event_id="evt-0001-001-b", amount_minor=99999)
    for rows in ([*case["merchant_deliveries"], rival], [rival, *case["merchant_deliveries"]]):
        variant = {**deepcopy(case), "merchant_deliveries": deepcopy(rows)}
        report = evidence_report(variant, "rfc-0001-01")
        assert ("merchant:rec-0001-001", "conflicting_identity") in report.exclusions
        assert report.answer("q.breakdown").status == "unresolved"
        assert report.answer("q.source_of_funds").hold_reasons == ("identity_check_failed",)


def test_synthetic_bank_revision_and_later_evidence_are_respected():
    case = _case(1)
    request = case["rfcs"][0]
    credit = next(e for e in case["bank_deliveries"] if e["row"]["txn_id"] == "txn-0001-004")
    case["bank_deliveries"].append({**credit, "row": {**credit["row"], "revision": 2}})
    late = deepcopy(case["merchant_deliveries"][0])
    late["received_at"] = "2100-01-01T00:00:00Z"
    late["event"].update(event_id="evt-late", record_id="rec-late")
    case["merchant_deliveries"].append(late)

    def lookup(find):
        def call(**key):
            future = {**find(**key)[0], "event_id": "evt-future", "record_id": "rec-future",
                      "occurred_at": "2100-01-01T00:00:00Z"}
            return find(**key) + [future]
        return call

    report = _run(case, request["request_id"], lookup)
    assert ("bank:txn-0001-004", 2) in [(r.source_id, r.revision) for r in report.answer("q.source_of_funds").evidence_refs]
    assert ("merchant:rec-future", "after_decision_time") in report.exclusions
    assert "rec-late" not in json.dumps(report.to_dict())
    assert report.actions[0].rejected == 1


def test_synthetic_foreign_subject_and_currency_never_link():
    def foreign(find):
        return lambda **key: [{**row, "subject_ref": "someone-else"} for row in find(**key)]

    report = _run(_case(1), lookup=foreign)
    assert ("merchant:rec-0001-021", "foreign_subject") in report.exclusions
    assert report.checks()["txn-0001-004/settlement.record"] == "missing"
    assert report.answer("q.source_of_funds").status == "unresolved"

    def euro(find):
        return lambda **key: [{**row, "currency": "EUR"} for row in find(**key)]

    report = _run(_case(1), lookup=euro)
    assert report.credits[0].link_state == "unmatched"
    assert report.checks()["txn-0001-004/settlement.currency"] == "fail"
    assert report.answer("q.source_of_funds").status == "unresolved"


def test_synthetic_same_record_id_from_another_issuer_is_withheld():
    case = _case(1)
    clash = deepcopy(case["merchant_deliveries"][0])
    clash["event"].update(event_id="evt-other-issuer", issuer_id="other-issuer", amount_minor=1)
    case["merchant_deliveries"].append(clash)
    report = evidence_report(case, "rfc-0001-01")
    checks = report.checks()
    assert checks["txn-0001-004/components.identity"] == "fail"
    assert ("merchant:rec-0001-001", "identity_collision") in report.exclusions
    assert report.answer("q.source_of_funds").status == "unresolved"
    assert "identity_check_failed" in report.answer("q.source_of_funds").hold_reasons
    assert report.answer("q.breakdown").facts == {}


def test_self_supplied_settlement_is_held_not_supported():
    # Public Case 39: the only payout evidence is the holder's CSV export. The shared rules
    # would draft "supported" citing import refs; the engine holds it for review instead.
    case = _case(39)
    report = evidence_report(case, _first(case))
    record = next(c for credit in report.credits for c in credit.checks if c.name == "settlement.record")
    assert record.basis == "asserted" and {ref.provenance for ref in record.refs} == {"self_supplied"}
    assert report.credits[0].link_state == "linked"
    for answer in report.answers:
        assert answer.status == "unresolved" and "self_supplied_evidence" in answer.hold_reasons
        assert not answer.evidence_refs and answer.facts == {}


def test_synthetic_canonical_payout_supplied_only_as_an_import_is_held():
    raw = load_case(1)
    payout = raw["merchant_lookup"].pop()["event"]
    raw["imports"].append({"received_at": "2026-10-30T00:00:00Z", "record": deepcopy(payout)})
    report = evidence_report(without_gold(raw), "rfc-0001-01")
    assert [(a.value, a.outcome) for a in report.actions] == [("PO-0001-01", "not_found")]
    [credit] = report.credits
    assert credit.link_state == "linked" and credit.payout_record_ids == ("rec-0001-021",)
    assert credit.residual.basis == "settlement" and credit.residual.residual_minor == 0
    source = report.answer("q.source_of_funds")
    assert (source.status, source.gap, source.hold_reasons) == (
        "unresolved", "settlement_not_found", ("self_supplied_evidence",))
    assert report.answer("q.breakdown").status == "unresolved"

    # An import that claims issuer trust and reuses the issuer's payout ID never replaces it.
    spoof = deepcopy(_case(1))
    payout = deepcopy(load_case(1)["merchant_lookup"][0]["event"])
    spoof["imports"].append({"received_at": "2026-10-30T00:00:00Z",
                             "record": {**payout, "trust": "issuer_verified", "revision": 9, "event_id": "imp-1"}})
    report = evidence_report(spoof, "rfc-0001-01")
    source = report.answer("q.source_of_funds")
    assert ("merchant:rec-0001-021", 1, "merchant_record") in [
        (r.source_id, r.revision, r.provenance) for r in source.evidence_refs]


def test_budget_exhaustion_proposes_but_never_supports():
    report = evidence_report(_case(1), "rfc-0001-01", budget=0)
    assert report.calls == 0
    assert [(a.value, a.state, a.outcome) for a in report.actions] == [("PO-0001-01", "skipped_budget", None)]
    assert not report.credits[0].retrieval_complete
    assert all(answer.status != "supported" for answer in report.answers)


def test_repeated_lookups_are_deduplicated_and_cached_per_scope():
    case = _case(1)
    calls = []

    def counting(find):
        def call(**key):
            calls.append(key)
            return find(**key)
        return call

    cache = LookupCache()
    first = _run(case, lookup=counting, cache=cache)
    assert calls == [{"payout_reference": "PO-0001-01"}]     # bank and holder cite the same reference
    again = _run(case, lookup=counting, cache=cache)
    assert len(calls) == 2 and again.cache_hits == 0          # no source watermark: recompute every call
    second = _run(case, lookup=counting, cache=cache, source_version="v1")
    third = _run(case, lookup=counting, cache=cache, source_version="v1")
    assert len(calls) == 3 and third.calls == 0 and third.cache_hits == 1
    assert third.answers == second.answers == first.answers

    corrected = deepcopy(case)
    credit = next(e for e in corrected["bank_deliveries"] if e["row"]["txn_id"] == "txn-0001-004")
    corrected["bank_deliveries"].append({**credit, "row": {**credit["row"], "revision": 2}})
    fourth = _run(corrected, lookup=counting, cache=cache, source_version="v1")
    assert len(calls) == 4 and fourth.cache_hits == 0              # a delivered revision clears the scope
    _run(case, "rfc-0001-02", lookup=counting, cache=cache, source_version="v1")   # another request
    _run(case, lookup=counting, cache=cache, source_version="v1")
    assert len(calls) == 5                                           # nothing leaked across those scopes


def test_synthetic_hidden_revision_change_is_not_served_stale_from_the_cache():
    # Reproduction from review: only the lookup-only payout changes (new event, revision 2, failed).
    raw = load_case(1)
    cache = LookupCache()
    supported = evidence_report(without_gold(raw), "rfc-0001-01", cache=cache)
    assert supported.answer("q.source_of_funds").status == "supported"
    revised = deepcopy(raw)
    event = revised["merchant_lookup"][0]["event"]
    event.update(revision=2, event_id="evt-0001-021-r2", status="failed", settled_at=None)
    reused = evidence_report(without_gold(revised), "rfc-0001-01", cache=cache)
    fresh = evidence_report(without_gold(revised), "rfc-0001-01")
    assert reused.to_dict() == fresh.to_dict()
    assert reused.answer("q.source_of_funds").status == "unresolved"
    assert reused.checks()["txn-0001-004/settlement.status"] == "fail"
    # A caller that changes the connector watermark gets the same fresh answer from a shared cache.
    shared = LookupCache()
    evidence_report(without_gold(raw), "rfc-0001-01", cache=shared, source_version="w1")
    after = evidence_report(without_gold(revised), "rfc-0001-01", cache=shared, source_version="w2")
    assert after.to_dict() | {"cache_hits": 0} == fresh.to_dict() | {"cache_hits": 0} and after.calls == 1


def test_synthetic_naive_timestamp_is_excluded_not_a_type_error():
    raw = load_case(1)
    raw["merchant_lookup"][0]["event"]["occurred_at"] = "2026-10-26T09:00:00"
    report = evidence_report(without_gold(raw), "rfc-0001-01")
    assert ("merchant:rec-0001-021", "malformed") in report.exclusions
    assert report.answer("q.source_of_funds").status == "unresolved"
    settled = load_case(1)
    settled["merchant_lookup"][0]["event"]["settled_at"] = "2026-10-27T16:00:00"
    assert evidence_report(without_gold(settled), "rfc-0001-01").answer("q.source_of_funds").status == "unresolved"
    pushed = without_gold(load_case(1))
    pushed["bank_deliveries"][-1]["row"]["booked_at"] = "2026-10-28T06:30:00"
    report = evidence_report(pushed, "rfc-0001-01")
    assert ("bank:txn-0001-004", "malformed") in report.exclusions
    assert report.checks()["txn-0001-004/credit.observed"] == "missing"


def test_synthetic_lookup_errors_and_refusals_are_typed_and_unsupported():
    def broken(find):
        def call(**key):
            raise RuntimeError("merchant endpoint down")
        return call

    def refused(find):
        def call(**key):
            raise PermissionError("scope")
        return call

    for wrapper, outcome in ((broken, "error"), (refused, "refused")):
        report = _run(_case(1), lookup=wrapper)
        assert report.actions[0].outcome == outcome
        assert not report.credits[0].retrieval_complete
        assert all(answer.status == "unresolved" for answer in report.answers)
        assert "merchant endpoint down" not in json.dumps(report.to_dict())


def test_metadata_gold_and_hidden_pool_do_not_change_the_report():
    case = _case(42)
    base = evidence_report(case, _first(case)).to_dict()
    relabelled = deepcopy(case)
    relabelled.update(family="clean", variant="x", title="evaluator-only", label="SYNTHETIC")
    assert evidence_report(relabelled, _first(case)).to_dict() == base
    with pytest.raises(ValueError):
        evidence_report(load_case(42), _first(case))
    view, find = request_context(case, _first(case))
    view["merchant_lookup"] = [{"available_from": "2000-01-01T00:00:00Z", "event": {"poison": True}}]
    assert run_engine(view, find).to_dict() == base


def test_hidden_reference_uses_one_justified_covering_lookup():
    case = _case(42)
    report = evidence_report(case, _first(case))
    [action] = report.actions
    assert (action.tier, action.key, action.outcome) == ("covering_record", "covers_record_id", "ok")
    assert action.justified_by and all(source.startswith("merchant:") for source in action.justified_by)
    assert report.credits[0].link_state == "linked"


@pytest.mark.parametrize("number", [1, 22, 23, 28, 29, 30, 44])
def test_synthetic_shuffled_deliveries_give_the_same_report(number):
    case = _case(number)
    request_id = _first(case)
    base = evidence_report(case, request_id).to_dict()
    for seed in (7, 8, 9):
        shuffled = deepcopy(case)
        rng = random.Random(seed)
        rng.shuffle(shuffled["merchant_deliveries"])
        rng.shuffle(shuffled["bank_deliveries"])
        rng.shuffle(shuffled["imports"])
        other = evidence_report(shuffled, request_id).to_dict()
        other.pop("visible_digest")
        assert other == {key: value for key, value in base.items() if key != "visible_digest"}


def test_period_request_uses_visible_totals_without_retrieval():
    report = evidence_report(_case(1), "rfc-0001-02")
    assert report.scope == "period" and report.calls == 0 and not report.actions
    assert report.answer("q.sales_refunds_in_period").facts == {
        "sales_minor": 240000, "refunds_minor": 18000, "sale_count": 8, "refund_count": 2}


def test_diff_reports_names_the_changed_dependencies():
    case = _case(1)
    before = _run(case, lookup=lambda find: lambda **key: [])
    after = evidence_report(case, "rfc-0001-01")
    diff = diff_reports(before, after)
    assert diff["checks"]["txn-0001-004/settlement.record"] == ["missing", "pass"]
    assert diff["answers"]["q.source_of_funds"] == ["unresolved", "supported"]
    assert diff["dependencies"]["merchant:rec-0001-021"] == [None, 1]
    assert not diff["visible_inputs_changed"]


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: str(case["case_number"]))
def test_every_public_request_is_bounded_and_serialisable(case):
    stripped = without_gold(case)
    for rfc in stripped["rfcs"]:
        report = evidence_report(stripped, rfc["request_id"])
        assert report.calls <= report.budget
        keys = [(a.key, a.value) for a in report.actions]
        assert len(keys) == len(set(keys))
        assert {answer.question_id for answer in report.answers} == set(rfc["question_ids"])
        assert all(answer.status != "supported" or answer.evidence_refs for answer in report.answers)
        json.dumps(report.to_dict())
