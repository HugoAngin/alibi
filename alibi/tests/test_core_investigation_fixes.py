"""Track 1b-ii regressions: settlement timing, pinned gap codes, question alignment and the
settlement-evidence gaps (alibi/NEEDS.md items 2, 3, 4 and 6; alibi/BLOCKERS.md "Rules engine
verdicts"; agents/eval/test_redteam_core.py). Unit layer through ``FakeGateway``, integration
layer through the real gateway and ingest path, and the two demo cases through the app's case
adapter. No network, no Gemini."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from alibi import api
from alibi.core import ingest
from alibi.core.investigate_rules import (
    BREAKDOWN,
    DOCUMENTED_TOTALS,
    GAP_CODES,
    SOURCE_OF_FUNDS,
    classify,
    classify_question,
    next_run_id,
    run_investigation,
)
from alibi.core.schemas import (
    Alert,
    AlertKind,
    BankTxn,
    ClaimStatus,
    KeyPurpose,
    LinkState,
    MerchantEvent,
    Question,
    RecordKind,
    RequestForContext,
)
from alibi.core.signing import DEMO_KEYS, DemoKeystore
from alibi.core.store import Store
from alibi.fixtures import maya
from alibi.gateway import Gateway, RejectedResult, ScopeError
from alibi.tests.test_investigate_rules import (
    FakeGateway,
    claim_of,
    investigate,
    keys,
    put_signed,
    record_of,
    rfc,
    rref,
    variant,
)

SUBJ = maya.SUBJECT_REF
RFC_A, RFC_B = maya.RFC_A_ID, maya.RFC_B_ID
SUPPORTED, CONTRADICTED, UNRESOLVED = (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED,
                                       ClaimStatus.UNRESOLVED)
PINNED_RFC_A = [{"question_id": "q.source_of_funds",
                 "text": "What is the origin of the incoming payment booked on 3 November?"},
                {"question_id": "q.breakdown", "text": "Please show how this amount was calculated."}]
PINNED_RFC_B = [{"question_id": "q.sales_refunds_in_period",
                 "text": "Please provide documented marketplace sales and refunds between 01 "
                         "October 2026 and 31 October 2026."}]


@pytest.fixture
def passport(tmp_path, keystore):
    opened = []

    def make(fx, name="passport.db"):
        if isinstance(fx, str):
            fx = maya.load_variant(fx)
        store = Store(tmp_path / name)
        opened.append(store)
        for raw in fx["merchant_events"]:
            put_signed(store, keystore, raw)
        store.put_bank_txns([BankTxn.model_validate(t) for t in fx["bank_txns"]],
                            connector="bank_feed")
        for raw in fx["rfcs"]:
            store.save_request(RequestForContext.model_validate(raw))
        return store, fx

    yield make
    for store in opened:
        store.close()


def pinned(fx):
    """The Maya RFCs with the team's pinned question ids."""
    fx["rfcs"][0]["questions"] = copy.deepcopy(PINNED_RFC_A)
    fx["rfcs"][1]["questions"] = copy.deepcopy(PINNED_RFC_B)


def settlement_at(effective_at, observed=None):
    def edit(fx):
        settlement = fx["lookup_events"][0]
        settlement["effective_at"] = effective_at
        settlement["occurred_at"] = observed or effective_at
    return edit


# --------------------------------------------------------------------------- settlement timing


def test_a_settlement_more_than_five_days_before_the_credit_cannot_support_it(passport, keystore,
                                                                             now):
    fx = variant("supported", settlement_at("2026-10-20T07:45:00Z", "2026-10-20T08:00:00Z"))
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert result.links[0].link_state == LinkState.LINKED      # the reference still links it
    assert claim.status == UNRESOLVED and claim.gap == "settlement_not_found"
    assert "more than 5 days before the booking" in claim.gap_detail
    assert claim.calculation.balanced is True and claim.calculation.timing.ok is False
    assert "more than 5 days before" in claim.calculation.timing.reason
    assert claim.facts == {}


def test_exactly_five_days_before_the_credit_still_supports_it(passport, keystore, now):
    fx = variant("supported", settlement_at("2026-10-29T10:00:00Z"))   # booked 11-03T10:00
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert claim.status == SUPPORTED and claim.gap is None and claim.gap_detail is None
    assert claim.facts == {"payout_references": ["PO-5001"], "settled_at": ["2026-10-29T10:00:00Z"]}


