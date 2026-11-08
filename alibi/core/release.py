"""Conservative diagnostic recognition of issuer-attested platform-balance releases."""
import re
from datetime import timedelta

from alibi.core.schemas import BankTxn, EvidenceRecord, RecordKind, SourceTrust

_AVAILABLE = re.compile(r"\b(?:is|are) now available in (?:your |the )?(?:seller|platform) balance\b", re.I)
_RELEASED = re.compile(r"\b(?:has|have) been released to (?:your |the )?(?:seller|platform) balance\b", re.I)
_NEGATED = re.compile(r"\b(?:no|not|never|isn't|aren't|wasn't|pending|failed|reversed|cancelled|if|whether|might|may|would|could)\b", re.I)


def is_balance_release(record: EvidenceRecord, txn: BankTxn, window: timedelta) -> bool:
    """Recognize a release diagnostic, never proof of settlement or permission to share."""
    if (record.subject_ref != txn.subject_ref or record.source_trust != SourceTrust.ISSUER_VERIFIED
            or record.kind != RecordKind.MESSAGE or record.amount_pence != txn.amount_pence
            or record.currency != txn.currency or not txn.is_credit
            or not timedelta(0) <= txn.booked_at - record.occurred_at <= window):
        return False
    if record.status == "released_to_balance":
        return True
    if record.status is not None:
        return False
    text = " ".join((record.text or "").split())
    return not _NEGATED.search(text) and bool(_AVAILABLE.search(text) or _RELEASED.search(text))
