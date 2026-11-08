"""Rules-only investigation: matching, fixed retrieval, reconciliation and claim statuses.

Two layers. The unit layer fills a Store directly and drives ``run_investigation`` through
``FakeGateway``, a small stand-in that follows the documented ``alibi.gateway.Gateway``
interface. The integration layer uses the real ``alibi.gateway.Gateway`` and
``alibi.core.ingest`` and is skipped until those modules exist.
"""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alibi.core import canon, reconcile
from alibi.core.investigate_rules import (
    UNMAPPED_GAP,
    BudgetExceeded,
    GatewayError,
    NotFoundError,
    ScopeError,
    Unavailable,
    classify_question,
    next_run_id,
    reference_tokens,
    run_investigation,
)
from alibi.core.schemas import (
    BankTxn,
    ClaimStatus,
    DerivedState,
    EvidenceRecord,
    EvidenceRef,
    LinkState,
    MerchantEvent,
    RecordKind,
    RequestForContext,
    SignatureEnvelope,
    SigningDomain,
    SourceTrust,
    TraceEvent,
)
from alibi.core.signing import verify
from alibi.core.store import NotFound, Store
from alibi.fixtures import maya

SUBJ = maya.SUBJECT_REF
RFC_A, RFC_B = maya.RFC_A_ID, maya.RFC_B_ID
RECEIVED = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
CORRECTED = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
SUPPORTED = ClaimStatus.SUPPORTED
CONTRADICTED = ClaimStatus.CONTRADICTED
UNRESOLVED = ClaimStatus.UNRESOLVED
NOT_SUPPORTED = {CONTRADICTED, UNRESOLVED}


# --------------------------------------------------------------------------- gateway double

_OUTCOMES = ((BudgetExceeded, "budget_exceeded"), (Unavailable, "unavailable"),
             (ScopeError, "refused"), (NotFoundError, "not_found"))


class FakeGateway:
    """Follows the documented ``alibi.gateway.Gateway`` interface: each call checks the budget
    (refused calls raise ``BudgetExceeded`` and are not counted), then the subject scope, then
    runs and appends a TraceEvent. The settlement lookup ingests the signed payload."""

    def __init__(self, store, *, subject_ref, run_id, registry=None, budget=12,
                 settlement_lookup=None, txn_refresh=None, now=None):
        self.store, self.subject_ref, self.run_id = store, subject_ref, run_id
        self.registry, self.budget = registry, budget
        self.settlement_lookup, self.txn_refresh = settlement_lookup, txn_refresh
        self.now = now or datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        self.calls_used = 0
        self.trace: list[TraceEvent] = []

    def _event(self, tool, args, result, outcome="ok", reason=None):
        seq = len(self.trace)
        self.trace.append(TraceEvent(
            trace_id=canon.make_id("trc", self.run_id, "gateway", seq), run_id=self.run_id,
            seq=seq, at=self.now, actor="gateway", step="tool_call", tool=tool, args=args,
            result=result, outcome=outcome, reason=reason))

    def _call(self, tool, args, run):
        if self.calls_used >= self.budget:
            self._event(tool, args, {}, "budget_exceeded", "call budget used up")
            raise BudgetExceeded(f"{tool}: the call budget of {self.budget} is used up")
        self.calls_used += 1
        try:
            value, summary = run()
        except GatewayError as exc:
            outcome = next((o for cls, o in _OUTCOMES if isinstance(exc, cls)), "error")
            self._event(tool, args, {}, outcome, str(exc)[:300])
            raise
        self._event(tool, args, summary)
        return value

    def _scoped(self, obj):
        if obj.subject_ref != self.subject_ref:
            raise ScopeError("that object belongs to another subject")
        return obj

    def get_source(self, record_id, revision=None):
        def run():
            try:
                record = self._scoped(self.store.get_source(record_id, revision))
            except NotFound:
                raise NotFoundError(f"no record {record_id}") from None
            return record, {"ref": record.ref().key(), "kind": record.kind.value}
        return self._call("get_source", {"record_id": record_id}, run)

    def get_txn(self, txn_id):
        def run():
            try:
                txn = self._scoped(self.store.get_bank_txn(txn_id))
            except NotFound:
                raise NotFoundError(f"no bank txn {txn_id}") from None
            return txn, {"ref": txn.ref().key()}
        return self._call("get_txn", {"txn_id": txn_id}, run)

    def list_records(self, *, kinds, period=None):
        def run():
            records = self.store.list_sources(self.subject_ref, kinds=kinds)
            if period is not None:
                records = [r for r in records if period.contains(r.occurred_at)]
            return records, {"refs": [r.ref().key() for r in records]}
        return self._call("list_records", {"kinds": [str(k) for k in kinds]}, run)

    def list_candidates(self, txn_id, window=timedelta(days=5), kinds=(RecordKind.PAYOUT,)):
        def run():
            try:
                txn = self._scoped(self.store.get_bank_txn(txn_id))
            except NotFound:
                raise NotFoundError(f"no bank txn {txn_id}") from None
            low, high = txn.booked_at - window, txn.booked_at + window
            records = [r for r in self.store.list_sources(self.subject_ref, kinds=kinds)
                       if low <= r.occurred_at < high]
            return records, {"refs": [r.ref().key() for r in records]}
        return self._call("list_candidates",
                          {"txn_id": txn_id, "window_s": int(window.total_seconds())}, run)

    def lookup_settlement(self, payment_ref):
        def run():
            if self.settlement_lookup is None:
                raise Unavailable("no settlement connector is configured")
            payload = self.settlement_lookup(payment_ref)
            if payload is None:
                return None, {"found": False}
            event = payload["event"]
            verify(event, payload["signature"], self.registry, domain=SigningDomain.RECORD,
                   issuer_id=event["issuer_id"])
            record = EvidenceRecord.from_event(
                self._scoped(MerchantEvent.model_validate(event)),
                source_trust=SourceTrust.ISSUER_VERIFIED, connector="merchant_lookup",
                received_at=self.now, payload_sha256=canon.digest(payload),
                issuer_signature=SignatureEnvelope.model_validate(payload["signature"]),
                signed_body=event)
            # Like an ingest, this returns the stored row: on a replay of a superseded
            # revision that is the old revision, not the active one.
            stored = self.store.put_source(record, raw_payload=payload).record
            return stored, {"found": True, "ref": stored.ref().key()}
        return self._call("lookup_settlement", {"payment_ref": payment_ref}, run)

    def refresh_txn(self, txn_id):
        def run():
            raise Unavailable("no bank refresh connector is configured")
        return self._call("refresh_txn", {"txn_id": txn_id}, run)

    def flush_trace(self, store):
        store.append_traces(self.trace)


class LoggingGateway(FakeGateway):
    """The double plus the real gateway's ``log_step`` hook: a caller's steps join its sequence."""

    def log_step(self, step, *, actor, tool=None, args=None, result=None, refs=(), outcome="ok",
                 reason=None, question_id=None):
        seq = len(self.trace)
        event = TraceEvent(
            trace_id=canon.make_id("trc", self.run_id, "gateway", seq), run_id=self.run_id,
            seq=seq, at=self.now, actor=actor, step=step, tool=tool, args=args or {},
            result=result or {}, refs=list(refs), outcome=outcome, reason=reason,
            question_id=question_id)
        self.trace.append(event)
        return event


# --------------------------------------------------------------------------- helpers


def rref(record_id, revision=1):
    return EvidenceRef(ref_type="record", ref_id=record_id, revision=revision)


def tref(txn_id, revision=1):
    return EvidenceRef(ref_type="txn", ref_id=txn_id, revision=revision)