# --------------------------------------------------------------------------- gap codes


@pytest.mark.parametrize("name, edit, status, code, words", [
    ("contradiction", None, CONTRADICTED, "settlement_pending_at_booking", "still pending"),
    ("ambiguous", None, UNRESOLVED, "ambiguous_candidates", "cannot choose"),
    ("supported", lambda fx: fx["lookup_events"].clear(), UNRESOLVED, "settlement_not_found",
     "No settlement record for payout PO-5001"),
    ("supported", lambda fx: fx["lookup_events"][0].update(status="reversed"), CONTRADICTED,
     "settlement_failed", "status reversed"),
    ("supported", settlement_at("2026-11-03T11:00:00Z", "2026-11-03T11:30:00Z"), CONTRADICTED,
     "settlement_after_credit", "after the credit was booked"),
    ("supported", lambda fx: fx["lookup_events"][0].update(amount_pence=200000), CONTRADICTED,
     "amount_mismatch", "200000 pence GBP, but the credit is 216000"),
    ("supported", lambda fx: record_of(fx, "po_5001")["related_record_ids"].append("ord_1099"),
     UNRESOLVED, "missing_cited_record", "ord_1099"),
    ("supported", lambda fx: (record_of(fx, "po_5001").update(status="released_to_balance"),
                              fx["lookup_events"].clear()), UNRESOLVED,
     "balance_release_not_settlement", "released to balance"),
], ids=["pending", "ambiguous", "not-found", "failed", "after-credit", "amount", "missing",
        "release"])
def test_claims_carry_the_pinned_gap_code_and_the_sentence_separately(passport, keystore, now,
                                                                     name, edit, status, code,
                                                                     words):
    fx = variant(name, edit)
    pinned(fx)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result, "q.source_of_funds")
    assert claim.status == status
    assert claim.gap == code and code in GAP_CODES
    assert words in claim.gap_detail
    assert claim.gap_detail in claim.rationale           # the recipient still reads the sentence
    assert claim.to_json_dict()["gap"] == code
    assert claim_of(result, "q.breakdown").gap == code   # the breakdown follows the same gap


def test_unmapped_questions_use_an_engine_code(passport, keystore, now):
    store, fx = passport("supported")
    store.save_request(rfc("rfc_u_001", ["Do you hold any cryptocurrency?"], txn_ids=["txn_9001"]))
    result, _ = investigate(store, fx, keystore, now, "rfc_u_001")
    claim = claim_of(result)
    assert (claim.status, claim.gap) == (UNRESOLVED, "question_not_mapped")
    assert claim.gap_detail.startswith("The rules engine could not map")


# --------------------------------------------------------------------------- issuer binding


@pytest.fixture
def two_issuers(tmp_path):
    keys = DemoKeystore(tmp_path / "two_keys", specs={
        **DEMO_KEYS, "merchant_other": (KeyPurpose.RECORD_SIGNING, "other_market")}).ensure()
    return SimpleNamespace(keys=keys, registry=keys.registry())


def real_world(tmp_path, keys, registry, fx, now, *, name="real.db"):
    """The real ingest path: signed events, bank txns and signed requests."""
    store = Store(tmp_path / name)
    for raw in fx["merchant_events"]:
        ingest.ingest_merchant_event(store, registry, maya.sign_event(raw, keys), now=now)
    ingest.ingest_bank_txns(store, fx["bank_txns"], subject_ref=SUBJ,
                            connection_id=maya.CONNECTION_ID, now=now)
    for raw in fx["rfcs"]:
        ingest.receive_rfc(store, registry, maya.sign_request(raw, keys),
                           authenticated_issuer=raw["issuer"], now=now,
                           account_refs=[maya.ACCOUNT_REF] if raw.get("txn_ids") else None)
    return store


def real_run(store, request_id, registry, now, lookup, *, budget=12):
    request = store.get_request(request_id)
    gateway = Gateway(store, subject_ref=SUBJ, run_id=next_run_id(store, request_id, audience=store.get_request(request_id).audience),
                      registry=registry, now=now, budget=budget, settlement_lookup=lookup,
                      request_id=request_id)
    return run_investigation(request, store, gateway, now=now), gateway


