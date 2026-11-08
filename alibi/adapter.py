"""Adapter: run one team case through the real connector paths and emit predictions.

    from alibi.adapter import run_case
    predictions = run_case(1, "rules")                 # list of prediction dicts, one per RFC
    uv run --frozen python -m alibi.adapter --mode rules --cases 1-80 --out data/alibi/predictions

``run_case(case, mode, as_of=None)`` takes a case number (loaded through ``agents.data.api``) or a
case dict (gold is stripped either way) and a mode, ``rules`` (the core engine), ``one_pass``
(the single-call Gemini baseline, ``alibi.baselines.one_pass``) or ``agents`` (the Gemini roles
pipeline, ``alibi.agents.runner``). It returns one prediction per RFC in the shape of
``alibi/eval/predictions.md`` (``alibi/eval/schemas/prediction.schema.json``), each validated with
``alibi.eval.jsonschema_lite`` before it is returned.

Nothing is fed to the passport directly. For every case a fresh passport store is seeded through
the simulated services in process: the mock merchant (``alibi.connectors.merchant``) pushes signed
webhooks into ``plug.webhook_sink``, the mock bank (``alibi.bank.service``) serves the feed and
issues the signed RFCs into ``plug.rfc_sink``, ``connectors.clients.bootstrap`` drives both, the
holder's imports go through the self-supplied import, and the investigation reaches lookup-only
merchant records only through ``MerchantClient.settlement_lookup`` behind the gateway. The clock
is the case's: each RFC is answered at its own ``issued_at``; when the RFCs are issued at different
times the services' clocks are advanced (``POST /admin/advance``) to the later moment, the newly
visible pushes, feed rows, imports and RFCs land, and only then is the later RFC answered. The
first RFC is always answered before the clock moves.

Every file the adapter writes lives under ``data/alibi/`` (``ALIBI_DATA_DIR`` moves it): the
per-case stores under ``adapter/<mode>/case_<n>/`` and the predictions under the ``--out`` folder.
The Gemini client for the model modes is ``GeminiClient.for_run(<mode>, <run id>)`` scoped to the
case and wrapped by ``alibi.agents.replay.make_client`` (``ALIBI_REPLAY=1`` serves recordings, a
live run records). A model outage, budget refusal or schema failure on a request becomes a typed
``failure`` object on that prediction, never an exception out of ``run_case``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.data import api as data_api
from alibi import api
from alibi.bank.service import create_bank_app
from alibi.connectors import keys
from alibi.connectors.bank_feed import FeedClient, HttpFeed
from alibi.connectors.case_source import CaseClock, parse_time
from alibi.connectors.clients import BankClient, MerchantClient, bootstrap, connect
from alibi.connectors.merchant import create_merchant_app
from alibi.connectors.plug import rfc_sink, webhook_sink
from alibi.connectors.self_supplied import import_case_imports
from alibi.core.ingest import IngestError
from alibi.core.schemas import (
    ClaimStatus,
    EvidenceRef,
    EvidenceRecord,
    LinkState,
    RecordKind,
    SourceTrust,
    format_utc,
)
from alibi.core.store import Store, StoreError
from alibi.eval import jsonschema_lite
from alibi.eval.common import GAP_CODES, Q_SOURCE, load_schema
from alibi.llm import config as llm_config

__all__ = ["MODES", "STRATEGIES", "run_case", "run_cases", "system_label", "main"]

MODES = ("rules", "one_pass", "agents")
MODE_ALIASES = {"single_call": "one_pass", "alibi": "agents"}
RECIPIENTS = ("bank_a", "bank_b")
ADMIN = "admin"
FEED = "feed"
SELF_PREFIX = "self_"
REASON_NOT_NEEDED = "not needed to answer the questions"
_LLM_FAILURES = {"unavailable": "unavailable", "budget_exceeded": "budget_exceeded",
                 "schema": "invalid_output", "error": "error"}

#: The predeclared retrieval strategy per mode (``retrieval.strategy`` on every prediction).
STRATEGIES = {
    "rules": ("deterministic: list the subject's records and bank entries; when no stored payout "
              "settles the credit, look up the settlement for the payout reference the credit "
              "carries or the subject's own records name; one call per reference"),
    "one_pass": "look up any payout reference named in a balance_release message",
    "agents": ("adaptive: the Detective proposes checks and the Challenger requests at most one "
               "lookup per round (settlement by reference, payouts by covered record or date), "
               "within an action budget of 3 per question and 3 rounds"),
}


# --------------------------------------------------------------------------- small helpers


def normalise_mode(mode: str) -> str:
    """``rules``, ``one_pass`` or ``agents``; ``single_call`` and ``alibi`` are aliases."""
    if not isinstance(mode, str):
        raise ValueError("mode must be a string")
    name = MODE_ALIASES.get(mode, mode)
    if name not in MODES:
        raise ValueError(f"unknown mode {mode!r}; modes are {', '.join(MODES)}")
    return name


def _clip(text: Any, limit: int = 300) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10, cwd=str(llm_config.REPO_ROOT))
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and sha else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def system_label(mode: str) -> dict[str, str]:
    """The first JSONL line's ``system`` object: name, model and commit."""
    mode = normalise_mode(mode)
    model = {"rules": "none", "one_pass": llm_config.FLASH,
             "agents": f"{llm_config.FLASH}+{llm_config.FRONTIER}"}[mode]
    return {"name": mode, "model": model, "commit": _commit()}