def keys(refs):
    return {ref.key() for ref in refs}


def put_signed(store, keystore, raw, *, connector="merchant_webhook", received_at=RECEIVED):
    payload = maya.sign_event(raw, keystore)
    record = EvidenceRecord.from_event(
        MerchantEvent.model_validate(payload["event"]), source_trust=SourceTrust.ISSUER_VERIFIED,
        connector=connector, received_at=received_at, payload_sha256=canon.digest(payload),
        issuer_signature=SignatureEnvelope.model_validate(payload["signature"]),
        signed_body=payload["event"])
    return store.put_source(record, raw_payload=payload)


def put_self_supplied(store, raw, *, received_at=RECEIVED):
    payload = {"event": raw}
    record = EvidenceRecord.from_event(
        MerchantEvent.model_validate(raw), source_trust=SourceTrust.SELF_SUPPLIED,
        connector="self_supplied_import", received_at=received_at,
        payload_sha256=canon.digest(payload))
    return store.put_source(record, raw_payload=payload)


def variant(name, edit=None):
    fx = copy.deepcopy(maya.load_variant(name))
    if edit is not None:
        edit(fx)
    return fx


def record_of(fx, record_id, section="merchant_events"):
    return next(e for e in fx[section] if e["record_id"] == record_id)


def rfc(request_id, questions, **fields):
    data = {"issuer": "bank_a", "audience": "bank_a", "request_id": request_id,
            "subject_ref": SUBJ, "issued_at": "2026-11-05T09:00:00Z",
            "expires_at": "2026-11-12T09:00:00Z",
            "questions": [{"question_id": f"q{i}", "text": text}
                          for i, text in enumerate(questions, start=1)]}
    data.update(fields)
    return RequestForContext.model_validate(data)


@pytest.fixture
def passport(tmp_path, keystore):
    """Factory: a Store filled from a fixture (webhook events, bank txns and requests)."""
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


def investigate(store, fx, keystore, now, request_id=RFC_A, *, lookup=True,
                include_corrections=False, budget=12, **kwargs):
    lookup_fn = (maya.settlement_lookup(fx, keystore, include_corrections=include_corrections)
                 if lookup else None)
    request = store.get_request(request_id)
    gateway = FakeGateway(store, subject_ref=SUBJ,
                          run_id=next_run_id(store, request_id, audience=request.audience),
                          registry=keystore.registry(), budget=budget,
                          settlement_lookup=lookup_fn, now=now)
    result = run_investigation(request, store, gateway, now=now, **kwargs)
    return result, gateway


def claim_of(result, question_id="q1"):
    (claim,) = [c for c in result.claims if c.question_id == question_id]
    return claim


def tools(gateway):
    return [event.tool for event in gateway.trace]


# --------------------------------------------------------------------------- classification


def test_questions_are_classified_by_keyword():
    raw_a, raw_b = maya.load_variant("supported")["rfcs"]
    a, b = RequestForContext.model_validate(raw_a), RequestForContext.model_validate(raw_b)
    assert classify_question(a.questions[0].text, a).category == "source_of_funds"
    assert classify_question(b.questions[0].text, b).category == "sales_total"
    assert classify_question(b.questions[1].text, b).category == "refunds_total"
    assert classify_question("What COMMISSION did the platform take?", b).category == "fees_total"
    net = classify_question("What did you earn after refunds and fees?", b)
    assert (net.category, net.kinds) == ("net_total", (RecordKind.ORDER, RecordKind.REFUND,
                                                       RecordKind.FEE))
    # Source questions need txn_ids; totals need a period; anything else is unmapped.
    assert classify_question("Where did this credit come from?", b).category == "unmapped"
    assert classify_question("Please confirm your sales.", a).category == "unmapped"
    assert classify_question("Do you hold any cryptocurrency?", a).category == "unmapped"


def test_reference_tokens_come_from_refs_and_uppercase_description_tokens():
    supported = BankTxn.model_validate(maya.load_variant("supported")["bank_txns"][0])
    assert {"PO-5001", "STL-88001"} <= reference_tokens(supported)
    # Description words without a digit (INCOMING, TRANSFER, PAYOUT) are never references.
    bare = BankTxn.model_validate(maya.load_variant("contradiction")["bank_txns"][0])
    assert reference_tokens(bare) == frozenset()
    # The description is upper-cased first, so mixed-case references still count.
    mixed = bare.model_copy(update={"description": "Threadline payout ref Po-5001 / Stl-88001"})
    assert reference_tokens(mixed) == {"PO-5001", "STL-88001"}
    # Explicit bank refs always count, digits or not.
    worded = bare.model_copy(update={"refs": ["TRANSFER"]})
    assert reference_tokens(worded) == {"TRANSFER"}


# --------------------------------------------------------------------------- RFC A variants


def test_supported_credit_links_by_reference_and_q1_is_supported(passport, keystore, now):
    store, fx = passport("supported")
    result, gateway = investigate(store, fx, keystore, now)

    (link,) = result.links
    assert (link.link_state, link.method, link.record_ref) == (LinkState.LINKED, "reference",
                                                               rref("po_5001"))
    assert link.candidates == [rref("po_5001")] and link.txn_ref == tref("txn_9001")
    gold_link = fx["expected"]["links"]["txn_9001"]
    assert (link.link_state.value, link.method, link.record_ref.ref_id) \
        == (gold_link["link_state"], gold_link["method"], gold_link["record_id"])

    claim = claim_of(result)
    assert claim.status == SUPPORTED and claim.gap is None
    gold = fx["expected"]["claims"][RFC_A]["q1"]
    assert claim.status.value in gold["statuses"]
    assert (keys(claim.evidence_refs) == set()) if claim.status == UNRESOLVED else (set(gold["evidence"]) <= keys(claim.evidence_refs))
    assert "record:stl_88001@1" in keys(claim.evidence_refs)
    assert set(maya.COMPONENT_IDS) <= {ref.ref_id for ref in claim.evidence_refs}
    calc = claim.calculation
    assert (calc.total_pence, calc.target_ref, calc.target_pence, calc.difference_pence,
            calc.balanced) == (216000, tref("txn_9001"), 216000, 0, True)
    assert calc.timing.ok and calc.timing.settlement_ref == rref("stl_88001")
    assert reconcile.credit_supported(calc)
    assert "stl_88001" in claim.statement and "216000" in claim.rationale
    assert "follow-up lookup" in claim.rationale
    # Fixed retrieval: one txn read, one candidate list, the lookup, the passport's settlements
    # for the reference, and one listing for the six components.
    assert tools(gateway) == ["get_txn", "list_candidates", "lookup_settlement", "list_records",
                              "list_records"]
    assert result.calls_used == gateway.calls_used == 5 and result.unavailable == []


def test_contradiction_is_never_supported(passport, keystore, now):
    """Lead's required test: a settlement still pending after the booking never supports."""
    store, fx = passport("contradiction")
    result, _ = investigate(store, fx, keystore, now)

    (link,) = result.links
    assert (link.link_state, link.method, link.record_ref) == (LinkState.LINKED, "amount_date",
                                                               rref("po_5001"))
    claim = claim_of(result)
    assert claim.status != SUPPORTED
    assert claim.status in NOT_SUPPORTED
    assert claim.status.value in fx["expected"]["claims"][RFC_A]["q1"]["statuses"]
    assert claim.status == CONTRADICTED  # the pending status conflicts with the claim
    assert "record:stl_88001@1" in keys(claim.evidence_refs)  # cites the conflicting settlement
    assert claim.calculation.balanced is True  # the amounts still reconcile...
    assert claim.calculation.timing.ok is False  # ...but the timing does not
    assert "remains unresolved" in claim.gap_detail
    assert [c.status for c in store.list_claims(request_id=RFC_A)] == [claim.status]


