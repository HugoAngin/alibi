"""Execute an agent's proposed retrieval through the scoped gateway, within a per-run budget.

The Challenger (and the Detective's ``next_checks``) may propose one of:

* ``lookup_settlement {payout_reference}``: the merchant connector's settlement evidence for a
  payout reference one retrieved record carries (``Gateway.lookup_settlement``): a
  ``settlement`` record, or a ``payout`` record carrying a ``status`` (the team's payouts fold
  the settlement state in). Either is stored in the ``Material`` tagged ``via_lookup``, and
  ``Material.settlement_lookup_refs`` then names the reference, so the candidate rule treats a
  payout returned for its own reference as the merchant's answer about its settlement;
* ``lookup_payouts_covering {record_id}``: payouts and settlements whose ``covers_record_ids``
  name a retrieved record (a passport listing filtered in code);
* ``lookup_by_date {payout_date}``: payouts and settlements initiated or effective on a
  ``YYYY-MM-DD`` day (a passport listing filtered in code);
* ``refresh_txn {txn_id}``: the bank's current revision of a retrieved transaction
  (``Gateway.refresh_txn``);
* ``none``.

Every executed action costs one unit of ``ActionBudget`` (default 3 per run) on top of the
gateway's own call budget. Results are added to the ``Material`` with ``via_lookup`` set to the
lookup's index, so a source retrieved by an agent's choice is traceable to that choice. Errors
never propagate: a gateway refusal or failure becomes an ``ActionResult`` with the typed
outcome, which the runner feeds back to the agent and logs.

The two passport-side lookups (``covering``, ``by_date``) stand in for the team merchant
lookup keys ``covers_record_id`` and ``payout_date``; wiring them to the mock merchant is in
``alibi/NEEDS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from alibi.agents.material import Material
from alibi.agents.models import ActionArgs, NextAction, NextCheck
from alibi.core.schemas import BankTxn, EvidenceRecord, RecordKind
from alibi.gateway import GatewayError

ACTION_BUDGET = 3
LOOKUP_KINDS = (RecordKind.PAYOUT, RecordKind.SETTLEMENT)


class ActionBudget:
    """Counts executed retrievals for one run; ``spend`` returns False when exhausted."""

    def __init__(self, cap: int = ACTION_BUDGET):
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError("action budget must be a non-negative integer")
        self.cap = cap
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    def spend(self) -> bool:
        if self.used >= self.cap:
            return False
        self.used += 1
        return True


@dataclass
class ActionResult:
    tool: str
    args: dict[str, Any]
    outcome: str                    # ok | not_found | refused | unavailable | budget_exceeded | error | skipped
    reason: str | None = None
    records: list[EvidenceRecord] = field(default_factory=list)
    txns: list[BankTxn] = field(default_factory=list)
    lookup_index: int | None = None
    new_keys: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.records or self.txns)

    def feedback(self) -> dict[str, Any]:
        """What the agent is told about the result: ids, revisions, kinds and statuses only."""
        items = [{"record_id": r.record_id, "revision": r.revision, "kind": r.kind.value,
                  "status": r.status} for r in self.records]
        items += [{"record_id": t.txn_id, "revision": t.revision, "kind": "bank_txn"} for t in self.txns]
        return {"tool": self.tool, "args": self.args, "outcome": self.outcome, "reason": self.reason,
                "returned": items, "lookup_index": self.lookup_index}


def _args(action: NextAction | NextCheck) -> dict[str, Any]:
    return {k: v for k, v in action.args.model_dump().items() if v is not None}


def _typed(exc: GatewayError) -> str:
    outcome = getattr(exc, "outcome", "error")
    return outcome if outcome in ("refused", "unavailable", "budget_exceeded", "not_found", "error") else "error"


def execute_action(gateway: Any, material: Material, action: NextAction | NextCheck,
                   budget: ActionBudget) -> ActionResult:
    """Run one proposed action and fold what it returns into ``material``."""
    kind = action.type
    args = _args(action)
    if kind == "none":
        return ActionResult("none", args, "skipped", "no retrieval requested")
    if kind == "merchant_lookup":
        return ActionResult(kind, args, "refused", "merchant_lookup is not available through the gateway")
    if not budget.spend():
        index = material.add_lookup(kind, args, "budget_exceeded", reason="the run's action budget is spent",
                                    actor="challenger")
        return ActionResult(kind, args, "budget_exceeded", f"the action budget of {budget.cap} is spent",
                            lookup_index=index)

    records: list[EvidenceRecord] = []
    txns: list[BankTxn] = []
    outcome, reason = "ok", None
    try:
        if kind == "lookup_settlement":
            ref = args.get("payout_reference")
            if not isinstance(ref, str) or not material.knows_reference(ref):
                outcome, reason = "refused", "payout_reference is not carried by any retrieved record"
            else:
                found = gateway.lookup_settlement(ref)
                if found is None:
                    outcome, reason = "not_found", f"the merchant knows no settlement for {ref}"
                else:
                    records = [found]
        elif kind == "lookup_payouts_covering":
            record_id = args.get("record_id")
            if not isinstance(record_id, str) or not material.has(record_id):
                outcome, reason = "refused", "record_id is not in the retrieved material"
            else:
                listed = gateway.list_records(kinds=list(LOOKUP_KINDS))
                records = [r for r in listed if record_id in r.related_record_ids]
                if not records:
                    outcome, reason = "not_found", f"no payout or settlement covers {record_id}"
        elif kind == "lookup_by_date":
            day = _parse_day(args.get("payout_date"))
            if day is None:
                outcome, reason = "refused", "payout_date is not YYYY-MM-DD"
            else:
                listed = gateway.list_records(kinds=list(LOOKUP_KINDS))
                records = [r for r in listed if r.occurred_at.date() == day
                           or (r.effective_at is not None and r.effective_at.date() == day)]
                if not records:
                    outcome, reason = "not_found", f"no payout or settlement on {day.isoformat()}"
        elif kind == "refresh_txn":
            txn_id = args.get("txn_id")
            if not isinstance(txn_id, str) or not material.has(txn_id):
                outcome, reason = "refused", "txn_id is not in the retrieved material"
            else:
                txns = [gateway.refresh_txn(txn_id)]
        else:
            outcome, reason = "refused", f"unknown action {kind!r}"
    except GatewayError as exc:
        outcome, reason = _typed(exc), str(exc)
    except Exception as exc:  # noqa: BLE001 - report the type, never the payload
        outcome, reason = "error", f"unexpected error ({type(exc).__name__})"

    keys = [f"{r.record_id}@{r.revision}" for r in records] + [f"{t.txn_id}@{t.revision}" for t in txns]
    index = material.add_lookup(kind, args, outcome, refs=keys, reason=reason, actor="challenger")
    new_keys = []
    for record in records:
        if material.add_record(record, via_lookup=index):
            new_keys.append(f"{record.record_id}@{record.revision}")
    for txn in txns:
        if material.add_txn(txn, via_lookup=index):
            new_keys.append(f"{txn.txn_id}@{txn.revision}")
    return ActionResult(kind, args, outcome, reason, records, txns, index, new_keys)


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


__all__ = ["ACTION_BUDGET", "ActionBudget", "ActionResult", "ActionArgs", "execute_action"]
