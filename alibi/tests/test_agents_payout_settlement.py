"""The team's record shape through the agents runner and the one-pass baseline: a payout that
carries its own settlement ``status`` (no separate settlement record) is settlement evidence
for itself.

Every test seeds a fresh in-process world from team case 1 or case 2 through the adapter's
stack (mock merchant, mock bank, feed, RFC intake) and drives the engines with ``FakeClient``
scripts. No network, no Gemini. The S1 two-record fixtures keep their own tests in
``test_agents_runner.py``, ``test_agents_foundation.py`` and ``test_baselines_one_pass.py``.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from alibi.adapter import _load_case, _World
from alibi.agents.candidates import (
    GAP_PENDING_AT_BOOKING,
    GAP_SETTLEMENT_NOT_FOUND,
    own_status_decisive,
    settlement_candidates,
)
from alibi.agents.material import Material
from alibi.agents.runner import run_investigation, run_stats
from alibi.baselines import one_pass
from alibi.connectors.case_source import CaseClock, parse_time
from alibi.core.schemas import ClaimStatus, LinkState, RecordKind, SourceTrust
from alibi.llm.fake import FakeClient
from alibi.tests.test_agents_runner import curator_all

Q_SOURCE, Q_BREAKDOWN = "q.source_of_funds", "q.breakdown"
BOOKED = datetime(2026, 10, 28, 6, 30, tzinfo=timezone.utc)   # txn-000N-004 in both cases


# --------------------------------------------------------------------------- the world


def case_dict(number: int, *, push_payout: bool = False, payout_updates: dict | None = None) -> dict:
    """Team case ``number`` without gold. ``push_payout`` moves the lookup-only payout into the
    merchant pushes (received when it became available); ``payout_updates`` edits its raw
    fields (``status``, ``settled_at``, ``occurred_at``)."""
    case = _load_case(number)
    if payout_updates or push_payout:
        (lookup,) = case["merchant_lookup"]
        event = copy.deepcopy(lookup["event"])
        event.update(payout_updates or {})
        if push_payout:
            case["merchant_lookup"] = []
            case["merchant_deliveries"].append({"received_at": lookup["available_from"], "event": event})
        else:
            case["merchant_lookup"] = [{"available_from": lookup["available_from"], "event": event}]
    return case


class World:
    """One seeded passport at the Bank A RFC's issue time, with the connectors to run on it."""

    def __init__(self, case: dict, root):
        self.inner = _World(case, root)
        rfc_case = min(case["rfcs"], key=lambda r: (r["issued_at"], r["request_id"]))
        self.now = parse_time(rfc_case["issued_at"], "issued_at")
        clock = CaseClock(case, self.now)
        self.inner.seed(clock)
        self.inner.policy(clock)
        self.rfc = self.inner.store.get_request(rfc_case["request_id"], audience=rfc_case["audience"])
        self.store, self.registry = self.inner.store, self.inner.registry

    def kwargs(self) -> dict:
        return dict(registry=self.registry, now=self.now, **self.inner.connectors())

    def agents(self, client, **kw):
        return run_investigation(self.store, self.rfc, client=client, **self.kwargs(), **kw)

    def one_pass(self, client, **kw):
        return one_pass.run_one_pass(self.store, self.rfc, client=client, **self.kwargs(), **kw)

    def credit(self):
        return self.store.get_bank_txn(self.rfc.txn_ids[0])


@pytest.fixture
def make(tmp_path):
    worlds: list[World] = []

    def build(case: int | dict) -> World:
        world = World(case if isinstance(case, dict) else case_dict(case), tmp_path / f"w{len(worlds)}")
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.inner.close()


# --------------------------------------------------------------------------- scripts


def detective(txn_id: str, ref: str):
    claim = {"statement": f"The credit {txn_id} is payout {ref}.", "status": "unresolved",
             "refs": [{"record_id": txn_id, "revision": 1, "role": "context"}],
             "gaps": ["No settlement evidence in the material."],
             "next_checks": [{"type": "lookup_settlement", "args": {"payout_reference": ref},
                              "reason": "Confirm bank settlement."}]}
    return {"alerts": [], "claims": [{"claim_id": "c1", "question_id": Q_SOURCE, **claim},
                                     {"claim_id": "c2", "question_id": Q_BREAKDOWN, **claim}]}