def _replay_on() -> bool:
    return str(os.environ.get("ALIBI_REPLAY", "")).strip().lower() in ("1", "true", "yes")


def _load_case(case: int | dict[str, Any]) -> dict[str, Any]:
    if isinstance(case, bool) or not isinstance(case, (int, dict)):
        raise ValueError("case must be a case number or a case dict")
    loaded = data_api.load_case(case) if isinstance(case, int) else case
    stripped = data_api.without_gold(loaded)
    if "case_number" not in stripped or "subject_ref" not in stripped:
        raise ValueError("a case needs case_number and subject_ref")
    return stripped


def _gap_code(text: str | None) -> str | None:
    """The pinned code at the head of ``Claim.gap`` (the engines write ``<code> <sentence>``)."""
    if not text:
        return None
    return text.split()[0].rstrip(".,;:")


# --------------------------------------------------------------------------- the simulated world


class _World:
    """One fresh passport store and the two mock services wired to it in process."""

    def __init__(self, case: dict[str, Any], root: Path, *,
                 trusted_issuers: Mapping[str, str] | None = None,
                 synthetic_key_seed: str | None = None):
        if synthetic_key_seed is not None and (not isinstance(synthetic_key_seed, str)
                                                or not synthetic_key_seed):
            raise ValueError("synthetic_key_seed must be a nonempty trusted setup secret")
        self.case = case
        self.number = int(case["case_number"])
        self.subject = case["subject_ref"]
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.keystore = keys.connector_keystore(root / "keys", trusted_issuers=trusted_issuers,
                                                seed=synthetic_key_seed)
        self.registry = self.keystore.registry()
        self.store = Store(root / "passport.db")
        # Fresh bearer tokens per world; they never leave the process and are never printed.
        self.tokens = {name: secrets.token_urlsafe(24) for name in (*RECIPIENTS, FEED, ADMIN)}
        admin = self.tokens[ADMIN]
        number, snapshot = self.number, copy.deepcopy(case)

        def loader(n: int) -> dict[str, Any]:
            if n != number:
                raise KeyError(f"case {n} is not the adapter's case")
            return copy.deepcopy(snapshot)

        merchant_app = create_merchant_app(
            webhook_sink(self.store, self.registry, subject_ref=self.subject),
            keystore=self.keystore, admin_token=admin, case_loader=loader)
        bank_app = create_bank_app(keystore=self.keystore, registry=self.registry,
                                   tokens=self.tokens, admin_token=admin,
                                   rfc_sink=rfc_sink(self.store, self.registry), case_loader=loader)
        from fastapi.testclient import TestClient
        self.merchant_client = TestClient(merchant_app)
        self.bank_client = TestClient(bank_app)
        self.merchant = MerchantClient(self.merchant_client, admin_token=admin)
        self.banks = {name: BankClient(self.bank_client, self.tokens[name], recipient=name,
                                       admin_token=admin) for name in RECIPIENTS}
        self.feed = FeedClient(HttpFeed("", self.tokens[FEED], client=self.bank_client), self.store,
                               subject_ref=self.subject, connection_id=f"conn-case-{self.number}")
        self.issued: set[str] = set()
        self.notes: list[str] = []

    def close(self) -> None:
        for client in (self.merchant_client, self.bank_client):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self.store.close()

    def connectors(self) -> dict[str, Any]:
        return connect(self.merchant, self.feed)

    # ----------------------------------------------------------------- seeding and the clock

    def seed(self, clock: CaseClock) -> dict[str, Any]:
        summary = bootstrap(self.store, self.registry, case=self.number, merchant=self.merchant,
                            banks=self.banks, feed=self.feed, clock=clock)
        self.issued.update(summary.get("rfcs") or {})
        return summary

    def advance(self, clock: CaseClock, moment: datetime) -> None:
        """Move the services and the local clock forward to ``moment`` and land what became
        visible: merchant pushes, feed rows, imports and RFCs, through the same paths."""
        previous = clock.as_of
        clock.advance(moment)
        self.merchant.advance(moment)              # pushes the newly visible deliveries
        self.banks[RECIPIENTS[0]].advance(moment)  # one bank service behind both recipients
        self.feed.sync(now=moment)
        for put in import_case_imports(self.store, clock, since=previous):
            self.notes.append(f"import {put.record.record_id}: {put.status}")
        self.issue_visible(clock)

    def issue_visible(self, clock: CaseClock) -> None:
        for rfc in clock.visible_rfcs():
            request_id = rfc["request_id"]
            if request_id in self.issued:
                continue
            self.issued.add(request_id)
            bank = self.banks.get(rfc["audience"])
            if bank is None:
                self.notes.append(f"{request_id}: no client for {rfc['audience']}")
                continue
            issued = bank.issue_rfc(request_id)
            signed, account_refs = issued.get("rfc"), issued.get("account_refs")
            try:
                api.receive_rfc(self.store, signed, authenticated_issuer=rfc["audience"],
                                registry=self.registry, account_refs=account_refs, now=clock.as_of)
            except IngestError as exc:
                self.notes.append(f"{request_id}: refused: {_clip(exc.reason)}")

    def policy(self, clock: CaseClock) -> None:
        """Record the case's holder policy when it carries one (``holder_policy`` or
        ``policy``: a list of rules or rule texts); otherwise the subject has no policy."""
        rules = self.case.get("holder_policy", self.case.get("policy"))
        if not rules:
            return
        try:
            api.record_policy(self.store, self.subject, rules, confirmed=True, now=clock.as_of)
        except Exception as exc:  # noqa: BLE001 - a policy the core cannot express is noted
            self.notes.append(f"holder policy not recorded: {type(exc).__name__}")

    # ----------------------------------------------------------------- refs

    def gold_ref(self, ref: EvidenceRef) -> str:
        """``bank:<txn_id>``, ``merchant:<record_id>`` or ``import:<record_id>`` (the holder's
        original id, without the ``self_`` namespace)."""
        if ref.ref_type == "txn":
            return f"bank:{ref.ref_id}"
        record_id = ref.ref_id
        trust = None
        try:
            trust = self.store.get_source(record_id, ref.revision).source_trust
        except StoreError:
            try:
                trust = self.store.get_source(record_id).source_trust
            except StoreError:
                trust = None
        if trust == SourceTrust.SELF_SUPPLIED or record_id.startswith(SELF_PREFIX):
            if record_id.startswith(SELF_PREFIX):
                record_id = record_id[len(SELF_PREFIX):]
            return f"import:{record_id}"
        return f"merchant:{record_id}"

    def gold_refs(self, refs: Iterable[EvidenceRef]) -> list[str]:
        out: list[str] = []
        for ref in refs:
            token = self.gold_ref(ref)
            if token not in out:
                out.append(token)
        return out

    def all_refs(self) -> list[tuple[str, EvidenceRecord | Any]]:
        """Every active record and bank entry of the subject, as gold refs."""
        out = []
        for record in self.store.list_sources(self.subject):
            out.append((self.gold_ref(record.ref()), record))
        for txn in self.store.list_bank_txns(self.subject):
            out.append((self.gold_ref(txn.ref()), txn))
        return out