def test_ambiguous_payouts_leave_q1_unresolved(passport, keystore, now):
    store, fx = passport("ambiguous")
    result, gateway = investigate(store, fx, keystore, now)

    (link,) = result.links
    assert (link.link_state, link.method, link.record_ref) == (LinkState.AMBIGUOUS, "amount_date",
                                                               None)
    assert link.candidates == [rref("po_5001"), rref("po_5002")]
    claim = claim_of(result)
    assert claim.status == UNRESOLVED
    assert claim.status.value in fx["expected"]["claims"][RFC_A]["q1"]["statuses"]
    assert keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record
    # The gap gives the number of candidates, not their contents.
    assert claim.gap_detail.startswith("2 payout records")
    assert not any(word in claim.gap_detail for word in ("po_500", "PO-500", "216000"))
    assert "lookup_settlement" not in tools(gateway)  # nothing matched, so nothing to retrieve


@pytest.mark.parametrize("connector_has_correction", [True, False],
                         ids=["lookup-serves-revision-2", "lookup-serves-stale-revision-1"])
def test_pending_correction_invalidates_the_claim_and_a_rerun_is_not_supported(
        passport, keystore, now, connector_has_correction):
    store, fx = passport("pending_correction")
    first, _ = investigate(store, fx, keystore, now)
    before = claim_of(first)
    assert before.status == SUPPORTED and "record:stl_88001@1" in keys(before.evidence_refs)

    (fix,) = fx["corrections"]
    put = put_signed(store, keystore, fix, received_at=CORRECTED)
    assert put.status == "corrected"
    assert put.invalidated["claims"] == [before.claim_id]
    assert put.invalidated["links"] == []  # the link depends on the credit and payout only
    (old,) = [c for c in store.list_claims(request_id=RFC_A, state=None)
              if c.claim_id == before.claim_id]
    assert old.state == DerivedState.INVALIDATED

    second, _ = investigate(store, fx, keystore, now, include_corrections=connector_has_correction)
    assert second.run_id != first.run_id
    after = claim_of(second)
    assert after.status in NOT_SUPPORTED and after.state == DerivedState.ACTIVE
    gold = fx["expected"]["claims"][RFC_A]["q1"]["after_correction"]
    assert after.status.value in gold["statuses"]
    assert set(gold["evidence"]) <= keys(after.evidence_refs)
    assert "record:stl_88001@1" not in keys(after.evidence_refs)
    (link,) = second.links
    assert (link.link_state, link.record_ref) == (LinkState.LINKED, rref("po_5001"))
    assert [c.claim_id for c in store.list_claims(request_id=RFC_A)] == [after.claim_id]


def test_no_settlement_connector_and_none_in_the_passport_is_unresolved(passport, keystore, now):
    store, fx = passport("supported")
    result, gateway = investigate(store, fx, keystore, now, lookup=False)

    assert result.links[0].link_state == LinkState.LINKED  # linked, yet the claim is open
    claim = claim_of(result)
    assert claim.status == UNRESOLVED
    assert claim.gap_detail.startswith("No settlement record for payout PO-5001")
    assert "unavailable" in claim.gap_detail
    ids = {ref.ref_id for ref in claim.evidence_refs}
    assert "msg_7001" not in ids and not any(i.startswith("stl_") for i in ids)
    assert "unavailable: lookup_settlement PO-5001" in result.unavailable
    assert tools(gateway)[2:4] == ["lookup_settlement", "list_records"]


@pytest.mark.parametrize("name, expected", [("supported", SUPPORTED),
                                            ("contradiction", CONTRADICTED)])
def test_a_settlement_already_in_the_passport_is_used_when_the_lookup_is_unavailable(
        passport, keystore, now, name, expected):
    store, fx = passport(name)
    (settlement,) = fx["lookup_events"]
    put_signed(store, keystore, settlement, connector="merchant_lookup")
    result, _ = investigate(store, fx, keystore, now, lookup=False)

    claim = claim_of(result)
    assert claim.status == expected
    assert ("record:stl_88001@1" in keys(claim.evidence_refs)) == (expected != UNRESOLVED)
    assert "already in the passport" in claim.rationale
    paths = [e.result.get("path") for e in result.trace if e.actor == "rules"]
    assert "passport" in paths
    assert any(r.startswith("unavailable") for r in result.unavailable)


@pytest.mark.parametrize("name, late_id, state, record_id", [
    ("supported", "po_5001", LinkState.UNMATCHED, None),     # named by the credit's reference
    ("contradiction", "po_5001", LinkState.UNMATCHED, None),  # amount and date only
    ("ambiguous", "po_5002", LinkState.LINKED, "po_5001"),   # the late twin is no candidate
])
def test_a_payout_after_the_booking_never_links(passport, keystore, now, name, late_id, state,
                                                record_id):
    def late(fx):
        record_of(fx, late_id)["occurred_at"] = "2026-11-03T11:00:00Z"  # booked at 10:00

    fx = variant(name, late)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)

    (link,) = result.links
    assert link.link_state == state
    assert rref(late_id) not in link.candidates and link.record_ref != rref(late_id)
    assert (link.record_ref.ref_id if link.record_ref else None) == record_id
    assert any("after the booking" in signal for signal in link.signals)
    claim = claim_of(result)
    assert claim.status == UNRESOLVED  # unmatched, or matched by amount and date alone


def test_matching_amounts_alone_never_support_a_claim(passport, keystore, now):
    def settled_in_time(fx):
        fx["lookup_events"] = copy.deepcopy(maya.load_variant("supported")["lookup_events"])

    fx = variant("contradiction", settled_in_time)  # a bare credit, a settlement in time
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)

    assert result.links[0].method == "amount_date"
    claim = claim_of(result)
    assert claim.calculation is None and claim.evidence_refs == [] and claim.examined_refs
    calc = [e.result for e in result.trace if e.step == "calculation"][-1]
    assert calc["balanced"] is True and calc["timing_ok"] is True
    assert claim.status == UNRESOLVED
    assert "alone" in claim.gap_detail


@pytest.mark.parametrize("change, expected", [
    ({"status": "reversed"}, CONTRADICTED),
    # A different amount leaves part of the credit unexplained, not a conflicting claim.
    ({"amount_pence": 200000}, UNRESOLVED),
    ({"effective_at": "2026-11-03T11:00:00Z", "occurred_at": "2026-11-03T11:30:00Z"},
     CONTRADICTED),
    ({"status": "pending", "effective_at": None, "occurred_at": "2026-11-03T08:00:00Z"},
     UNRESOLVED),
    # Settled without effective_at takes effect when observed (DESIGN), here after the booking.
    ({"effective_at": None, "occurred_at": "2026-11-03T12:00:00Z"}, CONTRADICTED),
    ({"status": "on_hold"}, UNRESOLVED),
], ids=["failed", "amount-differs", "settled-after-booking", "pending-before-booking",
        "settled-seen-late", "unknown-status"])
def test_explicit_settlement_fields_decide_the_status(passport, keystore, now, change, expected):
    def edit(fx):
        settlement = fx["lookup_events"][0]
        for key, value in change.items():
            if value is None:
                settlement.pop(key, None)
            else:
                settlement[key] = value

    fx = variant("supported", edit)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert claim.status == expected
    assert ("record:stl_88001@1" in keys(claim.evidence_refs)) == (expected != UNRESOLVED)
    assert claim.gap
    if "occurred_at" in change and change.get("status") != "pending":
        assert "after the credit was booked" in claim.gap_detail and "remains unresolved" in claim.gap_detail