def challenger(status: str, ref: str, *, action=("none", {}), confirming=(), conflicting=(),
               established=False):
    kind, args = action
    return {"claim_id": "c1", "proposed_status": status, "bank_settlement_established": established,
            "payout_reference": ref,
            "confirming_refs": [{"record_id": r, "revision": 1, "why": "matches"} for r in confirming],
            "conflicting_refs": [{"record_id": r, "revision": 1, "why": "conflicts"} for r in conflicting],
            "missing_evidence": [] if status == "supported" else ["settlement"],
            "next_action": {"type": kind, "args": args, "reason": "the one record that decides"},
            "trace": ["Checked the payout."], "rationale": "Decide from the settlement status."}


def lookup_then(ref: str, second: dict):
    """Round 1 asks for the settlement of ``ref``; from round 2 answers ``second``."""
    def script(model, system, user, schema):
        claim = json.loads(user)["claim"]
        if claim.get("lookup_results"):
            return second
        return challenger("unresolved", ref, action=("lookup_settlement", {"payout_reference": ref}),
                          confirming=[claim["refs"][0]["record_id"]])
    return script


def client_for(number: int, second: dict | None = None) -> FakeClient:
    txn, ref, payout = f"txn-000{number}-004", f"PO-000{number}-01", f"rec-000{number}-021"
    if second is None:
        second = challenger("supported", ref, confirming=(txn, payout), established=True)
    return FakeClient({"You are the Detective": detective(txn, ref),
                       "You are the Challenger": lookup_then(ref, second),
                       "You are the Curator": curator_all()})


def one_pass_answer(number: int, status: str = "supported") -> dict:
    """A one-pass proposal for the source question only: the baseline classifies questions by
    keyword, so the team's ``q.breakdown`` text is unmapped there (a pre-existing gap outside
    this test file's subject)."""
    txn, payout = f"txn-000{number}-004", f"rec-000{number}-021"
    refs = [{"record_id": txn, "revision": 1, "role": "confirms"},
            {"record_id": payout, "revision": 1, "role": "confirms"}]
    return {"claims": [{"question_id": Q_SOURCE, "statement": "The answer.", "status": status,
                        "refs": refs, "gaps": []}],
            "disclosures": [], "rationale": "From the payout status."}


def keys(claim) -> set[str]:
    return {r.key() for r in claim.evidence_refs}


def text(claim) -> str:
    return " ".join(filter(None, [claim.statement, claim.rationale, claim.gap]))


def by_question(result) -> dict:
    return {c.question_id: c for c in result.claims}


# --------------------------------------------------------------------------- agents runner


def test_case_1_agents_support_both_bank_a_questions_from_the_looked_up_payout(make):
    world = make(1)
    client = client_for(1)
    result = world.agents(client)
    claims = by_question(result)
    assert set(claims) == {Q_SOURCE, Q_BREAKDOWN}
    for claim in claims.values():
        assert claim.status == ClaimStatus.SUPPORTED and claim.gap is None
        assert "record:rec-0001-021@1" in keys(claim) and "txn:txn-0001-004@1" in keys(claim)
        assert claim.calculation is not None and claim.calculation.balanced is True
        assert claim.calculation.timing is not None and claim.calculation.timing.ok
        assert claim.calculation.timing.settlement_ref.ref_id == "rec-0001-021"
        assert "does not show bank settlement" not in text(claim)
        assert "settlement not retrieved" not in text(claim)
        assert "settlement rec-0001-021" not in text(claim), "a payout is never called a settlement record"
        assert "own status settled" in claim.statement
    (link,) = result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "rec-0001-021"
    stats = run_stats(result)
    assert stats["lookups"] >= 1 and stats["action_budget_used"] == 1
    # One question's rounds; the second question reuses the credit's outcome. Model call
    # counting is unchanged: detective, two challenger rounds, curator.
    assert stats["rounds"] == 2 and stats["model_calls"] == 4 and stats["tokens"] == 80
    assert stats["unsafe_proposals"] == 0
    lookups = [e for e in result.trace if e.actor == "challenger" and e.tool == "lookup_settlement"]
    assert lookups and lookups[0].outcome == "ok" and "rec-0001-021@1" in lookups[0].result["new_keys"]
    # The payout reached the Challenger's second round in both role lists, tagged with its lookup.
    second = [c for c in client.calls if c.system.startswith("# Challenger")][1]
    sent = json.loads(second.user)
    assert [s["record_id"] for s in sent["settlements"]] == ["rec-0001-021"]
    assert sent["settlements"][0]["via_lookup"] == 0 and sent["settlements"][0]["status"] == "settled"
    assert "rec-0001-021" in {r["record_id"] for r in sent["merchant"]}
    assert sent["claim"]["lookup_results"][0]["returned"] == [
        {"record_id": "rec-0001-021", "revision": 1, "kind": "payout", "status": "settled"}]