# --------------------------------------------------------------------------- one request


def _calc_json(calc: Any) -> dict[str, Any] | None:
    if calc is None:
        return None
    data = calc.to_json_dict() if hasattr(calc, "to_json_dict") else calc.model_dump(mode="json")
    return data if isinstance(data, dict) else None


def _failure_from_exception(exc: BaseException) -> dict[str, str]:
    name = type(exc).__name__
    if name == "BudgetExceeded":
        kind = "budget_exceeded"
    elif name in ("Unavailable", "MissingApiKey"):
        kind = "unavailable"
    elif name in ("SchemaValidationError", "ValidationError"):
        kind = "invalid_output"
    else:
        kind = "error"
    return {"type": kind, "detail": _clip(f"{name}: {exc}")}


def _failure_from_run(mode: str, result: Any, one_pass_run: Any) -> dict[str, str] | None:
    """The typed failure a model mode reports inside its trace, if any."""
    if mode == "agents":
        for entry in getattr(result, "unavailable", []) or []:
            head = str(entry).split(":", 1)[0].strip()
            kind = "invalid_output" if "(schema)" in str(entry) else _LLM_FAILURES.get(head)
            if kind:
                return {"type": kind, "detail": _clip(entry)}
        return None
    if mode == "one_pass":
        for event in result.trace:
            if event.actor != "one_pass":
                continue
            if event.tool == "generate" and event.outcome != "ok":
                reason = event.reason or ""
                if "SchemaValidationError" in reason:
                    kind = "invalid_output"
                else:
                    kind = _LLM_FAILURES.get(event.outcome, "error")
                return {"type": kind, "detail": _clip(reason or event.outcome)}
            if event.step == "validation" and event.outcome == "refused" and event.reason and (
                    "did not match the schema" in event.reason
                    or "not a JSON object" in event.reason):
                return {"type": "invalid_output", "detail": _clip(event.reason)}
    return None