def test_another_issuers_settlement_cannot_prove_the_payout_on_either_path(tmp_path, two_issuers,
                                                                          now):
    fx = maya.load_variant("supported")
    store = real_world(tmp_path, two_issuers.keys, two_issuers.registry, fx, now)
    foreign = {**fx["lookup_events"][0], "issuer_id": "other_market", "record_id": "stl_foreign",
               "event_id": "evt_stl_foreign_r1"}
    signed_foreign = maya.sign_event(foreign, two_issuers.keys, kid="merchant_other")
    # Lookup path: only threadline's records carry PO-5001, so the gateway refuses the other
    # issuer's answer before anything is stored.
    result, gateway = real_run(store, RFC_A, two_issuers.registry, now, lambda _: signed_foreign)
    assert claim_of(result).status == UNRESOLVED
    refusal = [e for e in gateway.trace if e.tool == "lookup_settlement" and e.step == "refusal"]
    assert refusal and "another issuer" in refusal[0].reason
    # Passport path: the other issuer's settlement is pushed and verified, so it now carries the
    # reference in the passport; the rules engine still does not let it prove threadline's payout.
    ingest.ingest_merchant_event(store, two_issuers.registry, signed_foreign, now=now)
    result, _ = real_run(store, RFC_A, two_issuers.registry, now, lambda _: None)
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and claim.gap == "settlement_not_found"
    assert "record:stl_foreign@1" not in keys(claim.evidence_refs)
    # Nor through the lookup, now that the gateway's scope admits the other issuer's record.
    result, _ = real_run(store, RFC_A, two_issuers.registry, now, lambda _: signed_foreign)
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "not settlement evidence for PO-5001 from threadline" in claim.rationale
    # The payout's own issuer still supports it.
    result, _ = real_run(store, RFC_A, two_issuers.registry, now,
                         maya.settlement_lookup(fx, two_issuers.keys))
    assert claim_of(result).status == SUPPORTED


def test_a_settlement_reference_from_another_issuer_never_names_the_payout(tmp_path, two_issuers,
                                                                          now):
    # The settlement-reference path through the passport is bound to the payout's issuer too.
    fx = variant("contradiction")
    fx["bank_txns"][0]["refs"] = ["STL-88001"]
    store = real_world(tmp_path, two_issuers.keys, two_issuers.registry, fx, now)
    foreign = {**fx["lookup_events"][0], "issuer_id": "other_market", "status": "settled",
               "effective_at": "2026-11-03T07:45:00Z", "record_id": "stl_foreign",
               "event_id": "evt_stl_foreign_r1"}
    ingest.ingest_merchant_event(store, two_issuers.registry,
                                 maya.sign_event(foreign, two_issuers.keys, kid="merchant_other"),
                                 now=now)   # the only record tying STL-88001 to PO-5001 is foreign
    result, _ = real_run(store, RFC_A, two_issuers.registry, now, lambda _: None)
    assert result.links[0].method == "amount_date"   # not matched by that reference
    assert claim_of(result).status != SUPPORTED


# --------------------------------------------------------------------------- one payout, one credit


def test_one_payout_cannot_support_two_credits_in_separate_requests(passport, keystore, now):
    def twin(fx):
        credit = copy.deepcopy(fx["bank_txns"][0])
        credit["txn_id"] = "txn_9002"
        fx["bank_txns"].append(credit)
        fx["rfcs"].append({**fx["rfcs"][0], "request_id": "rfc_second", "txn_ids": ["txn_9002"]})

    fx = variant("supported", twin)
    store, _ = passport(fx)
    first, _ = investigate(store, fx, keystore, now, RFC_A)
    assert claim_of(first).status == SUPPORTED
    second, _ = investigate(store, fx, keystore, now, "rfc_second")
    claim = claim_of(second)
    assert claim.status == UNRESOLVED and claim.gap == "payout_already_linked"
    assert "another credit of the same account holder in an earlier request" in claim.gap_detail
    assert claim.facts == {}
    # The earlier credit may belong to another recipient: the claim counts it, never names it.
    assert "txn_9001" not in claim.gap_detail and "txn_9001" not in claim.rationale
    # The conflict is symmetric: once both credits are linked to the payout, a rerun of the
    # first request no longer supports it either (the evidence fits both credits), and its own
    # earlier link is never counted against itself.
    again, _ = investigate(store, fx, keystore, now, RFC_A)
    claim = claim_of(again)
    assert claim.status == UNRESOLVED and claim.gap == "payout_already_linked"
    assert "another credit of the same account holder in an earlier request" in claim.gap_detail
    assert "txn_9002" not in claim.gap_detail and "txn_9002" not in claim.rationale
    assert "source of txn_9001" in claim.gap_detail   # its own credit is still named