# --------------------------------------------------------------------------- RFC B and totals


@pytest.mark.parametrize("name", maya.VARIANTS)
def test_rfc_b_sales_and_refunds_are_supported_in_every_variant(passport, keystore, now, name):
    store, fx = passport(name)
    result, _ = investigate(store, fx, keystore, now, RFC_B)
    gold = fx["expected"]["claims"][RFC_B]
    for question_id, total in (("q1", 240000), ("q2", 18000)):
        claim = claim_of(result, question_id)
        assert claim.status == SUPPORTED
        assert claim.calculation.total_pence == total == gold[question_id]["total_pence"]
        assert claim.calculation.currency == "GBP"
        assert [ref.key() for ref in claim.evidence_refs] == gold[question_id]["evidence"]
        assert str(total) in claim.statement
    assert result.links == []


def test_period_totals_count_self_supplied_records_and_skip_unfinished_ones(passport, keystore,
                                                                           now):
    store, fx = passport("supported")
    order = {"record_id": "self_ord_9001", "revision": 1, "issuer_id": "self_supplied",
             "subject_ref": SUBJ, "kind": "order", "occurred_at": "2026-10-20T12:00:00Z",
             "amount_pence": 5000, "currency": "GBP", "status": "completed",
             "event_id": "evt_self_ord_9001"}
    put_self_supplied(store, order)
    for record_id, when, status in (("ord_1005", "2026-10-28T12:00:00Z", "cancelled"),
                                    ("ord_1006", "2026-11-01T00:00:00Z", "completed")):
        put_signed(store, keystore, {**order, "record_id": record_id, "issuer_id": "threadline",
                                     "event_id": f"evt_{record_id}_r1", "occurred_at": when,
                                     "status": status, "amount_pence": 9000})
    result, _ = investigate(store, fx, keystore, now, RFC_B)

    sales = claim_of(result, "q1")
    assert sales.status == SUPPORTED and sales.calculation.total_pence == 245000
    ids = {ref.ref_id for ref in sales.evidence_refs}
    assert "self_ord_9001" in ids
    assert "ord_1005" not in ids  # cancelled
    assert "ord_1006" not in ids  # the period is half-open: [Oct 1, Nov 1)
    assert "self-supplied" in sales.rationale and "self_ord_9001" in sales.rationale
    assert "ord_1005 (status cancelled)" in sales.rationale
    assert claim_of(result, "q2").calculation.total_pence == 18000


def test_net_totals_fee_totals_and_unmapped_questions(passport, keystore, now):
    store, fx = passport("supported")
    store.save_request(rfc("rfc_c_001", [
        "What were your net sales after refunds and fees?",
        "What commission or fees did the platform charge?",
        "Do you hold any cryptocurrency?",
    ], period=maya.RFC_B_PERIOD, issuer="bank_b", audience="bank_b"))
    result, _ = investigate(store, fx, keystore, now, "rfc_c_001")

    net = claim_of(result, "q1")
    assert net.status == SUPPORTED and net.calculation.total_pence == 216000
    assert sorted((t.ref.ref_id, t.sign) for t in net.calculation.terms) == [
        ("fee_3001", -1), ("ord_1001", 1), ("ord_1002", 1), ("ord_1003", 1), ("ord_1004", 1),
        ("rfd_2001", -1)]
    for figure in ("240000", "18000", "6000", "216000"):
        assert figure in net.rationale
    fees = claim_of(result, "q2")
    assert fees.status == SUPPORTED and fees.calculation.total_pence == 6000
    assert keys(fees.evidence_refs) == {"record:fee_3001@1"}
    other = claim_of(result, "q3")
    assert (other.status, other.gap, other.gap_detail, other.evidence_refs) \
        == (UNRESOLVED, "question_not_mapped", UNMAPPED_GAP, [])


# --------------------------------------------------------------------------- other cases


def test_a_debit_is_unresolved_without_a_link(passport, keystore, now):
    store, fx = passport("retail_purchase")
    store.save_request(rfc("rfc_d_001", ["What was this payment for?"], txn_ids=["txn_9002"]))
    result, _ = investigate(store, fx, keystore, now, "rfc_d_001")
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "debit" in claim.gap_detail
    assert result.links == [] and keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record


def test_several_transactions_share_one_claim_and_the_worst_status_wins(passport, keystore, now):
    store, fx = passport("supported")
    debit = maya.load_variant("retail_purchase")["bank_txns"][0]
    store.put_bank_txns([BankTxn.model_validate(debit)], connector="bank_feed")
    store.save_request(rfc("rfc_e_001", ["Please explain the source of these transactions."],
                           txn_ids=["txn_9001", "txn_9002"]))
    result, _ = investigate(store, fx, keystore, now, "rfc_e_001")
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "debit" in claim.gap_detail
    assert keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record
    assert "txn_9001" in claim.statement and "txn_9002" in claim.statement
    assert [link.txn_ref.ref_id for link in result.links] == ["txn_9001"]


@pytest.mark.parametrize("budget", [0, 1, 2, 3, 4])
def test_budget_exhaustion_gives_a_typed_unresolved_reason(passport, keystore, now, budget):
    store, fx = passport("supported")
    result, gateway = investigate(store, fx, keystore, now, budget=budget)

    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "budget_exceeded" in claim.gap_detail
    assert result.calls_used == gateway.calls_used == budget
    assert result.unavailable
    assert all(reason.startswith("budget_exceeded: ") for reason in result.unavailable)
    assert [c.claim_id for c in store.list_claims(request_id=RFC_A)] == [claim.claim_id]
    refused = [e for e in result.trace if e.actor == "rules" and e.outcome == "budget_exceeded"]
    assert refused


def test_the_rules_baseline_is_deterministic(passport, keystore, now):
    store_a, fx = passport("contradiction", "a.db")
    store_b, _ = passport("contradiction", "b.db")
    a, _ = investigate(store_a, fx, keystore, now)
    b, _ = investigate(store_b, fx, keystore, now)
    assert a.to_json_dict() == b.to_json_dict()  # same store state, same result

    rerun, _ = investigate(store_a, fx, keystore, now)
    assert (a.run_id, rerun.run_id) == (canon.make_id("run", "bank_a", RFC_A, "rules", 0),
                                        canon.make_id("run", "bank_a", RFC_A, "rules", 1))
    for first, again in zip(a.claims, rerun.claims, strict=True):
        assert first.claim_id != again.claim_id
        assert (first.status, keys(first.evidence_refs), first.statement, first.gap) \
            == (again.status, keys(again.evidence_refs), again.statement, again.gap)
    assert [(l.link_state, l.record_ref, l.candidates) for l in a.links] \
        == [(l.link_state, l.record_ref, l.candidates) for l in rerun.links]
    assert next_run_id(store_a, RFC_A, audience="bank_a") == canon.make_id("run", "bank_a", RFC_A, "rules", 2)