def test_case_2_agents_pending_payout_is_never_supported_and_names_the_pending_gap(make):
    world = make(2)
    # The Challenger tries to contradict from the pending payout; code decides from fields.
    second = challenger("contradicted", "PO-0002-01", conflicting=("rec-0002-021",))
    result = world.agents(client_for(2, second))
    claims = by_question(result)
    for claim in claims.values():
        assert claim.status in (ClaimStatus.UNRESOLVED, ClaimStatus.CONTRADICTED)
        assert claim.gap is not None and claim.gap.startswith(f"{GAP_PENDING_AT_BOOKING}:")
        if claim.status == ClaimStatus.UNRESOLVED:
            assert keys(claim) == set()   # minimum disclosure: an unresolved claim cites no record
        else:
            assert "record:rec-0002-021@1" in keys(claim)
        assert "pending" in claim.gap and "settlement not retrieved" not in text(claim)
    # The reference came from the issuer's message text, so the lookup was allowed and ran.
    stats = run_stats(result)
    assert stats["lookups"] == 1 and stats["action_budget_used"] == 1
    refused = [e for e in result.trace if e.step == "validation" and e.outcome == "refused"
               and "payout_reference" in (e.reason or "")]
    assert refused == []
    (link,) = result.links
    assert link.link_state == LinkState.UNMATCHED and link.record_ref is None
    # The pending status was observed before the booking, so it is no explicit conflict: the
    # Challenger's contradicted proposal is not applied and the code verdict is unresolved.
    assert claims[Q_SOURCE].status == ClaimStatus.UNRESOLVED
    assert any("without an explicit-field conflict" in (e.reason or "") for e in result.trace)


def test_case_2_agents_pending_observed_after_booking_is_a_contradiction(make):
    # The team's status_as_of (2026-10-29T06:30Z, after the booking) as the observation time.
    world = make(case_dict(2, payout_updates={"occurred_at": "2026-10-29T06:30:00Z"}))
    second = challenger("contradicted", "PO-0002-01", conflicting=("rec-0002-021",))
    result = world.agents(client_for(2, second))
    claims = by_question(result)
    for claim in claims.values():
        assert claim.status == ClaimStatus.CONTRADICTED
        assert claim.gap.startswith(f"{GAP_PENDING_AT_BOOKING}:")
        assert "at or after the booking" in claim.gap
        assert "record:rec-0002-021@1" in keys(claim)
    assert run_stats(result)["lookups"] == 1


def test_payout_without_status_stays_unresolved_in_agents_mode(make):
    world = make(case_dict(1, push_payout=True, payout_updates={"status": None, "settled_at": None}))
    payout = world.store.get_source("rec-0001-021")
    assert payout.kind == RecordKind.PAYOUT and payout.status is None and payout.effective_at is None
    result = world.agents(client_for(1))
    claims = by_question(result)
    for claim in claims.values():
        assert claim.status == ClaimStatus.UNRESOLVED
        assert claim.gap.startswith((f"{GAP_SETTLEMENT_NOT_FOUND}:", "balance_release_not_settlement:"))
        assert "no status" in claim.gap
        assert keys(claim) == set()  # minimum disclosure: an unresolved claim cites no record
    (link,) = result.links
    assert link.link_state == LinkState.UNMATCHED and [r.ref_id for r in link.candidates] == ["rec-0001-021"]
    # The gateway refuses a payout with no settlement status as a lookup result.
    gateway = [e for e in result.trace if e.actor == "gateway" and e.tool == "lookup_settlement"]
    assert gateway and gateway[0].outcome != "ok"


def test_pushed_settled_payout_supports_without_any_lookup_in_agents_mode(make):
    world = make(case_dict(1, push_payout=True))
    second = challenger("supported", "PO-0001-01", confirming=("txn-0001-004", "rec-0001-021"), established=True)
    client = FakeClient({"You are the Detective": detective("txn-0001-004", "PO-0001-01"),
                         "You are the Challenger": second, "You are the Curator": curator_all()})
    result = world.agents(client)
    claims = by_question(result)
    assert {c.status for c in claims.values()} == {ClaimStatus.SUPPORTED}
    assert run_stats(result)["lookups"] == 0 and run_stats(result)["rounds"] == 1
    (link,) = result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "rec-0001-021"
    # The Detective saw the payout in both role lists before any lookup.
    sent = json.loads([c for c in client.calls if c.system.startswith("# Detective")][0].user)
    assert [s["record_id"] for s in sent["settlements"]] == ["rec-0001-021"]
    assert sent["settlements"][0]["via_lookup"] is None
    assert "rec-0001-021" in {r["record_id"] for r in sent["merchant"]}


# --------------------------------------------------------------------------- one-pass baseline