def test_another_banks_transaction_id_never_reaches_a_signed_package(passport, keystore, now):
    """Bank A's RFC links the payout to txn_9001; Bank B then asks about txn_9002, the same
    amount on another account. Bank B's claim is unresolved (payout_already_linked) and its
    package, which carries gap, gap_detail-derived rationale and the holder review's gaps,
    never names Bank A's transaction."""
    def twin(fx):
        credit = copy.deepcopy(fx["bank_txns"][0])
        credit["txn_id"] = "txn_9002"
        credit["account_ref"] = "acc_maya_bank_b"
        fx["bank_txns"].append(credit)
        fx["rfcs"].append({**fx["rfcs"][0], "request_id": "rfc_bank_b_credit",
                           "issuer": "bank_b", "audience": "bank_b", "txn_ids": ["txn_9002"],
                           "questions": [{"question_id": "q1", "text": "Please explain the "
                                          "source of the credit received on 3 November 2026 "
                                          "(txn_9002)."}]})

    fx = variant("supported", twin)
    store, _ = passport(fx)
    first, _ = investigate(store, fx, keystore, now, RFC_A)
    assert claim_of(first).status == SUPPORTED
    second, _ = investigate(store, fx, keystore, now, "rfc_bank_b_credit")
    claim = claim_of(second)
    assert claim.status == UNRESOLVED and claim.gap == "payout_already_linked"
    assert "txn_9001" not in (claim.gap_detail or "") and "txn_9001" not in claim.rationale
    assert "txn_9001" not in claim.statement
    # The validation note counts the earlier link instead of naming it.
    notes = [e for e in second.trace if e.step == "validation"
             and (e.result or {}).get("earlier_links") is not None]
    assert notes and all(n.result["earlier_links"] == 1 and n.result["other_txn_ids"] == []
                         for n in notes)
    assert "txn_9001" not in json.dumps([n.result for n in notes])
    draft = api.build_review(store, "rfc_bank_b_credit", now=now)
    approval = api.approve(store, draft.draft_id, holder_id="holder_maya", now=now)
    package = api.sign_and_package(store, approval.approval_id, keystore=keystore, now=now)
    body = json.dumps(package.body.to_json_dict())
    assert "txn_9002" in body                     # Bank B's own transaction is in its package
    assert "txn_9001" not in body                 # Bank A's is not
    assert [c.gap for c in package.body.claims] == ["payout_already_linked"]


def test_the_one_payout_rule_writes_its_trace_note_once_per_credit(passport, keystore, now):
    """Both pinned Bank A questions go through the one-payout rule for the same credit; the
    combined outcome is cached, so the rule's validation note appears once, not per question."""
    def twin(fx):
        pinned(fx)
        credit = copy.deepcopy(fx["bank_txns"][0])
        credit["txn_id"] = "txn_9002"
        fx["bank_txns"].append(credit)
        fx["rfcs"].append({**fx["rfcs"][0], "request_id": "rfc_second", "txn_ids": ["txn_9002"]})

    fx = variant("supported", twin)
    store, _ = passport(fx)
    investigate(store, fx, keystore, now, RFC_A)
    second, _ = investigate(store, fx, keystore, now, "rfc_second")
    by_q = {c.question_id: c for c in second.claims}
    assert by_q["q.source_of_funds"].gap == "payout_already_linked"
    assert by_q["q.breakdown"].gap == "payout_already_linked"
    notes = [e for e in second.trace if e.step == "validation"
             and (e.result or {}).get("earlier_links") is not None]
    assert len(notes) == 1


# --------------------------------------------------------------------------- question alignment