def test_links_claims_and_trace_are_saved(passport, keystore, now):
    store, fx = passport("supported")
    result, gateway = investigate(store, fx, keystore, now)
    run_id = result.run_id
    assert run_id == canon.make_id("run", "bank_a", RFC_A, "rules", 0) == gateway.run_id
    assert result.mode == "rules" and result.request_id == RFC_A

    (claim,) = result.claims
    assert claim.claim_id == canon.make_id("clm", run_id, "q1") and claim.run_id == run_id
    (link,) = result.links
    assert link.link_id == canon.make_id("lnk", run_id, "txn_9001") and link.run_id == run_id
    assert store.list_links(txn_id="txn_9001") == [link]
    assert store.list_claims(request_id=RFC_A) == [claim]

    stored = store.list_traces(run_id)
    assert [e.trace_id for e in stored] == [e.trace_id for e in result.trace]
    assert len({e.trace_id for e in stored}) == len(stored)
    assert {e.tool for e in stored if e.actor == "gateway"} \
        == {"get_txn", "list_candidates", "lookup_settlement", "list_records"}
    rules = [e for e in stored if e.actor == "rules"]
    assert {"note", "validation", "calculation", "status_change"} <= {e.step for e in rules}
    assert min(e.seq for e in rules) > max(e.seq for e in stored if e.actor == "gateway")
    (change,) = [e for e in rules if e.step == "status_change"]
    assert change.question_id == "q1" and change.reason
    assert change.result == {"claim_id": claim.claim_id, "status": "supported", "gap": "none"}
    assert keys(change.refs) == keys(claim.evidence_refs)
    (matching,) = [e for e in rules if e.step == "validation" and e.tool == "list_candidates"]
    assert matching.result["link_state"] == "linked" and matching.result["method"] == "reference"
    assert any(e.result.get("path") == "lookup" for e in rules)


def test_rules_steps_join_the_gateway_sequence_when_it_offers_log_step(passport, keystore, now):
    store, fx = passport("supported")
    gateway = LoggingGateway(store, subject_ref=SUBJ, run_id=next_run_id(store, RFC_A, audience="bank_a"),
                             registry=keystore.registry(), now=now,
                             settlement_lookup=maya.settlement_lookup(fx, keystore))
    result = run_investigation(store.get_request(RFC_A), store, gateway, now=now)

    assert claim_of(result).status == SUPPORTED
    assert result.trace == gateway.trace
    assert [e.seq for e in result.trace] == list(range(len(result.trace)))
    order = [(e.actor, e.step, e.tool) for e in result.trace]
    assert order[0] == ("rules", "note", None)  # the classification comes first
    assert order[1] == ("gateway", "tool_call", "get_txn")
    matching = order.index(("rules", "validation", "list_candidates"))
    assert order[matching - 1] == ("gateway", "tool_call", "list_candidates")
    assert order[-1][:2] == ("rules", "status_change")
    assert [e.trace_id for e in store.list_traces(result.run_id)] \
        == [e.trace_id for e in result.trace]


@pytest.mark.parametrize("mode", ["agents", "single_call", "RULES"])
def test_other_modes_are_not_implemented(passport, keystore, now, mode):
    store, fx = passport("supported")
    gateway = FakeGateway(store, subject_ref=SUBJ, run_id=next_run_id(store, RFC_A, audience="bank_a"), now=now)
    with pytest.raises(NotImplementedError):
        run_investigation(store.get_request(RFC_A), store, gateway, mode=mode, now=now)
    assert gateway.calls_used == 0 and gateway.trace == []
    assert store.list_claims(request_id=RFC_A, state=None) == []
    assert store.list_links(state=None) == []


def test_a_gateway_for_another_subject_is_refused(passport, keystore, now):
    store, fx = passport("supported")
    gateway = FakeGateway(store, subject_ref="subj_other", run_id="run_x", now=now)
    with pytest.raises(ValueError):
        run_investigation(store.get_request(RFC_A), store, gateway, now=now)
    assert gateway.calls_used == 0 and store.list_claims(request_id=RFC_A, state=None) == []


# --------------------------------------------------------------------------- reviewer regressions

TOTALS_QUESTIONS = ["What were your total sales?", "What refunds did you issue?",
                    "What fees did the platform charge?",
                    "What did you earn after refunds and fees?"]
SELF_PAYOUT = {"record_id": "self_po_9", "event_id": "evt_self_po_9", "revision": 1,
               "issuer_id": "self_supplied", "subject_ref": SUBJ, "kind": "payout",
               "occurred_at": "2026-11-02T09:00:00Z", "amount_pence": 216000, "currency": "GBP",
               "payment_ref": "TRANSFER", "status": "paid",
               "related_record_ids": list(maya.COMPONENT_IDS)}
SELF_SETTLEMENT = {"record_id": "self_stl_9", "event_id": "evt_self_stl_9", "revision": 1,
                   "issuer_id": "self_supplied", "subject_ref": SUBJ, "kind": "settlement",
                   "occurred_at": "2026-11-03T08:00:00Z", "effective_at": "2026-11-03T07:00:00Z",
                   "amount_pence": 216000, "currency": "GBP", "payment_ref": "TRANSFER",
                   "settlement_ref": "STL-9", "status": "settled"}


def statuses(**by_id):
    """Fixture edit: set the status of the named merchant events."""
    def edit(fx):
        for record_id, status in by_id.items():
            record_of(fx, record_id)["status"] = status
    return edit


def returned_settlement(fx):
    """A later settlement for PO-5001 under its own record id, with status returned."""
    returned = copy.deepcopy(fx["lookup_events"][0])
    returned.update(record_id="stl_88001_ret", event_id="evt_stl_88001_ret_r1",
                    status="returned", occurred_at="2026-11-05T09:00:00Z",
                    settlement_ref="STL-88001-RET")
    returned.pop("effective_at", None)
    return returned


def totals_rfc(store, request_id):
    store.save_request(rfc(request_id, TOTALS_QUESTIONS, period=maya.RFC_B_PERIOD,
                           issuer="bank_b", audience="bank_b"))


@pytest.mark.parametrize("lookup", [True, False], ids=["lookup-works", "lookup-unavailable"])
def test_a_conflicting_settlement_in_the_passport_counts_even_when_the_lookup_works(
        passport, keystore, now, lookup):
    store, fx = passport("supported")
    put_signed(store, keystore, returned_settlement(fx))
    result, gateway = investigate(store, fx, keystore, now, lookup=lookup)

    claim = claim_of(result)
    assert claim.status == CONTRADICTED  # the same verdict whether or not the connector is up
    assert "record:stl_88001_ret@1" in keys(claim.evidence_refs)
    assert "status returned" in claim.gap_detail and "remains unresolved" in claim.gap_detail
    assert tools(gateway)[2:4] == ["lookup_settlement", "list_records"]
    if lookup:  # the lookup's settled record is named and outweighed by the failed one
        assert "record:stl_88001@1 (status settled)" in claim.rationale
        assert "failed status outweighs" in claim.rationale


def test_period_totals_count_ordinary_statuses(passport, keystore, now):
    fx = variant("supported", statuses(ord_1001="shipped", ord_1003="partially_refunded",
                                       rfd_2001="approved", fee_3001="deducted"))
    store, _ = passport(fx)
    totals_rfc(store, "rfc_t_001")
    result, _ = investigate(store, fx, keystore, now, "rfc_t_001")
    assert [(c.status, c.calculation.total_pence) for c in result.claims] == [
        (SUPPORTED, 240000), (SUPPORTED, 18000), (SUPPORTED, 6000), (SUPPORTED, 216000)]


def test_an_unrecognised_status_leaves_the_total_unresolved(passport, keystore, now):
    fx = variant("supported", statuses(ord_1003="on_hold"))
    store, _ = passport(fx)
    totals_rfc(store, "rfc_t_002")
    result, _ = investigate(store, fx, keystore, now, "rfc_t_002")
    sales, refunds, fees, net = result.claims
    for claim in (sales, net):  # never a supported partial total
        assert claim.status == UNRESOLVED and claim.calculation is None
        assert "ord_1003 (on_hold)" in claim.gap_detail
        assert keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record
    assert (refunds.status, refunds.calculation.total_pence) == (SUPPORTED, 18000)
    assert (fees.status, fees.calculation.total_pence) == (SUPPORTED, 6000)


