"""What a claim rests on: only an answered claim cites records.

Minimum disclosure applied at the claim: a claim that is ``supported`` or ``contradicted``
cites the records it rests on; an ``unresolved`` claim rests on nothing established, so it
cites no evidence record or calculation. Its private examined refs retain every source
dependency for corrections and approval checks; the gap and trace explain what is missing. Because
the package attaches only what claims cite, a request whose questions are all unresolved
sends the bank no records at all.

All three modes (rules, one-pass, agents) build their claims through these two helpers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from alibi.core.schemas import Calculation, ClaimStatus, EvidenceRef

__all__ = ["answered_calculation", "answered_refs", "examined_refs", "is_answered"]


def is_answered(status: Any) -> bool:
    """True for ``supported`` and ``contradicted``."""
    try:
        return ClaimStatus(status) != ClaimStatus.UNRESOLVED
    except ValueError:
        return False


def answered_refs(status: Any, refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    """The refs in order without exact repeats for an answered claim; none for unresolved."""
    if not is_answered(status):
        return []
    seen: dict[str, EvidenceRef] = {}
    for ref in refs:
        seen.setdefault(ref.key(), ref)
    return list(seen.values())


def answered_calculation(status: Any, calculation: Calculation | None) -> Calculation | None:
    """Only an answered claim discloses a calculation; unresolved arithmetic stays in trace."""
    return calculation if is_answered(status) else None


def examined_refs(status: Any, refs: Iterable[EvidenceRef],
                  calculation: Calculation | None) -> list[EvidenceRef]:
    """Private dependencies captured before unresolved citations/calculations are removed."""
    if is_answered(status):
        return []
    dependencies = list(refs)
    if calculation is not None:
        dependencies.extend(calculation.cited_refs)
        dependencies.extend(term.ref for term in calculation.terms)
        if calculation.target_ref is not None:
            dependencies.append(calculation.target_ref)
        if calculation.timing is not None:
            dependencies.extend((calculation.timing.settlement_ref, calculation.timing.credit_ref))
    unique = {ref.key(): ref for ref in dependencies}
    return [unique[key] for key in sorted(unique)]