def _run(world: _World, mode: str, rfc: Any, now: datetime, client: Any) -> tuple[Any, Any]:
    """Run the investigation in ``mode`` and return ``(InvestigationResult, OnePassRun | None)``."""
    common = dict(registry=world.registry, now=now, **world.connectors())
    if mode == "rules":
        # The holder's explanation goes in as an untrusted claim to check, never as evidence.
        return api.run_investigation(world.store, rfc.request_id, audience=rfc.audience,
                                     mode="rules", holder_claim=world.case.get("holder_claim"),
                                     **common), None
    if mode == "agents":
        return api.run_investigation(world.store, rfc.request_id, audience=rfc.audience,
                                     mode="agents", client=client, **common), None
    # one_pass goes through run_one_pass so the pre-policy proposal is available; the run then
    # gets the same audit row api.run_investigation(mode="one_pass") would write, so the holder's
    # passport records every investigation whatever the mode.
    from alibi.baselines import one_pass
    run = one_pass.run_one_pass(world.store, rfc, client=client,
                                holder_claim=world.case.get("holder_claim"), **common)
    _audit_model_run(world.store, rfc, run.result, actor=one_pass.ACTOR, at=now)
    return run.result, run


def _audit_model_run(store: Store, rfc: Any, result: Any, *, actor: str, at: datetime) -> None:
    """The ``run_investigation`` audit row of a model mode, in the words ``api.run_investigation``
    uses for the modes it dispatches itself."""
    answers = ", ".join(f"{claim.question_id} {ClaimStatus(claim.status).value}"
                        for claim in result.claims)
    problems = f"; {len(result.unavailable)} model step(s) failed" if result.unavailable else ""
    api._audit(store, action="run_investigation", actor=actor, outcome="ok", at=at,  # noqa: SLF001
               target_id=rfc.request_id,
               detail=f"run {result.run_id} ({result.mode}) for {rfc.audience}: {answers}; "
                      f"{result.calls_used} gateway calls{problems}")


def _links(world: _World, result: Any) -> list[dict[str, Any]]:
    holder = world.case.get("holder_claim") or {}
    txn_id = holder.get("txn_id")
    if not isinstance(txn_id, str) or not txn_id:
        return []
    try:
        links = world.store.list_links(txn_id=txn_id)
    except StoreError:
        return []
    if not links:
        return []
    link = links[-1]
    payouts: list[str] = []
    if link.link_state == LinkState.LINKED and link.record_ref is not None:
        payouts.append(link.record_ref.ref_id)
        # A settlement batch: every payout the source-of-funds claim cites belongs to the link.
        for claim in result.claims:
            if claim.question_id != Q_SOURCE or claim.status != ClaimStatus.SUPPORTED:
                continue
            for ref in claim.evidence_refs:
                if ref.ref_type != "record" or ref.ref_id in payouts:
                    continue
                try:
                    record = world.store.get_source(ref.ref_id, ref.revision)
                except StoreError:
                    continue
                if record.kind == RecordKind.PAYOUT:
                    payouts.append(ref.ref_id)
    return [{"txn_id": txn_id, "state": link.link_state.value,
             "payout_record_ids": payouts,
             "candidate_record_ids": [c.ref_id for c in link.candidates],
             "method": link.method}]


def _claims(world: _World, result: Any) -> dict[str, dict[str, Any]]:
    """The run's stored claims keyed by question id: status, the pinned gap code at the head
    of ``Claim.gap`` (unresolved answers only; gold carries no code for a contradiction), the
    typed ``Claim.facts``, and the cited refs (evidence plus the calculation's terms) in gold
    syntax."""
    out: dict[str, dict[str, Any]] = {}
    for claim in result.claims:
        status = ClaimStatus(claim.status).value
        refs = list(claim.evidence_refs)
        if claim.calculation is not None:
            refs.extend(claim.calculation.cited_refs)
        gap = _gap_code(claim.gap) if status == ClaimStatus.UNRESOLVED.value else None
        out[claim.question_id] = {
            "status": status, "gap": gap, "facts": dict(claim.facts),
            "evidence_refs": world.gold_refs(refs),
            "statement": claim.statement, "rationale": claim.rationale,
            "calculation": _calc_json(claim.calculation),
        }
    return out


def _package_claims(world: _World, draft: Any) -> list[dict[str, Any]]:
    """What the policy gate did to the claims: the body's claim per question (status, gap text,
    refs) and the review's ``claim_changes``. Debug data for the holder side; the scorer
    ignores it."""
    if draft is None:
        return []
    changes = {c.question_id: c.reason for c in draft.review.claim_changes}
    out = []
    for claim in draft.body.claims:
        out.append({"question_id": claim.question_id, "status": ClaimStatus(claim.status).value,
                    "gap": claim.gap, "evidence_refs": world.gold_refs(claim.evidence_refs),
                    "changed": changes.get(claim.question_id)})
    return out