def test_one_pass_case_1_pushed_payout_is_looked_up_from_the_message_and_supports(make):
    world = make(case_dict(1, push_payout=True))
    client = FakeClient({"*": one_pass_answer(1)})
    out = world.one_pass(client)
    claim = by_question(out.result)[Q_SOURCE]
    assert claim.status == ClaimStatus.SUPPORTED and claim.gap is None
    assert "record:rec-0001-021@1" in keys(claim)
    assert claim.calculation is not None and claim.calculation.timing.ok
    assert claim.calculation.timing.settlement_ref.ref_id == "rec-0001-021"
    assert claim.statement == "The answer.", "a clean model statement with the right status is kept"
    assert "settlement rec-0001-021" not in text(claim)
    assert "by its own status" in claim.rationale and "Timing of payout record:rec-0001-021@1" in claim.rationale
    # The fixed retrieval: the balance-release message names PO-0001-01, so exactly one lookup
    # ran, and it returned the payout itself, which the model saw as settlement evidence.
    assert out.lookups == 1 and out.model_calls == 1 and out.unsafe_proposals == 0
    sent = json.loads(client.calls[0].user)
    assert [s["record_id"] for s in sent["settlements"]] == ["rec-0001-021"]
    assert sent["settlements"][0]["status"] == "settled"
    (link,) = out.result.links
    assert link.link_state == LinkState.LINKED and link.record_ref.ref_id == "rec-0001-021"


def test_one_pass_case_2_pushed_pending_payout_is_unresolved_with_the_pending_gap(make):
    world = make(case_dict(2, push_payout=True))
    out = world.one_pass(FakeClient({"*": one_pass_answer(2)}))
    claim = by_question(out.result)[Q_SOURCE]
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith(f"{GAP_PENDING_AT_BOOKING}:") and "pending" in claim.gap
    assert keys(claim) == set()  # minimum disclosure: an unresolved claim cites no record
    assert "settlement not retrieved" not in text(claim)
    assert out.lookups == 1
    assert out.unsafe_proposals >= 1   # supported was proposed, the code verdict is not supported
    note = next(e for e in reversed(out.result.trace) if e.result.get("proposed_statuses") is not None)
    assert note.result["proposed_statuses"] == {Q_SOURCE: "supported"}


def test_one_pass_case_2_lookup_only_pending_payout_is_examined_but_not_disclosed(make):
    # The issuer's message names the lookup-only payout. Fixed retrieval examines it once;
    # its pending state still cannot support the source or disclose unresolved evidence.
    world = make(2)
    out = world.one_pass(FakeClient({"*": one_pass_answer(2)}))
    claim = by_question(out.result)[Q_SOURCE]
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.evidence_refs == [] and claim.calculation is None
    assert "rec-0002-021" in {r.ref_id for r in claim.examined_refs}
    assert out.lookups == 1


def test_one_pass_pending_payout_not_looked_up_stays_provisional_and_honest(make):
    # A pushed pending payout whose reference no message names: nothing is looked up, the
    # payout is provisional, and the gap names its status rather than calling it absent.
    case = case_dict(2, push_payout=True)
    for delivery in case["merchant_deliveries"]:
        if delivery["event"]["kind"] == "balance_release":
            delivery["event"]["message"] = "Good news! Your sales are now available in your seller balance."
    world = make(case)
    out = world.one_pass(FakeClient({"*": one_pass_answer(2, "unresolved")}))
    claim = by_question(out.result)[Q_SOURCE]
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap.startswith(f"{GAP_SETTLEMENT_NOT_FOUND}:")
    assert "status pending" in claim.gap and "initial state" in claim.gap
    assert out.lookups == 0
    (link,) = out.result.links
    assert [r.ref_id for r in link.candidates] == ["rec-0002-021"]


# --------------------------------------------------------------------------- the rule itself


def test_own_status_decisive_follows_the_pinned_rule(make):
    world = make(case_dict(1, push_payout=True))
    settled = world.store.get_source("rec-0001-021")
    assert own_status_decisive(settled, BOOKED)
    assert own_status_decisive(settled.model_copy(update={"status": "failed", "effective_at": None}), BOOKED)
    pending = settled.model_copy(update={"status": "pending", "effective_at": None})
    assert not own_status_decisive(pending, BOOKED), "pending before the booking is the initial state"
    assert own_status_decisive(pending, BOOKED, looked_up={"PO-0001-01"})
    assert own_status_decisive(pending.model_copy(update={"occurred_at": BOOKED}), BOOKED)
    assert own_status_decisive(pending.model_copy(update={"occurred_at": BOOKED + timedelta(hours=1)}), BOOKED)
    no_status = settled.model_copy(update={"status": None, "effective_at": None})
    assert not own_status_decisive(no_status, BOOKED, looked_up={"PO-0001-01"})
    assert not own_status_decisive(settled.model_copy(update={"kind": RecordKind.SETTLEMENT}), BOOKED)


