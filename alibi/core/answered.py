"""What a claim rests on: only an answered claim cites records.

Minimum disclosure applied at the claim: a claim that is ``supported`` or ``contradicted``
cites the records it rests on; an ``unresolved`` claim rests on nothing established, so it
cites no evidence record. It keeps its calculation, and its gap code and gap sentence say what
is missing; the investigation trace still records every read and check. Because
the package attaches only what claims cite, a request whose questions are all unresolved
sends the bank no records at all.

All three modes (rules, one-pass, agents) build their claims through these two helpers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from alibi.core.schemas import Calculation, ClaimStatus, EvidenceRef

__all__ = ["answered_calculation", "answered_refs", "is_answered"]


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
    """The calculation, kept for every claim so the arithmetic behind an unresolved answer
    (for example amounts that do not reconcile) stays visible."""
    return calculation
