"""One running Alibi: the passport store, demo keys, the loaded team case and app-side state.

``Runtime.open(settings)`` opens (or creates) the encrypted passport for the configured case,
ensures the demo keystore and trust registry exist, loads the case through the adapter (a replay
is all duplicates, so restarts keep history) and exposes the clock every route uses.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from agents.data import api as data_api
from alibi import api
from alibi.app import cases
from alibi.app.config import APP_KEYS, Settings, settings_from_env
from alibi.app.sidecar import Sidecar
from alibi.core import canon
from alibi.core.schemas import AuditEvent, format_utc
from alibi.core.signing import DemoKeystore
from alibi.core.store import Store

APP_ACTOR = "app"


class Runtime:
    def __init__(self, settings: Settings, store: Store, keystore: DemoKeystore, case: dict,
                 sidecar: Sidecar):
        self.settings = settings
        self.store = store
        self.keystore = keystore
        self.registry = keystore.registry()
        self.case = case
        self.sidecar = sidecar
        self.lock = threading.RLock()
        self.subject_ref: str = case["subject_ref"]
        self.connection_id = cases.connection_id(case)
        self.account_refs = sorted({d["row"]["account_ref"] for d in case.get("bank_deliveries", [])})
        self.settlement_lookup = cases.settlement_lookup_for(case, keystore, self.clock)
        self.load_summary: dict[str, Any] | None = None

    # ----------------------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, settings: Settings | None = None, *, store: Store | None = None,
             case: dict | None = None) -> "Runtime":
        settings = settings or settings_from_env()
        case = case or data_api.load_case(settings.case_number)
        keystore = DemoKeystore(settings.key_dir, specs=APP_KEYS).ensure()
        if store is None:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            store = api.open_store(settings.db_path)
        sidecar = Sidecar(settings.sidecar_path if settings.data_dir is not None else None)
        rt = cls(settings, store, keystore, case, sidecar)
        rt.load_case()
        from alibi.app import monitor
        monitor.tick(rt)             # proactive checks run on every start, before any request
        return rt

    def load_case(self) -> dict[str, Any]:
        """Replay the case's deliveries up to the clock into the passport (idempotent)."""
        with self.lock:
            self.load_summary = cases.load_case(self.store, self.case, self.keystore,
                                                up_to=self.clock())
            return self.load_summary

    def close(self) -> None:
        self.store.close()

    # ----------------------------------------------------------------------- clock and ids

    def clock(self) -> datetime:
        return self.settings.now()

    def now_text(self) -> str:
        return format_utc(self.clock())

    def recipient_name(self, recipient: str) -> str:
        from alibi.app.config import recipient_name
        return recipient_name(recipient)

    # ----------------------------------------------------------------------- audit

    def audit(self, *, action: str, outcome: str, target_id: str | None = None,
              detail: str | None = None, actor: str = APP_ACTOR) -> None:
        """One app-level audit event in the passport (ids and reasons only, never amounts)."""
        at = self.clock()
        text = " ".join(str(detail).split())[:300] if detail else None
        target = target_id if _is_ident(target_id) else None
        audit_id = canon.make_id("aud", action, actor, target, outcome, text, format_utc(at))
        self.store.append_audit(AuditEvent(audit_id=audit_id, at=at, actor=actor, action=action,
                                           target_id=target, outcome=outcome, detail=text))

    def bank_audit(self, recipient: str, *, action: str, outcome: str, target_id: str | None,
                   detail: str | None = None, actor: str | None = None) -> None:
        """Bank-side audit entry, kept with the bank's own state (never in the holder's passport)."""
        entry = {"entry_id": canon.make_id("baud", recipient, action, target_id, outcome, detail,
                                           self.now_text()),
                 "at": self.now_text(), "actor": actor or recipient, "role": "bank",
                 "action": action, "target_id": target_id, "outcome": outcome,
                 "detail": (detail or "")[:300]}
        self.sidecar.append(f"bank_audit:{recipient}", entry)

    def bank_audit_entries(self, recipient: str | None = None) -> list[dict]:
        keys = [f"bank_audit:{r}" for r in ((recipient,) if recipient else ("bank_a", "bank_b"))]
        out: list[dict] = []
        for key in keys:
            out.extend(self.sidecar.get(key, []) or [])
        return out


def _is_ident(value: Any) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", value) is not None