def test_a_net_total_never_states_zero_from_zero_records(passport, keystore, now):
    fx = variant("supported", statuses(rfd_2001="cancelled"))
    store, _ = passport(fx)
    totals_rfc(store, "rfc_t_003")
    result, _ = investigate(store, fx, keystore, now, "rfc_t_003")
    net = claim_of(result, "q4")
    assert net.status == SUPPORTED and net.calculation.total_pence == 234000
    assert "refunds 0 (no counted records)" in net.rationale and "0 from 0" not in net.rationale
    assert "rfd_2001 (status cancelled)" in net.rationale
    assert claim_of(result, "q2").status == UNRESOLVED  # no refund went through


def split_first_order(fx, parts=6):
    """Fixture edit: ord_1001 (60000) becomes ``parts`` equal orders; the totals stay."""
    first = record_of(fx, "ord_1001")
    fx["merchant_events"].remove(first)
    ids = []
    for index in range(parts):
        record_id = f"ord_1001_{index}"
        part = copy.deepcopy(first)
        part.update(record_id=record_id, event_id=f"evt_{record_id}_r1",
                    amount_pence=60000 // parts)
        part["items"] = [{**first["items"][0], "unit_amount_pence": 60000 // parts}]
        fx["merchant_events"].append(part)
        ids.append(record_id)
    payout = record_of(fx, "po_5001")
    payout["related_record_ids"] = ids + [r for r in payout["related_record_ids"]
                                          if r != "ord_1001"]


def test_a_bundled_payout_is_read_in_one_call_within_the_default_budget(passport, keystore,
                                                                        now):
    fx = variant("supported", split_first_order)
    assert len(record_of(fx, "po_5001")["related_record_ids"]) == 11
    store, _ = passport(fx)
    result, gateway = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert claim.status == SUPPORTED and claim.calculation.total_pence == 216000
    assert len(claim.calculation.terms) == 11
    assert "get_source" not in tools(gateway) and result.calls_used == 5


@pytest.mark.parametrize("settlement_status, expected", [("settled", UNRESOLVED),
                                                         ("reversed", CONTRADICTED)])
def test_a_missing_component_is_named_and_never_hides_a_conflict(passport, keystore, now,
                                                                 settlement_status, expected):
    def edit(fx):
        fx["lookup_events"][0]["status"] = settlement_status
        record_of(fx, "po_5001")["related_record_ids"].append("ord_1099")

    fx = variant("supported", edit)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert claim.status == expected
    assert "ord_1099" in claim.gap_detail and claim.calculation is None
    if expected == CONTRADICTED:
        assert "status reversed" in claim.gap_detail and "record:stl_88001@1" in keys(claim.evidence_refs)


def test_a_late_pending_settlement_contradicts_even_when_the_budget_cuts_the_components(
        passport, keystore, now):
    store, fx = passport("contradiction")
    result, gateway = investigate(store, fx, keystore, now, budget=4)
    claim = claim_of(result)
    assert claim.status == CONTRADICTED and "budget_exceeded" in claim.gap_detail
    assert "record:stl_88001@1" in keys(claim.evidence_refs) and claim.calculation is None
    assert result.calls_used == gateway.calls_used == 4


def test_a_failed_settlement_in_another_currency_contradicts(passport, keystore, now):
    def edit(fx):
        fx["lookup_events"][0].update(currency="EUR", status="reversed")

    fx = variant("supported", edit)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    claim = claim_of(result)
    assert claim.status == CONTRADICTED and claim.calculation is None
    assert "status reversed" in claim.gap_detail and "does not convert currencies" in claim.gap_detail


def test_several_categories_give_a_net_total_only_when_the_question_asks_for_one(
        passport, keystore, now):
    store, fx = passport("supported")
    store.save_request(rfc("rfc_n_001", [
        "What refunds did you issue on orders over the same period?",
        "What fees were charged on your sales in October?",
        "In order to complete our review, what refunds did you issue in October?",
        "What were your sales and refunds in October?",
        "What did you earn after refunds and fees?",
    ], period=maya.RFC_B_PERIOD, issuer="bank_b", audience="bank_b"))
    request = store.get_request("rfc_n_001")
    assert [classify_question(q.text, request).category for q in request.questions] == [
        "refunds_total", "fees_total", "refunds_total", "several_totals", "net_total"]

    result, _ = investigate(store, fx, keystore, now, "rfc_n_001")
    assert [(c.status, c.calculation.total_pence if c.calculation else None)
            for c in result.claims] == [(SUPPORTED, 18000), (SUPPORTED, 6000),
                                        (SUPPORTED, 18000), (SUPPORTED, 222000),
                                        (SUPPORTED, 216000)]
    # Two documented totals in one claim (NEEDS item 6): sales and refunds each stated with
    # their count, the records cited, and the signed sum carried as a documented_totals calc.
    several = claim_of(result, "q4")
    assert several.gap is None and several.calculation.method == "documented_totals"
    assert several.facts == {"sales_minor": 240000, "sale_count": 4, "refunds_minor": 18000,
                             "refund_count": 1}
    assert "sales total 240000 pence GBP across 4 records" in several.statement
    assert "refunds total 18000 pence GBP across 1 record" in several.statement
    assert {ref.ref_id for ref in several.evidence_refs} == {*maya.ORDER_IDS, "rfd_2001"}


def test_a_gateway_under_another_run_id_is_refused_before_any_call(passport, keystore, now):
    store, fx = passport("supported")
    lookup = maya.settlement_lookup(fx, keystore)
    fixed = FakeGateway(store, subject_ref=SUBJ, run_id="run_fixed",
                        registry=keystore.registry(), settlement_lookup=lookup, now=now)
    with pytest.raises(ValueError, match="run id"):
        run_investigation(store.get_request(RFC_A), store, fixed, now=now)
    assert fixed.calls_used == 0 and fixed.trace == []
    assert store.list_claims(request_id=RFC_A, state=None) == []

    # A gateway serves one run: a rerun through it is refused instead of half-saved.
    gateway = FakeGateway(store, subject_ref=SUBJ, run_id=next_run_id(store, RFC_A, audience="bank_a"),
                          registry=keystore.registry(), settlement_lookup=lookup, now=now)
    first = run_investigation(store.get_request(RFC_A), store, gateway, now=now)
    calls = gateway.calls_used
    with pytest.raises(ValueError, match="run id"):
        run_investigation(store.get_request(RFC_A), store, gateway, now=now)
    assert gateway.calls_used == calls
    assert store.list_claims(request_id=RFC_A, state=None) == first.claims


def test_a_different_settlement_reference_on_the_credit_is_not_supported(passport, keystore,
                                                                         now):
    def other_ref(fx):
        fx["bank_txns"][0].update(refs=["PO-5001", "STL-99999"],
                                  description="THREADLINE PAYOUT PO-5001 STL-99999")

    fx = variant("supported", other_ref)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    (link,) = result.links
    assert (link.link_state, link.method) == (LinkState.LINKED, "reference")
    claim = claim_of(result)
    assert claim.status == UNRESOLVED
    assert "STL-99999" in claim.gap_detail and "STL-88001" in claim.gap_detail


@pytest.mark.parametrize("credit_refs, state", [([], LinkState.AMBIGUOUS),
                                                (["TRANSFER"], LinkState.LINKED)],
                         ids=["word-in-description", "explicit-ref"])
def test_a_self_supplied_payout_and_settlement_never_support(passport, keystore, now,
                                                             credit_refs, state):
    def refs(fx):
        fx["bank_txns"][0]["refs"] = credit_refs

    fx = variant("contradiction", refs)  # "INCOMING TRANSFER"; the issuer's settlement is late
    store, _ = passport(fx)
    put_self_supplied(store, SELF_PAYOUT)
    put_self_supplied(store, SELF_SETTLEMENT)
    result, _ = investigate(store, fx, keystore, now)
    (link,) = result.links
    assert link.link_state == state  # TRANSFER in the description is not a reference
    claim = claim_of(result)
    assert claim.status == UNRESOLVED
    if state == LinkState.LINKED:
        assert link.record_ref == rref("self_po_9")
        assert "self-supplied" in claim.gap_detail and "self-supplied" in claim.rationale
        assert keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record


def test_a_self_supplied_settlement_neither_supports_nor_overrides_issuer_evidence(
        passport, keystore, now):
    # No issuer settlement anywhere: the holder's own settled record cannot support the claim.
    store, fx = passport("supported", "own.db")
    put_self_supplied(store, {**SELF_SETTLEMENT, "record_id": "self_stl_88001",
                              "event_id": "evt_self_stl_88001", "payment_ref": "PO-5001",
                              "settlement_ref": "STL-88001",
                              "effective_at": "2026-11-03T07:45:00Z"})
    result, _ = investigate(store, fx, keystore, now, lookup=False)
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "self-supplied" in claim.gap_detail
    assert keys(claim.evidence_refs) == set()  # minimum disclosure: an unresolved claim cites no record

    # An issuer-verified pending settlement is not shadowed by a later self-supplied one.
    store, fx = passport("contradiction", "shadow.db")
    put_signed(store, keystore, fx["lookup_events"][0], connector="merchant_lookup")
    put_self_supplied(store, {**SELF_SETTLEMENT, "record_id": "self_stl_x",
                              "event_id": "evt_self_stl_x", "payment_ref": "PO-5001",
                              "settlement_ref": "STL-88001",
                              "occurred_at": "2026-11-04T08:00:00Z",
                              "effective_at": "2026-11-03T07:45:00Z"})
    result, _ = investigate(store, fx, keystore, now, lookup=False)
    claim = claim_of(result)
    assert claim.status == CONTRADICTED
    assert "record:stl_88001@1" in keys(claim.evidence_refs)
    assert "record:self_stl_x@1" not in keys(claim.evidence_refs)
    assert "self_stl_x set aside" in claim.rationale


def test_one_payout_never_supports_two_credits(passport, keystore, now):
    def duplicate(fx):
        twin = copy.deepcopy(fx["bank_txns"][0])
        twin.update(txn_id="txn_9003", booked_at="2026-11-03T11:00:00Z")
        fx["bank_txns"].append(twin)

    fx = variant("supported", duplicate)
    store, _ = passport(fx)
    store.save_request(rfc("rfc_dup_001", ["Please explain the source of these two credits."],
                           txn_ids=["txn_9001", "txn_9003"]))
    result, _ = investigate(store, fx, keystore, now, "rfc_dup_001")
    assert [(l.txn_ref.ref_id, l.link_state, l.record_ref) for l in result.links] == [
        ("txn_9001", LinkState.LINKED, rref("po_5001")),
        ("txn_9003", LinkState.LINKED, rref("po_5001"))]
    claim = claim_of(result)
    assert claim.status == UNRESOLVED
    assert "also matched to credit txn_9003" in claim.gap_detail
    assert "also matched to credit txn_9001" in claim.gap_detail
    assert "settled as" not in claim.statement
    assert result.calls_used == 10 and result.unavailable == []


def test_source_questions_need_a_cue_about_the_money(passport, keystore, now):
    store, fx = passport("supported")
    a, b = store.get_request(RFC_A), store.get_request(RFC_B)
    for text in ("Please provide supporting documents for the £2,160 deposit received on "
                 "3 November (txn_9001).",
                 "Why did £2,160 arrive in your account on 3 November?",
                 "Where did this money come from?",
                 "What was this payment for?"):
        assert classify_question(text, a).category == "source_of_funds", text
    for text in ("Do you hold any credit cards or loans with other lenders?",
                 "Where is your business registered?",
                 "What is your main source of income?",
                 "Explain why you have two accounts.",
                 "Where did the loan come from?"):
        assert classify_question(text, a).category == "unmapped", text
    for text, category in (("What was your turnover in October 2026?", "sales_total"),
                           ("What were your total takings in October?", "sales_total"),
                           ("What returns did you process?", "refunds_total"),
                           ("Have you filed your tax returns?", "unmapped")):
        assert classify_question(text, b).category == category, text

    store.save_request(rfc("rfc_q_001", [
        maya.RFC_A_Q1, "Do you hold any credit cards or loans with other lenders?",
        "Where is your business registered?"], txn_ids=["txn_9001"]))
    result, _ = investigate(store, fx, keystore, now, "rfc_q_001")
    assert claim_of(result, "q1").status == SUPPORTED
    for question_id in ("q2", "q3"):  # never the credit's supported claim
        other = claim_of(result, question_id)
        assert (other.status, other.gap, other.gap_detail, other.evidence_refs) \
            == (UNRESOLVED, "question_not_mapped", UNMAPPED_GAP, [])


@pytest.mark.parametrize("lookup", [True, False], ids=["settled-settlement", "no-settlement"])
def test_a_failed_payout_contradicts_the_claim(passport, keystore, now, lookup):
    fx = variant("supported", statuses(po_5001="cancelled"))
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now, lookup=lookup)
    claim = claim_of(result)
    assert claim.status == CONTRADICTED
    assert "status cancelled" in claim.gap_detail and "record:po_5001@1" in keys(claim.evidence_refs)


def test_a_settlement_reference_already_in_the_passport_names_the_payout(passport, keystore,
                                                                         now):
    def stl_ref(fx):
        fx["bank_txns"][0].update(refs=["STL-88002"], description="THREADLINE STL-88002")

    fx = variant("ambiguous", stl_ref)
    store, _ = passport(fx)
    for settlement in fx["lookup_events"]:
        put_signed(store, keystore, settlement, connector="merchant_lookup")
    result, _ = investigate(store, fx, keystore, now)
    (link,) = result.links
    assert (link.link_state, link.method, link.record_ref) == (LinkState.LINKED, "reference",
                                                               rref("po_5002"))
    assert any("STL-88002" in signal for signal in link.signals)
    claim = claim_of(result)
    assert claim.status == SUPPORTED and "record:stl_88002@1" in keys(claim.evidence_refs)


def test_mixed_case_references_in_the_description_count(passport, keystore, now):
    def mixed(fx):
        fx["bank_txns"][0].update(refs=[], description="Threadline payout ref Po-5001 / Stl-88001")

    fx = variant("supported", mixed)
    store, _ = passport(fx)
    result, _ = investigate(store, fx, keystore, now)
    assert result.links[0].method == "reference"
    assert claim_of(result).status == SUPPORTED


# --------------------------------------------------------------------------- integration layer


@pytest.fixture
def real(tmp_path, keystore, registry, now):
    """The real gateway and ingest path; skipped until alibi/gateway.py and core/ingest.py exist."""
    gateway_module = pytest.importorskip("alibi.gateway")
    ingest = pytest.importorskip("alibi.core.ingest")
    opened = []

    def make(fx):
        store = Store(tmp_path / f"real_{len(opened)}.db")
        opened.append(store)
        for raw in fx["merchant_events"]:
            ingest.ingest_merchant_event(store, registry, maya.sign_event(raw, keystore), now=now)
        ingest.ingest_bank_txns(store, fx["bank_txns"], subject_ref=SUBJ,
                                connection_id=maya.CONNECTION_ID, now=now)
        # A request naming txn_ids is accepted only with the authenticated bank's accounts.
        rfcs = {raw["request_id"]: ingest.receive_rfc(
                    store, registry, maya.sign_request(raw, keystore),
                    authenticated_issuer=raw["issuer"], now=now,
                    account_refs=[maya.ACCOUNT_REF] if raw.get("txn_ids") else None)
                for raw in fx["rfcs"]}
        return store, rfcs

    def run(store, fx, request, *, include_corrections=False, lookup=True, budget=12):
        lookup_fn = (maya.settlement_lookup(fx, keystore, include_corrections=include_corrections)
                     if lookup else None)
        gateway = gateway_module.Gateway(
            store, subject_ref=SUBJ, run_id=next_run_id(store, request.request_id, audience=request.audience),
            registry=registry, now=now, budget=budget, settlement_lookup=lookup_fn,
            request_id=request.request_id)
        return run_investigation(request, store, gateway, now=now), gateway

    yield SimpleNamespace(make=make, run=run, ingest=ingest, gateway=gateway_module)
    for store in opened:
        store.close()


@pytest.mark.parametrize("name", maya.VARIANTS)
def test_integration_rfc_a_matches_the_gold(real, name):
    fx = maya.load_variant(name)
    store, rfcs = real.make(fx)
    result, gateway = real.run(store, fx, rfcs[RFC_A])

    gold_link = fx["expected"]["links"]["txn_9001"]
    (link,) = result.links
    assert (link.link_state.value, link.method) == (gold_link["link_state"], gold_link["method"])
    assert (link.record_ref.ref_id if link.record_ref else None) == gold_link["record_id"]
    assert sorted(ref.ref_id for ref in link.candidates) == sorted(gold_link["candidates"])
    gold = fx["expected"]["claims"][RFC_A]["q1"]
    claim = claim_of(result)
    assert claim.status.value in gold["statuses"]
    assert (keys(claim.evidence_refs) == set()) if claim.status == UNRESOLVED else (set(gold["evidence"]) <= keys(claim.evidence_refs))
    if name == "contradiction":
        assert claim.status != SUPPORTED
    assert result.calls_used == gateway.calls_used
    assert gateway.run_id == result.run_id
    assert [e.trace_id for e in store.list_traces(result.run_id)] \
        == [e.trace_id for e in result.trace]
    rules = [e for e in result.trace if e.actor == "rules"]
    assert rules[-1].step == "status_change" and rules[-1].reason
    assert rules[-1].result["status"] == claim.status.value
    assert store.list_claims(request_id=RFC_A) == result.claims


@pytest.mark.parametrize("connector_has_correction", [True, False])
def test_integration_pending_correction(real, keystore, registry, now, connector_has_correction):
    fx = maya.load_variant("pending_correction")
    store, rfcs = real.make(fx)
    first, _ = real.run(store, fx, rfcs[RFC_A])
    before = claim_of(first)
    assert before.status == SUPPORTED

    (fix,) = fx["corrections"]
    put = real.ingest.ingest_merchant_event(store, registry, maya.sign_event(fix, keystore),
                                            now=now)
    assert put.status == "corrected" and before.claim_id in put.invalidated["claims"]
    second, _ = real.run(store, fx, rfcs[RFC_A], include_corrections=connector_has_correction)
    after = claim_of(second)
    assert after.status in NOT_SUPPORTED and after.state == DerivedState.ACTIVE
    assert "record:stl_88001@2" in keys(after.evidence_refs)


def test_integration_fallback_and_budget(real, keystore, registry, now):
    fx = maya.load_variant("supported")
    store, rfcs = real.make(fx)
    (settlement,) = fx["lookup_events"]
    real.ingest.ingest_merchant_event(store, registry, maya.sign_event(settlement, keystore),
                                      now=now)
    found, _ = real.run(store, fx, rfcs[RFC_A], lookup=False)  # no connector: passport fallback
    assert claim_of(found).status == SUPPORTED
    assert "record:stl_88001@1" in keys(claim_of(found).evidence_refs)
    assert any(r.startswith("unavailable: lookup_settlement") for r in found.unavailable)

    starved, gateway = real.run(store, fx, rfcs[RFC_A], budget=2)
    claim = claim_of(starved)
    assert claim.status == UNRESOLVED and "budget_exceeded" in claim.gap_detail
    assert starved.calls_used == gateway.calls_used == 2
    assert all(r.startswith("budget_exceeded: ") for r in starved.unavailable)


def test_integration_rfc_b_totals(real):
    fx = maya.load_variant("ambiguous")
    store, rfcs = real.make(fx)
    result, _ = real.run(store, fx, rfcs[RFC_B])
    assert [(c.question_id, c.status, c.calculation.total_pence) for c in result.claims] \
        == [("q1", SUPPORTED, 240000), ("q2", SUPPORTED, 18000)]


def test_integration_conflicting_settlement_and_run_ids(real, keystore, registry, now):
    fx = maya.load_variant("supported")
    store, rfcs = real.make(fx)
    # A gateway recording under another run id is refused before any call.
    custom = real.gateway.Gateway(store, subject_ref=SUBJ, run_id="run_custom",
                                  registry=registry, now=now, request_id=RFC_A,
                                  settlement_lookup=maya.settlement_lookup(fx, keystore))
    with pytest.raises(ValueError, match="run id"):
        run_investigation(rfcs[RFC_A], store, custom, now=now)
    assert custom.calls_used == 0 and custom.trace == []

    # A later returned settlement already in the passport counts though the lookup works.
    real.ingest.ingest_merchant_event(store, registry,
                                      maya.sign_event(returned_settlement(fx), keystore), now=now)
    result, gateway = real.run(store, fx, rfcs[RFC_A])
    claim = claim_of(result)
    assert claim.status == CONTRADICTED
    assert "record:stl_88001_ret@1" in keys(claim.evidence_refs)
    assert result.calls_used == gateway.calls_used == 5 and result.unavailable == []


def test_integration_self_supplied_imports_never_support(real, now):
    def refs(fx):
        fx["bank_txns"][0]["refs"] = ["TRANSFER"]

    fx = variant("contradiction", refs)
    store, rfcs = real.make(fx)
    for raw in (SELF_PAYOUT, SELF_SETTLEMENT):
        real.ingest.import_self_supplied(store, dict(raw), subject_ref=SUBJ, now=now)
    result, _ = real.run(store, fx, rfcs[RFC_A])
    (link,) = result.links
    assert link.record_ref == rref("self_po_9")
    claim = claim_of(result)
    assert claim.status == UNRESOLVED and "self-supplied" in claim.gap_detail
    # The credit itself carries TRANSFER, so the gateway may ask the connector about it (NEEDS
    # item 3); the connector knows nothing, and the self-supplied records never support.
    lookups = [e for e in result.trace if e.tool == "lookup_settlement" and e.step == "tool_call"]
    assert lookups and all(e.result == {"found": False} for e in lookups)
    assert result.unavailable == []

def test_run_ids_belong_to_one_banks_request(tmp_path):
    """Another bank using the same request_id gets unrelated run ids (no shared counter)."""
    store = Store(tmp_path / "runs.db")
    try:
        a = next_run_id(store, "rfc_shared", audience="bank_a")
        b = next_run_id(store, "rfc_shared", audience="bank_b")
        assert a == canon.make_id("run", "bank_a", "rfc_shared", "rules", 0)
        assert b == canon.make_id("run", "bank_b", "rfc_shared", "rules", 0)
    finally:
        store.close()
