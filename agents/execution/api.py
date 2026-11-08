"""Execution's public interface. Teammates import only this module.

    from agents.execution.api import load_probes, core_probes, probe_ids, summarise

The probe catalogue lives in agents/execution/out/probes.json and is rendered to
agents/execution/out/PROBES.md with `uv run --frozen python -m agents.execution.probes`.
The analyst disposition checklist is agents/execution/out/ANALYST_FLOW.md.

Each probe has `kind`: "refusal" (the backend must refuse it) or "control" (it must be
accepted). `summarise` turns per-probe outcomes into the `probes_blocked` /
`probes_attempted` keys from shared/CONTRACTS.md.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
PROBES_JSON = OUT / "probes.json"


def load_probes() -> list[dict]:
    """All probes in catalogue order."""
    return json.loads(PROBES_JSON.read_text(encoding="utf-8"))["probes"]


def core_probes() -> list[dict]:
    """Probes that must pass before the 00:15 gate."""
    return [p for p in load_probes() if p["core"]]


def probe_ids() -> list[str]:
    return [p["id"] for p in load_probes()]


def summarise(outcomes: dict[str, str]) -> dict:
    """Score probe outcomes.

    `outcomes` maps probe ID to what the system did: "refused" or "accepted".
    A refusal probe passes when refused; a control probe passes when accepted.
    Probes with no outcome count as attempted and failed, so nothing silently drops out
    of the denominator.
    """
    probes = {p["id"]: p for p in load_probes()}
    unknown = sorted(set(outcomes) - set(probes))
    if unknown:
        raise ValueError(f"unknown probe IDs: {unknown}")
    bad = sorted(k for k, v in outcomes.items() if v not in ("refused", "accepted"))
    if bad:
        raise ValueError(f"outcome must be 'refused' or 'accepted': {bad}")

    refusal = [p for p in probes.values() if p["kind"] == "refusal"]
    control = [p for p in probes.values() if p["kind"] == "control"]
    blocked = sum(outcomes.get(p["id"]) == "refused" for p in refusal)
    allowed = sum(outcomes.get(p["id"]) == "accepted" for p in control)
    failed = sorted(
        p["id"]
        for p in probes.values()
        if outcomes.get(p["id"]) != ("refused" if p["kind"] == "refusal" else "accepted")
    )
    return {
        "probes_attempted": len(refusal),
        "probes_blocked": blocked,
        "controls_attempted": len(control),
        "controls_allowed": allowed,
        "failed": failed,
    }


def load_policy_rules() -> list[dict]:
    """40 plain-English privacy rules with expected compilations (out/policy_rules.jsonl)."""
    with (OUT / "policy_rules.jsonl").open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_policy_material() -> dict:
    """Passport vocabulary the policy parser receives for the rules above."""
    return json.loads((OUT / "policy_material.json").read_text(encoding="utf-8"))


def load_injections() -> list[dict]:
    """25 adversarial strings with plant location and expected behaviour (out/injections.jsonl)."""
    with (OUT / "injections.jsonl").open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verifier_probe_plan() -> list[dict]:
    """SP-25..SP-32 steps in agents.eval.api.score_probe_steps `plan` format."""
    from agents.execution.verifier_probes import score_plan

    return score_plan()


def verifier_http_handlers() -> dict:
    """SP-25..SP-32 HTTP handlers for eval's ProbeSession: probe_id -> callable(ctx) -> "blocked" | "not_blocked" | "not_run".

    The ctx contract is documented in agents/execution/verifier_http.py.
    """
    from agents.execution.verifier_http import handlers

    return handlers()


def compile_policy(item, material: dict | None = None) -> dict:
    """Plain text, one typed rule, a list, or parser output -> {"rules", "unresolved", "alerts", "held", "version"}.

    Reference compiler for the Policy row in shared/CONTRACTS.md; see agents/execution/policy.py.
    """
    from agents.execution.policy import compile_policy as _compile

    return _compile(item, material)


def apply_policy(package: dict, compiled: dict, recipient: str, *, records: dict, now) -> dict:
    """Filter a package for one recipient -> {"held", "package", "gaps", "private_review", "version"}.

    Deny wins, withheld evidence leaves a generic gap, a final scan fails closed. `records` is
    agents.execution.api.record_index(case).
    """
    from agents.execution.policy import apply_policy as _apply

    return _apply(package, compiled, recipient, records=records, now=now)


def record_index(case: dict) -> dict:
    """record_id / txn_id -> metadata apply_policy needs, built from a data case."""
    from agents.execution.policy import record_index as _index

    return _index(case)


def approval_gate():
    """A fresh reference ApprovalGate implementing out/consent_matrix.md (C1-C16).

    See agents/execution/approval_gate.py; CONSENT_MATRIX there maps each code to its rows and
    verbatim reason template for HTTP assertions.
    """
    from agents.execution.approval_gate import ApprovalGate

    return ApprovalGate()