def test_pinned_question_ids_decide_the_check_before_keywords():
    a = RequestForContext.model_validate({**maya.load_variant("supported")["rfcs"][0],
                                          "questions": PINNED_RFC_A})
    b = RequestForContext.model_validate({**maya.load_variant("supported")["rfcs"][1],
                                          "questions": PINNED_RFC_B})
    assert classify(a.questions[0], a).category == SOURCE_OF_FUNDS
    assert classify(a.questions[1], a).category == BREAKDOWN
    found = classify(b.questions[0], b)
    assert (found.category, found.kinds) == (DOCUMENTED_TOTALS, (RecordKind.ORDER, RecordKind.REFUND))
    # The id wins over wording that the keyword classifier cannot place.
    odd = Question(question_id="q.source_of_funds", text="Tell us about this one, please.")
    assert classify(odd, a).category == SOURCE_OF_FUNDS
    # Without the scope a question needs, the id does not force a check.
    assert classify(Question(question_id="q.breakdown", text="Tell us."), b).category == "unmapped"
    # Keyword fallbacks: the demo wordings for q.breakdown classify as breakdown with txn_ids.
    for text in ("Please show how this amount was calculated.",
                 "How does the credit reconcile to your underlying sales, refunds and fees?"):
        assert classify(Question(question_id="q9", text=text), a).category == BREAKDOWN
        assert classify_question(text, a).category == "unmapped"   # keyword-only view unchanged
    several = classify(Question(question_id="q9", text="What were your sales and refunds in October?"), b)
    assert several.category == DOCUMENTED_TOTALS and several.kinds == (RecordKind.ORDER, RecordKind.REFUND)


def test_breakdown_follows_the_source_of_funds_verdict(passport, keystore, now):
    fx = variant("supported", pinned)
    store, _ = passport(fx)
    result, gateway = investigate(store, fx, keystore, now)
    source, breakdown = claim_of(result, "q.source_of_funds"), claim_of(result, "q.breakdown")
    assert source.status == SUPPORTED
    assert source.facts == {"payout_references": ["PO-5001"], "settled_at": ["2026-11-03T07:45:00Z"]}
    assert breakdown.status == SUPPORTED and breakdown.gap is None
    assert breakdown.facts == {"breakdown_minor": {"sales": 240000, "refunds": 18000, "fees": 6000,
                                                   "net": 216000}}
    assert breakdown.calculation.balanced is True and breakdown.calculation.total_pence == 216000
    assert set(maya.COMPONENT_IDS) <= {ref.ref_id for ref in breakdown.evidence_refs}
    assert {"txn:txn_9001@1", "record:po_5001@1", "record:stl_88001@1"} <= keys(breakdown.evidence_refs)
    assert "sales 240000 less refunds 18000 and fees 6000 = 216000" in breakdown.statement
    # The second question reuses the first question's reads: no extra gateway calls.
    assert result.calls_used == 5

    fx = variant("contradiction", pinned)
    store, _ = passport(fx, "contradiction.db")
    result, _ = investigate(store, fx, keystore, now)
    source, breakdown = claim_of(result, "q.source_of_funds"), claim_of(result, "q.breakdown")
    assert source.status == CONTRADICTED and source.gap == "settlement_pending_at_booking"
    assert breakdown.status == UNRESOLVED and breakdown.gap == "settlement_pending_at_booking"
    assert breakdown.evidence_refs == [] and breakdown.facts == {}
    assert "cannot be verified because the credit's source is not established" in breakdown.gap_detail


def test_sales_and_refunds_in_one_question_give_two_documented_totals(passport, keystore, now):
    fx = variant("supported", pinned)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now, RFC_B)
    claim = claim_of(result, "q.sales_refunds_in_period")
    assert claim.status == SUPPORTED and claim.gap is None
    assert claim.facts == {"sales_minor": 240000, "sale_count": 4, "refunds_minor": 18000,
                           "refund_count": 1}
    assert claim.calculation.method == "documented_totals" and claim.calculation.total_pence == 222000
    assert {ref.ref_id for ref in claim.evidence_refs} == {*maya.ORDER_IDS, "rfd_2001"}
    assert "sales total 240000 pence GBP across 4 records" in claim.statement
    assert "refunds total 18000 pence GBP across 1 record" in claim.statement
    assert "240000" in claim.rationale and "18000" in claim.rationale
    # Single-kind totals carry facts too.
    store, plain_fx = passport("supported", "plain.db")
    plain, _ = investigate(store, plain_fx, keystore, now, RFC_B)
    assert claim_of(plain, "q1").facts == {"sales_minor": 240000, "sale_count": 4}
    assert claim_of(plain, "q2").facts == {"refunds_minor": 18000, "refund_count": 1}


