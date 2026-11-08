"""The read-only bank feed: the server side of the two ``/feed`` routes, the transports that
reach it, and the passport-side client that syncs it into the store.

* ``FeedServer`` serves one case's visible bank rows (``CaseClock.visible_bank_rows``) in
  arrival order from an opaque cursor ``c:<n>`` (``n`` rows already served). The mock bank
  wraps ``page`` in ``GET /feed/transactions`` and ``latest`` in ``GET /feed/transactions/{txn_id}``.
* ``LocalFeed`` and ``HttpFeed`` are the transports a ``FeedClient`` reads through. Both answer
  ``page(cursor, limit) -> dict`` and ``txn(txn_id) -> dict | None``, where the txn answer is the
  route's shape ``{"as_of", "source_version", "transaction": row}``.
* ``FeedClient.sync`` checks each page's shape, maps it with ``case_source.map_bank_row`` and hands
  the batch and the page's ``next_cursor`` to ``alibi.api.ingest_bank_txns``, which commits them
  together. A refused batch leaves the cursor where it was and is audited. A page that is not a
  page, or a feed whose cursor does not advance while it reports more rows, is ``FeedError``
  before anything is written. ``FeedClient.refresh`` is the gateway's ``txn_refresh``.

Nothing here logs or returns a token; an HTTP failure is reported by status only, and a refusal
reason names identifiers, positions and fields, never row contents.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from alibi import api
from alibi.connectors.case_source import (
    FEED_CONNECTOR,
    FEED_VERSION,
    CaseClock,
    MappingError,
    map_bank_row,
    parse_time,
)
from alibi.connectors.http import Refusal
from alibi.core import canon
from alibi.core.ingest import IngestError
from alibi.core.schemas import AuditEvent, Ident, format_utc
from alibi.core.store import PutResult, Store, StoreError

__all__ = [
    "CURSOR_PREFIX", "DEFAULT_LIMIT", "MAX_LIMIT", "FeedClient", "FeedError", "FeedServer",
    "HttpFeed", "LocalFeed", "parse_cursor",
]

CURSOR_PREFIX = "c:"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
FEED_PATH = "/feed/transactions"
MAX_CURSOR_TEXT = 300          # the store's limit for a committed cursor
REASON_MAX = 300

_STATUSES = ("inserted", "duplicate", "corrected", "stale")
_CURSOR_DIGITS = re.compile(r"^[0-9]{1,12}$")      # ASCII digits only: int() and isdigit() differ
_IDENT = TypeAdapter(Ident)
_BAD_CURSOR = "cursor must be c:<n> as issued by the feed"


class FeedError(Exception):
    """The feed could not be read. The message names a status or a step, never a token."""


def _is_ident(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _IDENT.validate_python(value)
    except ValidationError:
        return False
    return True


def _clip(text: Any, limit: int = REASON_MAX) -> str:
    text = " ".join(str(text).split()) or "-"
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- server side


def parse_cursor(cursor: str | None) -> int:
    """The number of rows already served, from ``None`` (start) or ``c:<n>`` with ASCII digits.
    Anything else, including non-ASCII digits, is ``Refusal("invalid_query", ...)``."""
    if cursor is None or cursor == "":
        return 0
    if not isinstance(cursor, str) or not cursor.startswith(CURSOR_PREFIX):
        raise Refusal("invalid_query", _BAD_CURSOR)
    digits = cursor[len(CURSOR_PREFIX):]
    if not _CURSOR_DIGITS.match(digits):
        raise Refusal("invalid_query", _BAD_CURSOR)
    return int(digits)


def _check_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise Refusal("invalid_query", f"limit must be an integer from 1 to {MAX_LIMIT}")
    return limit


class FeedServer:
    """One account's read-only feed over a case clock: the pure functions behind the two
    ``/feed`` routes. ``account_ref`` and ``connection_id`` are echoed in every page.

    Built as ``FeedServer(clock, account_ref, connection_id)``; keywords work too.
    """

    def __init__(self, clock: CaseClock, account_ref: str, connection_id: str):
        self.clock = clock
        self.account_ref = account_ref
        self.connection_id = connection_id

    def _rows(self) -> list[dict[str, Any]]:
        return [d["row"] for d in self.clock.visible_bank_rows()]

    def page(self, cursor: str | None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """The visible rows in arrival order from ``cursor``, at most ``limit`` of them.

        ``next_cursor`` is ``c:<served so far>``; ``has_more`` says whether rows remain now.
        A malformed cursor, one past the rows this feed has ever served (never issued here),
        or a limit outside ``1..MAX_LIMIT`` is ``Refusal("invalid_query")``.
        """
        start = parse_cursor(cursor)
        limit = _check_limit(limit)
        rows = self._rows()
        if start > len(rows):
            raise Refusal("invalid_query", "cursor was not issued by this feed")
        chunk = rows[start:start + limit]
        served = start + len(chunk)
        return {
            "connection_id": self.connection_id,
            "account_ref": self.account_ref,
            "as_of": self.clock.as_of_text,
            "source_version": FEED_VERSION,
            "transactions": copy.deepcopy(chunk),
            "next_cursor": f"{CURSOR_PREFIX}{served}",
            "has_more": served < len(rows),
        }

    def latest(self, txn_id: str) -> dict[str, Any] | None:
        """The visible row for ``txn_id`` with the highest revision (the latest arrival among
        equal revisions), or ``None`` when the feed has no such row yet."""
        best: dict[str, Any] | None = None
        for row in self._rows():
            if row.get("txn_id") != txn_id:
                continue
            if best is None or _revision(row) >= _revision(best):
                best = row
        return copy.deepcopy(best) if best is not None else None


def _revision(row: dict[str, Any]) -> int:
    value = row.get("revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------- transports


class LocalFeed:
    """In-process transport over a ``FeedServer`` (no HTTP)."""

    def __init__(self, server: FeedServer):
        self.server = server

    def page(self, cursor: str | None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """One page, exactly as the route would return it."""
        return self.server.page(cursor, limit)

    def txn(self, txn_id: str) -> dict[str, Any] | None:
        """``{"as_of", "source_version", "transaction": row}`` for the latest visible row, or
        ``None``: the same shape as ``GET /feed/transactions/{txn_id}``."""
        row = self.server.latest(txn_id)
        if row is None:
            return None
        return {"as_of": self.server.clock.as_of_text, "source_version": FEED_VERSION,
                "transaction": row}


class HttpFeed:
    """HTTP transport to a running mock bank's feed routes with a bearer token.

    ``client`` is an ``httpx.Client`` or anything with ``.get(url, params=, headers=)`` whose
    answer has ``.status_code`` and ``.json()`` (FastAPI's ``TestClient`` qualifies). Without one,
    an ``httpx.Client`` is created for ``base_url``. A 404 from the txn route is ``None``; any
    other non-2xx answer, a 2xx body that is not JSON, or a JSON body that is not the route's
    shape raises ``FeedError`` naming the status or the step only.
    """

    def __init__(self, base_url: str, token: str, client: Any | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self._token = token
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client(base_url=self.base_url,
                                                                     timeout=10.0)

    def close(self) -> None:
        """Close the client this transport created (a passed-in client is left alone)."""
        if self._owns_client:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" if self.base_url else path

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.client.get(self._url(path), params=params or None, headers=self._headers())
        except httpx.HTTPError as exc:  # connection problems carry no token in their type name
            raise FeedError(f"feed request to {path} failed ({type(exc).__name__})") from None

    @staticmethod
    def _json(response: Any, what: str) -> Any:
        try:
            return response.json()
        except ValueError:   # json.JSONDecodeError is a ValueError
            raise FeedError(f"feed {what} response is not JSON") from None

    def page(self, cursor: str | None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """``GET /feed/transactions?cursor=&limit=``."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = self._get(FEED_PATH, params)
        if not 200 <= response.status_code < 300:
            raise FeedError(f"feed page request returned HTTP {response.status_code}")
        body = self._json(response, "page")
        if not isinstance(body, dict) or not isinstance(body.get("transactions"), list):
            raise FeedError("feed page response is not a page object")
        return body

    def txn(self, txn_id: str) -> dict[str, Any] | None:
        """``GET /feed/transactions/{txn_id}``: the route's body, or ``None`` on 404."""
        response = self._get(f"{FEED_PATH}/{txn_id}")
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise FeedError(f"feed txn request returned HTTP {response.status_code}")
        body = self._json(response, "txn")
        if not isinstance(body, dict) or not isinstance(body.get("transaction"), dict):
            raise FeedError("feed txn response is not a transaction object")
        return body


# --------------------------------------------------------------------------- passport-side client


def _check_page(page: Any, cursor: str | None) -> tuple[list[Any], str, datetime, bool]:
    """The page's rows, ``next_cursor``, ``as_of`` and ``has_more``, or ``FeedError`` when the
    answer is not a page or the feed reports more rows without moving its cursor."""
    if not isinstance(page, dict):
        raise FeedError("feed page is not an object")
    rows = page.get("transactions")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise FeedError("feed page transactions is not a list")
    next_cursor = page.get("next_cursor")
    if not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= MAX_CURSOR_TEXT:
        raise FeedError("feed page has no usable next_cursor")
    try:
        as_of = parse_time(page.get("as_of"), "as_of")
    except MappingError:
        raise FeedError("feed page has no usable as_of") from None
    has_more = bool(page.get("has_more"))
    if has_more and next_cursor == cursor:
        raise FeedError("the feed reports more rows but its cursor did not advance")
    return rows, next_cursor, as_of, has_more


class FeedClient:
    """Syncs one bank connection into the passport and answers the gateway's refresh.

    ``page_size`` is the page limit asked of the transport (tests use small pages to exercise
    the cursor; the default is the feed's default).
    """

    def __init__(self, transport: Any, store: Store, *, subject_ref: str, connection_id: str,
                 page_size: int = DEFAULT_LIMIT):
        self.transport = transport
        self.store = store
        self.subject_ref = subject_ref
        self.connection_id = connection_id
        self.page_size = page_size

    def cursor(self) -> str | None:
        """The cursor last committed with a batch for this connection (``None`` before any)."""
        return self.store.get_cursor(FEED_CONNECTOR, self.connection_id)

    def _audit_refusal(self, reason: str, at: datetime, fingerprint: Any) -> None:
        """One refused audit event for a page the connector would not hand to the core, so a
        refused feed page leaves the same trail as a batch the core refuses."""
        target = self.connection_id if _is_ident(self.connection_id) else None
        actor = FEED_CONNECTOR
        detail = _clip(f"refused: {reason}")
        audit_id = canon.make_id("aud", "ingest_bank_txns", actor, target, "refused", detail,
                                 format_utc(at), fingerprint)
        try:
            self.store.append_audit(AuditEvent(audit_id=audit_id, at=at, actor=actor,
                                               action="ingest_bank_txns", target_id=target,
                                               outcome="refused", detail=detail))
        except (StoreError, ValidationError):
            pass   # the refusal stands whether or not it could be recorded

    def _map(self, row: Any, index: int, page: dict[str, Any], next_cursor: str,
             as_of: datetime) -> dict[str, Any]:
        """One row as a ``BankTxn`` body. A row that does not fit is ``IngestError`` naming the
        txn id (when it is an identifier) or the row's position, the field and the code, never
        the row's contents."""
        txn_id = row.get("txn_id") if isinstance(row, dict) else None
        label = f"txn {txn_id}" if _is_ident(txn_id) else f"txn at position {index}"
        try:
            if not isinstance(row, dict):
                raise MappingError(f"{label} is not an object")
            if txn_id is not None and not _is_ident(txn_id):
                raise MappingError(f"{label} has a txn_id that is not an identifier")
            return map_bank_row(row, subject_ref=self.subject_ref, connection_id=self.connection_id,
                                retrieved_at=as_of,
                                source_version=page.get("source_version") or FEED_VERSION,
                                cursor=next_cursor).body
        except MappingError as exc:
            # The batch is refused before it reaches the store, so the cursor is untouched.
            text = str(exc).replace(f"bank row {txn_id}", label) if txn_id is not None else str(exc)
            raise IngestError(_clip(f"feed page {next_cursor} refused: {text}")) from None

    def sync(self, now: datetime | None = None) -> dict[str, Any]:
        """Read pages from the stored cursor until ``has_more`` is false, committing each page's
        rows and ``next_cursor`` together through ``alibi.api.ingest_bank_txns``.

        ``now`` is the passport clock for the audit trail; by default each page's ``as_of``.
        A page with no rows is not written. A page that is not a page, or one that reports more
        rows without advancing the cursor, raises ``FeedError`` before anything is written. A
        refused batch raises ``IngestError`` with the cursor still at the last committed page
        and a refused audit event. Returns ``{"pages", "inserted", "duplicate", "corrected",
        "stale", "cursor"}``.
        """
        counts = {status: 0 for status in _STATUSES}
        cursor = self.cursor()
        pages = 0
        while True:
            page = self.transport.page(cursor, self.page_size)
            pages += 1
            rows, next_cursor, as_of, has_more = _check_page(page, cursor)
            if rows:
                at = now if now is not None else as_of
                try:
                    bodies = [self._map(row, index, page, next_cursor, as_of)
                              for index, row in enumerate(rows)]
                except IngestError as exc:
                    self._audit_refusal(exc.reason, at, _fingerprint(rows))
                    raise
                results: list[PutResult] = api.ingest_bank_txns(
                    self.store, bodies, subject_ref=self.subject_ref,
                    connection_id=self.connection_id, cursor=next_cursor, now=at)
                for result in results:
                    counts[result.status] += 1
            if not has_more:
                break
            cursor = next_cursor
        return {"pages": pages, **counts, "cursor": self.cursor()}

    def refresh(self, txn_id: str) -> dict[str, Any] | None:
        """The feed's latest row for ``txn_id`` as ``BankTxn`` JSON from this connection, with
        ``retrieved_at`` = the answer's ``as_of``; ``None`` when the feed has no such row. This
        is the gateway's ``txn_refresh`` callable."""
        answer = self.transport.txn(txn_id)
        if answer is None:
            return None
        try:
            return map_bank_row(answer["transaction"], subject_ref=self.subject_ref,
                                connection_id=self.connection_id, retrieved_at=answer["as_of"],
                                source_version=answer.get("source_version") or FEED_VERSION).body
        except (KeyError, TypeError, MappingError) as exc:
            raise FeedError(f"feed txn {txn_id} could not be mapped ({type(exc).__name__})") from None


def _fingerprint(rows: list[Any]) -> str | None:
    try:
        return canon.digest(rows)
    except (canon.CanonError, RecursionError, TypeError, ValueError):
        return None
