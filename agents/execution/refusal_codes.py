"""Map alibi.core approval problems to the consent_matrix.md refusal codes.

    from agents.execution.api import refusal_from_problems
    try:
        sign_and_package(store, approval_id, ...)
    except ApprovalError as exc:
        body = refusal_from_problems(exc.problems)   # {"code", "reason", "problems"}
        return JSONResponse(body, status_code=409)

shared/CONTRACTS.md (Refusals row) pins a 4xx with {"code", "reason"}, codes from consent_matrix.md
and the reason as the backend's own sentence. alibi.core.approval.check_approval returns every
problem as free text and no code. This picks the code of the first problem in consent_matrix.md
check order, so the code is deterministic whatever order the problems come in, and keeps the
core's own sentence as the reason. An unknown problem maps to `approval_invalid`, never to a
pass.
"""
from __future__ import annotations

import re

# consent_matrix.md check order: first match wins.
ORDER = [
    ("approval_missing", [r"^no valid approval was given", r"^the approval was already used",
                          r"^only a pending approval can be used", r"^no holder approval"]),
    ("request_expired", [r"^the request .* expired", r"request .* has expired"]),
    ("approval_expired", [r"^the approval expired at", r"^the approval is not valid before"]),
    ("approval_request_mismatch", [r"^the approval is for request ", r"^the package body answers request "]),
    ("approval_audience_mismatch", [r"^the approval is for recipient ", r"^the package body is addressed to "]),
    ("policy_version_changed", [r"^the disclosure policy changed after approval"]),
    ("source_revision_changed", [r"^source \S+ changed after approval", r"^source \S+ is missing",
                                 r"^the approval does not cover source "]),
    ("approval_digest_mismatch", [r"^the package body changed after it was approved",
                                  r"^the package body is not a valid package body"]),
]
# An invalidated approval carries the reason it was invalidated; map that reason too.
INVALIDATED = re.compile(r"^the approval was invalidated(?::\s*(?P<why>.*))?$")
FALLBACK = "approval_invalid"


def code_for(problem: str) -> tuple[int, str] | None:
    for rank, (code, patterns) in enumerate(ORDER):
        if any(re.search(p, problem, re.I) for p in patterns):
            return rank, code
    return None


def refusal_from_problems(problems) -> dict:
    """[problem, ...] from ApprovalError.problems -> {"code", "reason", "problems"}."""
    problems = [str(p) for p in (problems or [])] or ["the approval is not valid"]
    ranked = []
    for p in problems:
        hit = code_for(p)
        if hit is None:
            m = INVALIDATED.match(p)
            if m:
                inner = code_for(m.group("why") or "")
                hit = (inner[0], inner[1]) if inner else (0, "approval_missing")
        if hit is not None:
            ranked.append((hit[0], hit[1], p))
    if not ranked:
        return {"code": FALLBACK, "reason": problems[0], "problems": problems}
    rank, code, reason = min(ranked, key=lambda r: r[0])
    return {"code": code, "reason": reason, "problems": problems}