# --------------------------------------------------------------------------- NEEDS item 2


def settled_payout(fx):
    """The team's shape: the payout record carries its own settlement status and time."""
    record_of(fx, "po_5001").update(status="settled", effective_at="2026-11-03T07:45:00Z")
    fx["lookup_events"].clear()


def test_a_payout_carrying_its_settlement_status_is_its_own_settlement_evidence(passport, keystore,
                                                                               now):
    fx = variant("supported", settled_payout)
    store, _ = passport(fx)
    result, gateway = investigate(store, fx, keystore, now, lookup=False)
    claim = claim_of(result)
    assert claim.status == SUPPORTED and claim.gap is None
    assert claim.calculation.timing.settlement_ref == rref("po_5001")
    assert keys(claim.evidence_refs) >= {"txn:txn_9001@1", "record:po_5001@1"}
    assert [r for r in claim.evidence_refs if r.ref_id == "po_5001"] == [rref("po_5001")]
    assert "settled (status settled) before the credit was booked" in claim.statement
    assert "carries its own settlement status" in claim.rationale
    assert claim.facts == {"payout_references": ["PO-5001"], "settled_at": ["2026-11-03T07:45:00Z"]}


@pytest.mark.parametrize("status, extra, expected, code", [
    ("failed", {}, CONTRADICTED, "settlement_failed"),
    ("settled", {"effective_at": "2026-11-03T11:00:00Z"}, CONTRADICTED, "settlement_after_credit"),
    ("initiated", {}, UNRESOLVED, "settlement_not_found"),   # pending before booking: no evidence
], ids=["failed", "settled-late", "pending-before"])
def test_a_payouts_own_status_contradicts_or_leaves_open(passport, keystore, now, status, extra,
                                                        expected, code):
    def edit(fx):
        record_of(fx, "po_5001").update(status=status, **extra)
        fx["lookup_events"].clear()

    fx = variant("supported", edit)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now, lookup=False)
    claim = claim_of(result)
    assert (claim.status, claim.gap) == (expected, code)


def test_a_pending_payout_observed_after_the_booking_contradicts_through_the_lookup(passport,
                                                                                   keystore, now):
    # Case 2 through the team's shape: the lookup answers with the payout itself, still pending
    # when observed after the credit was booked.
    def edit(fx):
        fx["lookup_events"] = [{**record_of(fx, "po_5001"), "status": "pending",
                                "occurred_at": "2026-11-03T12:00:00Z",
                                "event_id": "evt_po_5001_r2", "revision": 2,
                                "corrects_revision": 1}]

    fx = variant("supported", edit)
    store, _ = passport(fx)
    lookup = lambda ref: maya.sign_event(fx["lookup_events"][0], keystore)  # noqa: E731
    gateway = FakeGateway(store, subject_ref=SUBJ, run_id=next_run_id(store, RFC_A, audience="bank_a"),
                          registry=keystore.registry(), settlement_lookup=lookup, now=now)
    result = run_investigation(store.get_request(RFC_A), store, gateway, now=now)
    claim = claim_of(result)
    assert (claim.status, claim.gap) == (CONTRADICTED, "settlement_pending_at_booking")
    assert "record:po_5001@2" in keys(claim.evidence_refs)


