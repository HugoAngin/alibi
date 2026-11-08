"""Known pending payout state remains an unresolved settlement gap, with private dependencies."""
import pytest
from alibi.core.schemas import ClaimStatus
from alibi.tests.test_investigate_rules import passport, variant, record_of, investigate, claim_of

@pytest.mark.parametrize("status, expected_gap", [("pending", "settlement_pending_at_booking"), ("requested", "settlement_not_found")])
def test_no_later_settlement_distinguishes_pending_from_unknown(passport, keystore, now, status, expected_gap):
    def edit(fx):
        record_of(fx, "po_5001")["status"] = status
    store, fx = passport(variant("supported", edit))
    result, _ = investigate(store, fx, keystore, now, lookup=False)
    claim = claim_of(result)
    assert claim.status == ClaimStatus.UNRESOLVED
    assert claim.gap == expected_gap
    assert claim.evidence_refs == [] and claim.calculation is None
    assert "po_5001" in {r.ref_id for r in claim.examined_refs}
    assert "No settlement record" in claim.gap_detail