def test_candidate_rule_on_the_team_payout_shapes(make):
    world = make(case_dict(1, push_payout=True))
    credit = world.credit()
    settled = world.store.get_source("rec-0001-021")
    result = settlement_candidates(credit, [settled])
    assert result.state == LinkState.LINKED
    chosen = result.chosen
    assert chosen.settles_itself and chosen.settlement is settled
    assert chosen.settled_at == "2026-10-27T16:00:00Z" and chosen.reference_matched is True
    assert "by its own status" in chosen.summary()

    pending = settled.model_copy(update={"status": "pending", "effective_at": None})
    provisional = settlement_candidates(credit, [pending])
    assert provisional.state == LinkState.UNMATCHED and [c.tier for c in provisional.pool] == ["provisional"]
    assert provisional.coded == [] and provisional.rejected == []

    looked_up = settlement_candidates(credit, [pending], looked_up={"PO-0001-01"})
    assert looked_up.pool == [] and looked_up.conflicts == []
    (rejection,) = looked_up.coded
    assert rejection.code == GAP_PENDING_AT_BOOKING and rejection.settles_itself
    assert rejection.reason.startswith("payout rec-0001-021@1 was pending when observed at 2026-10-26T09:00:00Z")
    assert "does not show the funds reached the bank" in rejection.reason

    late = pending.model_copy(update={"occurred_at": BOOKED})
    conflict = settlement_candidates(credit, [late])
    (rejection,) = conflict.conflicts
    assert rejection.code == GAP_PENDING_AT_BOOKING and "at or after the booking" in rejection.reason

    after = settled.model_copy(update={"effective_at": BOOKED + timedelta(minutes=1)})
    (rejection,) = settlement_candidates(credit, [after]).conflicts
    assert rejection.code == "settlement_after_credit" and rejection.reason.startswith("payout rec-0001-021@1")

    failed = settled.model_copy(update={"status": "failed", "effective_at": None})
    (rejection,) = settlement_candidates(credit, [failed]).conflicts
    assert rejection.code == "settlement_failed" and rejection.reason == "payout status is failed"

    no_status = settled.model_copy(update={"status": None, "effective_at": None})
    plain = settlement_candidates(credit, [no_status], looked_up={"PO-0001-01"})
    assert [c.tier for c in plain.pool] == ["provisional"] and plain.rejected == []


def test_material_knows_text_references_and_settlement_lookup_refs(make):
    world = make(2)
    material = Material(world.rfc.subject_ref)
    for record in world.store.list_sources(world.rfc.subject_ref):
        material.add_record(record)
    material.add_txn(world.credit())
    # The issuer's balance-release message names the withdrawal reference as a whole token.
    assert material.knows_reference("PO-0002-01") and material.knows_reference("po-0002-01")
    assert not material.knows_reference("PO-0002-0") and not material.knows_reference("O-0002-01")
    assert not material.knows_reference("PO-0002-99") and not material.knows_reference("")
    # A self-supplied record's text opens no lookup, as at the gateway.
    message = next(r for r in material.records() if r.kind == RecordKind.MESSAGE)
    own = Material(world.rfc.subject_ref)
    own.add_record(message.model_copy(update={"source_trust": SourceTrust.SELF_SUPPLIED,
                                              "issuer_signature": None, "signed_body": None}))
    assert not own.knows_reference("PO-0002-01")
    # settlement_lookup_refs: the reference asked for and the payment_ref of what came back.
    assert material.settlement_lookup_refs() == frozenset()
    index = material.add_lookup("lookup_settlement", {"payout_reference": "po-0002-01"}, "not_found")
    assert material.settlement_lookup_refs() == frozenset()
    payout = world.store.get_source("rec-0002-020").model_copy(update={
        "record_id": "rec-0002-021", "kind": RecordKind.PAYOUT, "status": "pending", "payment_ref": "PO-0002-01",
        "text": None})
    index = material.add_lookup("lookup_settlement", {"payout_reference": "po-0002-01"}, "ok",
                                refs=["rec-0002-021@1"])
    material.add_record(payout, via_lookup=index)
    assert material.settlement_lookup_refs() == frozenset({"PO-0002-01"})
    assert material.via_lookup("rec-0002-021", 1) == index
    assert [r.record_id for r in material.settlement_evidence()] == ["rec-0002-021"]