def test_gateway_accepts_a_payout_with_a_status_as_settlement_evidence(tmp_path, keystore, registry,
                                                                      now):
    fx = variant("supported", settled_payout)
    store = real_world(tmp_path, keystore, registry, fx, now)
    answer = maya.sign_event(record_of(fx, "po_5001"), keystore)
    gateway = Gateway(store, subject_ref=SUBJ, run_id="run_gw_payout", registry=registry, now=now,
                      settlement_lookup=lambda ref: answer)
    record = gateway.lookup_settlement("PO-5001")
    assert record.kind == RecordKind.PAYOUT and record.status == "settled"
    assert gateway.trace[-1].result["put_status"] == "duplicate"   # the pushed record itself
    bare = {k: v for k, v in record_of(fx, "po_5001").items() if k not in ("status", "effective_at")}
    gateway = Gateway(store, subject_ref=SUBJ, run_id="run_gw_bare", registry=registry, now=now,
                      settlement_lookup=lambda ref: maya.sign_event(bare, keystore))
    with pytest.raises(RejectedResult, match="no settlement status"):
        gateway.lookup_settlement("PO-5001")


# --------------------------------------------------------------------------- NEEDS item 3


def test_lookup_scope_extends_to_text_and_bank_references_bound_to_own_issuers(tmp_path, two_issuers,
                                                                              now):
    fx = maya.load_variant("supported")
    store = real_world(tmp_path, two_issuers.keys, two_issuers.registry, fx, now)
    notice = {**record_of(fx, "msg_7001"), "record_id": "msg_7002", "event_id": "evt_msg_7002_r1",
              "text": "Your withdrawal PO-7777 was sent to your bank.", "amount_pence": None,
              "currency": None, "status": None}
    ingest.ingest_merchant_event(store, two_issuers.registry, maya.sign_event(notice, two_issuers.keys),
                                 now=now)
    ingest.import_self_supplied(store, {"record_id": "po_mine", "kind": "payout", "currency": "GBP",
                                        "occurred_at": "2026-11-02T08:30:00Z", "amount_pence": 100,
                                        "payment_ref": "PO-SELF"}, subject_ref=SUBJ, now=now)
    asked = []

    def lookup(ref):
        asked.append(ref)
        return None

    gateway = Gateway(store, subject_ref=SUBJ, run_id="run_scope", registry=two_issuers.registry,
                      now=now, settlement_lookup=lookup)
    assert gateway.lookup_settlement("STL-88001") is None   # on the credit's refs
    assert gateway.lookup_settlement("PO-7777") is None     # in an issuer-verified notice's text
    assert asked == ["STL-88001", "PO-7777"]
    for ref in ("PO-SELF", "PO-9999", "STL-8800"):           # self-supplied only, nobody's, a prefix
        with pytest.raises(ScopeError, match="issuer evidence"):
            gateway.lookup_settlement(ref)
    assert asked == ["STL-88001", "PO-7777"]
    # A text- or bank-side reference is still bound to the subject's own issuers.
    foreign = {**fx["lookup_events"][0], "payment_ref": "PO-7777", "issuer_id": "other_market",
               "record_id": "stl_foreign", "event_id": "evt_stl_foreign_r1"}
    gateway = Gateway(store, subject_ref=SUBJ, run_id="run_scope2", registry=two_issuers.registry,
                      now=now, settlement_lookup=lambda ref: maya.sign_event(
                          foreign, two_issuers.keys, kid="merchant_other"))
    with pytest.raises(RejectedResult, match="another issuer"):
        gateway.lookup_settlement("PO-7777")


def test_the_rules_engine_asks_for_the_credits_own_reference_when_nothing_matches(tmp_path, keystore,
                                                                                 registry, now):
    # Case 1's shape: the payout is lookup-only, the credit carries its reference.
    fx = maya.load_variant("supported")
    payout = {**record_of(fx, "po_5001"), "status": "settled", "effective_at": "2026-11-03T07:45:00Z"}
    fx["merchant_events"] = [e for e in fx["merchant_events"] if e["record_id"] != "po_5001"]
    fx["lookup_events"] = []
    store = real_world(tmp_path, keystore, registry, fx, now)
    asked = []

    def lookup(ref):
        asked.append(ref)
        return maya.sign_event(payout, keystore) if ref == "PO-5001" else None

    result, gateway = real_run(store, RFC_A, registry, now, lookup)
    claim = claim_of(result)
    assert result.links[0].link_state == LinkState.LINKED and result.links[0].method == "reference"
    assert claim.status == SUPPORTED and claim.facts["payout_references"] == ["PO-5001"]
    assert asked == ["PO-5001"]   # explicit refs first; the answer is reused, not asked again
    tools = [e.tool for e in gateway.trace if e.step == "tool_call"]
    assert tools == ["get_txn", "list_candidates", "list_records", "lookup_settlement",
                     "list_records", "list_records"]
    assert result.calls_used == 6 and result.unavailable == []
    assert store.get_source("po_5001").connector == "merchant_lookup"

    # When the merchant knows nothing, at most two references are tried and nothing is faked.
    fx2 = copy.deepcopy(fx)
    store2 = real_world(tmp_path, keystore, registry, fx2, now, name="real2.db")
    asked.clear()
    result, _ = real_run(store2, RFC_A, registry, now, lookup=lambda ref: asked.append(ref))
    claim = claim_of(result)
    assert asked == ["PO-5001", "STL-88001"]
    assert (claim.status, claim.gap) == (UNRESOLVED, "settlement_not_found")
    assert result.links[0].link_state == LinkState.UNMATCHED


