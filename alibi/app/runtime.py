"""One running Alibi: the passport store, demo keys, the loaded team case and app-side state.

``Runtime.open(settings)`` opens (or creates) the encrypted passport for the configured case,
ensures the demo keystore and trust registry exist, fills the passport and exposes the clock
every route uses. Two ways to fill it:

* ``case_adapter`` (default, the single-process app and every test): the case is replayed through
  ``alibi/app/cases.py`` at start. A replay is all duplicates, so restarts keep history.
* ``connectors`` (``ALIBI_MERCHANT_URL`` and ``ALIBI_BANK_URL`` both set): nothing is replayed.
  The passport fills through the connect routes: the merchant pushes signed events to
  ``POST /connect/merchant/webhook``, ``POST /connect/bank/sync`` pulls the holder's feed from the
  bank service, and the bank forwards every request it issues to ``POST /connect/bank/rfc``. Every
  path is idempotent (duplicates), so a restart keeps history and nothing is loaded twice.

``Runtime.http_client(base_url)`` is the one place outbound HTTP clients come from, so tests
inject ``TestClient`` instances for the merchant and bank URLs and no socket is opened.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import httpx

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
SEED_CONNECTORS = "connectors"
SEED_CASE_ADAPTER = "case_adapter"
HTTP_TIMEOUT = 10.0


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
        self.seed = SEED_CONNECTORS if settings.connectors else SEED_CASE_ADAPTER
        # Outbound HTTP: base_url -> client with httpx's ``get``/``post`` taking absolute URLs.
        # Tests put ``TestClient`` instances here; otherwise one ``httpx.Client`` per base URL.
        self.http_clients: dict[str, Any] = {}
        self.settlement_lookup = (self._merchant_settlement_lookup() if settings.merchant_url
                                  else cases.settlement_lookup_for(case, keystore, self.clock))
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
        if rt.seed == SEED_CASE_ADAPTER:
            rt.load_case()
        from alibi.app import monitor
        monitor.tick(rt)             # proactive checks run on every start, before any request
        return rt

    def load_case(self) -> dict[str, Any]:
        """Replay the case's deliveries up to the clock into the passport (idempotent)."""
        with self.lock:
            self.load_summary = cases.load_case(self.store, self.case, self.keystore,
                                                up_to=self.clock())
            # Every request the adapter delivered was signed by the issuing bank's key and
            # receive_rfc verified that signature before storing it; keep that outcome.
            for request_id in self.load_summary["requests"]:
                issuer = self.store.get_request(request_id).issuer
                self.record_rfc_signature(request_id, issuer)
            return self.load_summary

    def record_rfc_signature(self, request_id: str, key_id: str) -> None:
        """Remember that ``receive_rfc`` verified this request's bank signature at intake."""
        self.sidecar.update("rfc_signatures", lambda cur, rid=request_id, kid=key_id: {
            **(cur or {}), rid: {"verified": True, "key_id": kid}})

    def close(self) -> None:
        self.store.close()
        for client in self.http_clients.values():
            close = getattr(client, "close", None)
            if callable(close) and isinstance(client, httpx.Client):
                close()

    # ----------------------------------------------------------------------- outbound HTTP

    def http_client(self, base_url: str) -> Any:
        """The client for one service base URL (an ``httpx.Client`` unless a test injected one).
        Callers pass absolute URLs, so a ``TestClient`` stands in without changes."""
        key = (base_url or "").rstrip("/")
        client = self.http_clients.get(key)
        if client is None:
            client = httpx.Client(timeout=HTTP_TIMEOUT)
            self.http_clients[key] = client
        return client

    def service_client(self, base_url: str) -> "_Prefixed":
        """A client taking service paths (``/inbox``), for ``alibi.connectors.clients``: it
        prefixes ``base_url`` and sends through ``http_client`` (so tests still inject)."""
        return _Prefixed(self, (base_url or "").rstrip("/"))

    def _merchant_settlement_lookup(self):
        """The gateway's ``settlement_lookup`` over the running mock merchant
        (``GET {ALIBI_MERCHANT_URL}/settlements?payout_reference=``)."""
        from alibi.connectors.clients import MerchantClient

        return MerchantClient(self.service_client(self.settings.merchant_url or "")).settlement_lookup

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


class _Prefixed:
    """``get``/``post`` over one service: a path in, an absolute URL out through ``http_client``."""

    def __init__(self, rt: Runtime, base_url: str):
        self.rt = rt
        self.base_url = base_url

    def get(self, path: str, **kwargs):
        return self.rt.http_client(self.base_url).get(f"{self.base_url}{path}", **kwargs)

    def post(self, path: str, **kwargs):
        return self.rt.http_client(self.base_url).post(f"{self.base_url}{path}", **kwargs)


def _is_ident(value: Any) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}", value) is not None