def _attachment(world: _World, attachment: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"ref": world.gold_ref(attachment.ref), "form": attachment.form,
                            "source_trust": SourceTrust(attachment.source_trust).value}
    content = attachment.content if isinstance(attachment.content, dict) else {}
    item["content"] = copy.deepcopy(content)
    if attachment.form == "redacted_extract":
        item["fields"] = sorted(content)
    return item


def _package(world: _World, rfc: Any, draft: Any) -> dict[str, Any]:
    attachments = [_attachment(world, a) for a in draft.body.attachments] if draft is not None else []
    return {"request_id": rfc.request_id, "audience": rfc.audience, "empty": not attachments,
            "attachments": attachments}


def _exclusions(world: _World, draft: Any, disclosed: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if draft is not None:
        for entry in draft.review.excluded:
            ref = world.gold_ref(entry.ref)
            if ref in disclosed or ref in seen:
                continue
            seen.add(ref)
            item: dict[str, Any] = {"ref": ref, "reason": entry.reason}
            if entry.field:
                item["note"] = f"field {entry.field}"
            out.append(item)
    for ref, _item in world.all_refs():
        if ref in disclosed or ref in seen:
            continue
        seen.add(ref)
        out.append({"ref": ref, "reason": REASON_NOT_NEEDED})
    return out


def _proposal_attachments(world: _World, refs: Iterable[EvidenceRef]) -> list[dict[str, Any]]:
    items = []
    for token in world.gold_refs(refs):
        items.append({"ref": token})
    return items


def _proposed(world: _World, mode: str, rfc: Any, result: Any, one_pass_run: Any) -> dict[str, Any]:
    """The pre-policy proposal: the unfiltered cited set (rules), the one-pass model's
    disclosures (one_pass) or the Curator's kept set from the trace (agents)."""
    claims = [{"question_id": c.question_id, "status": ClaimStatus(c.status).value,
               "evidence_refs": world.gold_refs(
                   [*c.evidence_refs, *(c.calculation.cited_refs if c.calculation else [])])}
              for c in result.claims]
    refs: list[EvidenceRef] = []
    if mode == "one_pass" and one_pass_run is not None:
        for disclosure in one_pass_run.proposal.get("disclosures") or []:
            try:
                refs.append(EvidenceRef(ref_type="record", ref_id=disclosure["record_id"],
                                        revision=int(disclosure["revision"])))
            except (KeyError, TypeError, ValueError):
                continue
        # A model may propose a bank entry too; the proposal keys by record id, so a txn id
        # that the store knows as a bank entry is mapped to its txn ref.
        fixed: list[EvidenceRef] = []
        for ref in refs:
            try:
                world.store.get_source(ref.ref_id)
                fixed.append(ref)
            except StoreError:
                try:
                    txn = world.store.get_bank_txn(ref.ref_id)
                    fixed.append(txn.ref())
                except StoreError:
                    fixed.append(ref)
        refs = fixed
        claims = [{"question_id": c.get("question_id"), "status": c.get("status"),
                   "refs": [f"{r.get('record_id')}@{r.get('revision')}" for r in c.get("refs") or []]}
                  for c in one_pass_run.proposal.get("claims") or []]
    elif mode == "agents":
        kept_any = False
        for event in result.trace:
            if event.actor == "curator" and event.step == "note" and "kept" in event.result:
                kept_any = True
                for key in event.result.get("kept") or []:
                    try:
                        ref_type, rest = str(key).split(":", 1)
                        ref_id, revision = rest.rsplit("@", 1)
                        refs.append(EvidenceRef(ref_type=ref_type, ref_id=ref_id,
                                                revision=int(revision)))
                    except (ValueError, TypeError):
                        continue
        if not kept_any:
            refs = [r for c in result.claims for r in
                    [*c.evidence_refs, *(c.calculation.cited_refs if c.calculation else [])]]
    else:
        refs = [r for c in result.claims for r in
                [*c.evidence_refs, *(c.calculation.cited_refs if c.calculation else [])]]
    attachments = _proposal_attachments(world, refs)
    return {"request_id": rfc.request_id, "audience": rfc.audience, "empty": not attachments,
            "attachments": attachments, "claims": claims}


def _counts(world: _World, mode: str, result: Any, one_pass_run: Any) -> dict[str, Any]:
    """``{model_calls, tokens, lookups, observed_inputs, tool_calls}`` for the mode."""
    tool_calls = int(getattr(result, "calls_used", 0) or 0)
    if mode == "agents":
        from alibi.agents.runner import run_stats
        stats = run_stats(result)
        return {"model_calls": int(stats.get("model_calls", 0)), "tokens": int(stats.get("tokens", 0)),
                "lookups": int(stats.get("lookups", 0)),
                "observed_inputs": int(stats.get("observed_inputs", 0)), "tool_calls": tool_calls}
    if mode == "one_pass" and one_pass_run is not None:
        return {"model_calls": int(one_pass_run.model_calls), "tokens": int(one_pass_run.tokens),
                "lookups": int(one_pass_run.lookups),
                "observed_inputs": int(one_pass_run.observed_inputs), "tool_calls": tool_calls}
    lookups = sum(1 for e in result.trace
                  if e.step == "tool_call" and e.actor == "gateway" and e.tool == "lookup_settlement")
    observed = len(world.store.list_sources(world.subject)) + len(world.store.list_bank_txns(world.subject))
    return {"model_calls": 0, "tokens": 0, "lookups": lookups, "observed_inputs": observed,
            "tool_calls": tool_calls}


def _answer(world: _World, mode: str, rfc: Any, now: datetime, client: Any,
            schema: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    prediction: dict[str, Any] = {
        "case_number": world.number, "request_id": rfc.request_id, "system": mode,
        "links": [], "claims": {}, "package": None, "proposed_package": None, "exclusions": [],
        "cost": {"model_calls": 0, "tool_calls": 0, "tokens": 0, "latency_ms": 0.0,
                 "cost_usd_estimate": 0.0},
        "cost_note": "cost_usd_estimate is 0: no price list is pinned; tokens are reported",
        "retrieval": {"lookups": 0, "observed_inputs": 0, "strategy": STRATEGIES[mode]},
        "failure": None, "issued_at": format_utc(now),
    }
    result = one_pass_run = draft = None
    try:
        result, one_pass_run = _run(world, mode, rfc, now, client)
    except Exception as exc:  # noqa: BLE001 - typed below; nothing escapes run_case
        prediction["failure"] = _failure_from_exception(exc)
    if result is not None:
        prediction["failure"] = _failure_from_run(mode, result, one_pass_run)
        try:
            draft = api.build_review(world.store, rfc.request_id, audience=rfc.audience, now=now)
        except Exception as exc:  # noqa: BLE001 - the claims still go out, unpackaged
            if prediction["failure"] is None:
                prediction["failure"] = {"type": "error",
                                         "detail": _clip(f"build_review: {type(exc).__name__}: {exc}")}
        prediction["links"] = _links(world, result)
        prediction["claims"] = _claims(world, result)
        prediction["package_claims"] = _package_claims(world, draft)
        prediction["package"] = _package(world, rfc, draft)
        disclosed = {a["ref"] for a in prediction["package"]["attachments"]}
        for claim in prediction["claims"].values():
            disclosed.update(claim["evidence_refs"])
        prediction["exclusions"] = _exclusions(world, draft, disclosed)
        prediction["proposed_package"] = _proposed(world, mode, rfc, result, one_pass_run)
        counts = _counts(world, mode, result, one_pass_run)
        prediction["cost"].update(model_calls=counts["model_calls"], tool_calls=counts["tool_calls"],
                                  tokens=counts["tokens"])
        prediction["retrieval"].update(lookups=counts["lookups"],
                                       observed_inputs=counts["observed_inputs"])
    prediction["cost"]["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    problems = jsonschema_lite.errors(schema, prediction)
    if problems:
        # Never emit an object the scorer would count as invalid_output silently: say so.
        prediction["failure"] = {"type": "invalid_output",
                                 "detail": _clip("; ".join(problems))}
        prediction["package"] = {"request_id": rfc.request_id, "audience": rfc.audience,
                                 "empty": True, "attachments": []}
        prediction["claims"] = {q: c for q, c in prediction["claims"].items()
                                if not jsonschema_lite.errors(schema["$defs"]["claim"], c)}
        jsonschema_lite.validate(schema, prediction)
    return prediction


# --------------------------------------------------------------------------- run_case


def _client_for(mode: str, number: int, client: Any, run_id: str | None, replay: bool | None):
    """``(client to pass to the engines, live client to scope)`` for a model mode."""
    if mode == "rules":
        return None, None
    from alibi.agents.replay import make_client
    if client is None:
        live = None
        if not _replay_on():
            from alibi.llm.client import GeminiClient
            live = GeminiClient.for_run(mode, run_id or _default_run_id(), case_number=number,
                                        overwrite_log=True)
        return make_client(case=f"case{number}", variant=mode, live=live), live
    if replay:
        return make_client(case=f"case{number}", variant=mode, live=client), client
    return client, client


def _default_run_id() -> str:
    return "adapter-" + datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _case_root(mode: str, number: int, root: Path | str | None) -> Path:
    base = Path(root) if root is not None else llm_config.data_dir() / "adapter" / mode
    return base / f"case_{number}"


def run_case(case: int | dict[str, Any], mode: str = "rules", as_of: datetime | str | None = None,
             *, client: Any = None, run_id: str | None = None, replay: bool | None = None,
             root: Path | str | None = None,
             trusted_issuers: Mapping[str, str] | None = None,
             synthetic_key_seed: str | None = None) -> list[dict[str, Any]]:
    """Run one case in ``mode`` through the simulated services and return its predictions.

    ``case`` is a case number (``agents.data.api.load_case``) or a case dict; gold is stripped.
    ``as_of`` answers every RFC issued by that moment at that moment instead of each RFC at its
    own ``issued_at``. ``client`` is the model client for ``one_pass`` and ``agents`` (any object
    with ``GeminiClient.generate``'s signature, such as ``alibi.llm.fake.FakeClient``); without
    one, ``GeminiClient.for_run(mode, run_id)`` scoped to the case and wrapped by the replay
    recorder is used (``ALIBI_REPLAY=1`` serves the recording instead). ``replay=True`` wraps a
    given client in the recorder too. ``root`` moves the per-case store folder (default
    ``data/alibi/adapter/<mode>/case_<n>``); it is emptied first, so every run starts fresh.
    ``trusted_issuers`` is an optional ``kid -> issuer_id`` mapping from trusted synthetic
    service setup. It is shared by all modes and never read from the case or its events.
    ``synthetic_key_seed`` optionally derives repeatable keys for every simulated service
    across fresh workers. It is secret key material from trusted setup, never case data;
    use a private high-entropy random value and bind the public-key pins before capture.
    """
    mode = normalise_mode(mode)
    case_dict = _load_case(case)
    number = int(case_dict["case_number"])
    schema = load_schema("prediction")
    folder = _case_root(mode, number, root)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)

    rfcs = sorted(case_dict.get("rfcs", []), key=lambda r: (parse_time(r["issued_at"], "issued_at"),
                                                            r["request_id"]))
    fixed = parse_time(as_of, "as_of") if as_of is not None else None
    if fixed is not None:
        rfcs = [r for r in rfcs if parse_time(r["issued_at"], "issued_at") <= fixed]
    if not rfcs:
        return []
    start = fixed if fixed is not None else parse_time(rfcs[0]["issued_at"], "issued_at")

    world = _World(case_dict, folder, trusted_issuers=trusted_issuers,
                   synthetic_key_seed=synthetic_key_seed)
    predictions: list[dict[str, Any]] = []
    try:
        engine_client, live = _client_for(mode, number, client, run_id, replay)
        scoped = getattr(live, "scoped", None) if live is not None else None
        from contextlib import nullcontext
        with (scoped(case_number=number) if callable(scoped) else nullcontext()):
            clock = CaseClock(case_dict, start)
            world.seed(clock)
            world.policy(clock)
            for rfc_case in rfcs:
                moment = fixed if fixed is not None else parse_time(rfc_case["issued_at"], "issued_at")
                if moment > clock.as_of:
                    world.advance(clock, moment)
                try:
                    rfc = world.store.get_request(rfc_case["request_id"], audience=rfc_case["audience"])
                except StoreError as exc:
                    predictions.append(_refused(world, rfc_case, mode, clock.as_of, schema,
                                                f"request not in the passport: {_clip(exc)}"))
                    continue
                predictions.append(_answer(world, mode, rfc, clock.as_of, engine_client, schema))
    finally:
        world.close()
    return predictions


def _refused(world: _World, rfc_case: dict[str, Any], mode: str, now: datetime,
             schema: dict[str, Any], detail: str) -> dict[str, Any]:
    prediction = {
        "case_number": world.number, "request_id": rfc_case["request_id"], "system": mode,
        "links": [], "claims": {},
        "package": {"request_id": rfc_case["request_id"], "audience": rfc_case["audience"],
                    "empty": True, "attachments": []},
        "proposed_package": None, "exclusions": [],
        "cost": {"model_calls": 0, "tool_calls": 0, "tokens": 0, "latency_ms": 0.0,
                 "cost_usd_estimate": 0.0},
        "retrieval": {"lookups": 0, "observed_inputs": 0, "strategy": STRATEGIES[mode]},
        "failure": {"type": "error", "detail": _clip(detail)}, "issued_at": format_utc(now),
    }
    jsonschema_lite.validate(schema, prediction)
    return prediction


# --------------------------------------------------------------------------- many cases, the CLI


def _parse_numbers(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def select_cases(numbers: Iterable[int] | None = None,
                 families: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """The visible development cases with those numbers (all when None) in those families."""
    wanted = set(numbers) if numbers is not None else None
    fams = set(families) if families is not None else None
    rows = data_api.load_index()
    chosen = [r for r in rows if (wanted is None or r["case_number"] in wanted)
              and (fams is None or r["family"] in fams)]
    return [data_api.load_case(r["case_number"]) for r in chosen]


def run_cases(cases: Iterable[dict[str, Any]], mode: str, *, out: Path, as_of: str | None = None,
              run_id: str | None = None, client: Any = None, log=None,
              root: Path | str | None = None,
              trusted_issuers: Mapping[str, str] | None = None,
              synthetic_key_seed: str | None = None) -> dict[str, Any]:
    """Run every case in ``mode``, write ``<out>/<mode>.jsonl`` (system line first) and return the
    summary ``{mode, cases, requests, failures, lookups, observed_inputs, model_calls, tokens}``.
    A case whose run raises (a seeding problem, not a model failure) is written as one typed
    ``error`` failure per RFC so the file stays complete. ``root`` is ``run_case``'s: the folder
    the per-case stores go under (default ``data/alibi/adapter/<mode>/``).
    ``trusted_issuers`` forwards the same explicit synthetic service pins to every case.
    ``synthetic_key_seed`` forwards the same private synthetic key seed to every case.
    """
    mode = normalise_mode(mode)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{mode}.jsonl"
    schema = load_schema("prediction")
    summary = {"mode": mode, "cases": 0, "requests": 0, "failures": 0, "lookups": 0,
               "observed_inputs": 0, "model_calls": 0, "tokens": 0}
    live = client
    if mode != "rules" and client is None and not _replay_on():
        from alibi.llm.client import GeminiClient
        live = GeminiClient.for_run(mode, run_id or _default_run_id(), overwrite_log=True)
    log = log or (lambda text: print(text, flush=True))
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"system": system_label(mode)}, ensure_ascii=False) + "\n")
        for case in cases:
            number = int(case["case_number"])
            started = time.perf_counter()
            try:
                predictions = run_case(case, mode, as_of, client=live, run_id=run_id,
                                       replay=(mode != "rules" and client is None), root=root,
                                       trusted_issuers=trusted_issuers,
                                       synthetic_key_seed=synthetic_key_seed)
            except Exception as exc:  # noqa: BLE001 - reported as typed failures, never a crash
                predictions = []
                for rfc in sorted(case.get("rfcs", []), key=lambda r: r["issued_at"]):
                    predictions.append({
                        "case_number": number, "request_id": rfc["request_id"], "system": mode,
                        "links": [], "claims": {},
                        "package": {"request_id": rfc["request_id"], "audience": rfc["audience"],
                                    "empty": True, "attachments": []},
                        "exclusions": [], "retrieval": {"lookups": 0, "observed_inputs": 0,
                                                        "strategy": STRATEGIES[mode]},
                        "cost": {"model_calls": 0, "tool_calls": 0, "tokens": 0, "latency_ms": 0.0,
                                 "cost_usd_estimate": 0.0},
                        "failure": {"type": "error", "detail": _clip(f"{type(exc).__name__}: {exc}")}})
                    jsonschema_lite.validate(schema, predictions[-1])
            failures = sum(1 for p in predictions if p.get("failure"))
            summary["cases"] += 1
            summary["requests"] += len(predictions)
            summary["failures"] += failures
            for p in predictions:
                summary["lookups"] += int((p.get("retrieval") or {}).get("lookups", 0))
                summary["observed_inputs"] += int((p.get("retrieval") or {}).get("observed_inputs", 0))
                summary["model_calls"] += int((p.get("cost") or {}).get("model_calls", 0))
                summary["tokens"] += int((p.get("cost") or {}).get("tokens", 0))
                handle.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            statuses = ",".join(f"{q.split('.')[-1]}={c['status'][:3]}"
                                for p in predictions for q, c in (p.get("claims") or {}).items())
            log(f"case {number} {mode}: {len(predictions)} request(s), {failures} failure(s), "
                f"lookups {sum(int((p.get('retrieval') or {}).get('lookups', 0)) for p in predictions)}, "
                f"{statuses or 'no claims'}, {round((time.perf_counter() - started) * 1000)} ms")
    summary["out"] = str(path)
    return summary


def _under_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(llm_config.data_dir().resolve())
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alibi.adapter",
                                     description="Run the team's cases through the simulated "
                                                 "services and write predictions per mode.")
    parser.add_argument("--mode", default="rules", help="rules | one_pass | agents")
    parser.add_argument("--modes", help="comma-separated modes; overrides --mode")
    parser.add_argument("--cases", default="1-80", help="case numbers, e.g. 1-80 or 1,2,71")
    parser.add_argument("--families", help="comma-separated families to keep")
    parser.add_argument("--out", type=Path, default=llm_config.data_dir() / "predictions",
                        help="folder for <mode>.jsonl (must be under data/alibi)")
    parser.add_argument("--as-of", dest="as_of", help="answer every RFC issued by this moment at it")
    parser.add_argument("--run-id", dest="run_id", help="the Gemini log run id (model modes)")
    args = parser.parse_args(argv)

    modes = [normalise_mode(m.strip()) for m in (args.modes or args.mode).split(",") if m.strip()]
    if not _under_data_dir(args.out):
        print(f"--out must be under {llm_config.data_dir()}", file=sys.stderr)
        return 2
    families = [f.strip() for f in args.families.split(",")] if args.families else None
    cases = select_cases(_parse_numbers(args.cases), families)
    if not cases:
        print("no visible cases match", file=sys.stderr)
        return 2
    summaries = []
    for mode in modes:
        summaries.append(run_cases(cases, mode, out=args.out, as_of=args.as_of, run_id=args.run_id))
    for summary in summaries:
        print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