# --------------------------------------------------------------------------- NEEDS item 4


def test_counterparty_and_withdrawal_ref_reach_the_passport(tmp_path, keystore, registry, now):
    fx = maya.load_variant("supported")
    fx["bank_txns"][0]["counterparty"] = "Threadline Payments"
    record_of(fx, "po_5001")["withdrawal_ref"] = "W-5716"
    store = real_world(tmp_path, keystore, registry, fx, now)
    assert store.get_bank_txn("txn_9001").counterparty == "Threadline Payments"
    assert store.get_source("po_5001").withdrawal_ref == "W-5716"
    # A withdrawal reference opens the settlement lookup like a payment reference does.
    asked = []
    gateway = Gateway(store, subject_ref=SUBJ, run_id="run_w", registry=registry, now=now,
                      settlement_lookup=lambda ref: asked.append(ref))
    assert gateway.lookup_settlement("W-5716") is None and asked == ["W-5716"]
    alert = Alert(alert_id="alr_1", subject_ref=SUBJ, kind=AlertKind.SETTLED_NOT_RECEIVED,
                  severity="high", title="t", explanation="e", created_at=now)
    store.save_alert(alert)
    assert store.list_alerts(SUBJ)[0].kind == "settled_not_received"
    store.close()


# --------------------------------------------------------------------------- the demo cases


def test_rules_verdicts_on_the_demo_cases_for_all_three_questions(tmp_path):
    from agents.data.api import load_case
    from alibi.app import cases
    from alibi.app.config import APP_KEYS

    at = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    app_keys = DemoKeystore(tmp_path / "app_keys", specs=APP_KEYS).ensure()
    verdicts = {}
    for number in (1, 2):
        case = load_case(number)
        store = api.open_store(tmp_path / f"c{number}.db")
        loaded = cases.load_case(store, case, app_keys)
        lookup = cases.settlement_lookup_for(case, app_keys, lambda: at)
        for request_id in loaded["requests"]:
            result = api.run_investigation(store, request_id, mode="rules", settlement_lookup=lookup,
                                           registry=app_keys.registry(), now=at)
            for claim in result.claims:
                verdicts[(number, claim.question_id)] = (claim.status.value, claim.gap, claim.facts)
        store.close()
    assert verdicts[(1, "q.source_of_funds")] == (
        "supported", None, {"payout_references": ["PO-0001-01"], "settled_at": ["2026-10-27T16:00:00Z"]})
    assert verdicts[(1, "q.breakdown")] == (
        "supported", None, {"breakdown_minor": {"sales": 240000, "refunds": 18000, "fees": 6000,
                                                "net": 216000}})
    assert verdicts[(1, "q.sales_refunds_in_period")] == (
        "supported", None, {"sales_minor": 240000, "sale_count": 8, "refunds_minor": 18000,
                            "refund_count": 2})
    assert verdicts[(2, "q.source_of_funds")] == ("contradicted", "settlement_pending_at_booking", {})
    assert verdicts[(2, "q.breakdown")] == ("unresolved", "settlement_pending_at_booking", {})
    assert verdicts[(2, "q.sales_refunds_in_period")] == (
        "supported", None, {"sales_minor": 240000, "sale_count": 8, "refunds_minor": 18000,
                            "refund_count": 2})
    assert "MerchantEvent" in str(MerchantEvent)   # keeps the import honest for the mapping above
