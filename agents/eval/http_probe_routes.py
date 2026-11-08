"""Black-box HTTP probes for the Alibi isolation, RFC-intake and ingest boundaries.

This module is the HTTP runner for a slice of the catalogue in
``agents/execution/out/probes.json``: SP-17, SP-18, SP-19, SP-21, SP-22, SP-23,
SP-24, SP-33, SP-34, SP-35, SP-36, SP-37 and SP-39. Each handler drives the
simulated bank / merchant / console services over HTTP through the shared
``ProbeSession`` transport (``agents/eval/http_probe_runtime.py``) and returns one
outcome:

    BLOCKED      an observed passing check -- the backend refused a refusal probe,
                 or honoured a required sanitisation / scope control.
    NOT_BLOCKED  an observed security regression -- the attempt succeeded, or a
                 forged value was persisted / honoured.
    NOT_RUN      prerequisites or unambiguous evidence were missing. Raising
                 ``ProbeUnavailable`` (missing service, route, credential, fixture,
                 or a failed positive control) maps here too.

Two rules shape every handler:

* **A positive owner control precedes treating an opaque 404 as isolation.** A
  bare 404 is ambiguous: it can mean "not yours" or "no such route". So before a
  404 / 403 is read as isolation (SP-18, SP-19, SP-21), the resource's true owner
  (or the holder, for console routes) first reads it successfully. Only then is
  the attacker's refusal genuine isolation rather than a missing endpoint. A
  generic router 404 or a transport failure is therefore never scored as a refusal.
* **The read/ingest boundary is verified, not assumed.** For the ingest probes
  (SP-33..SP-37) the HTTP status is never trusted on its own: an active-state read
  -- itself gated by a positive control proving the read route is live -- decides
  whether the forged record became active. The stale-revision (SP-35) and
  self-supplied-tagging (SP-36) probes are decided *entirely* by the active record,
  because the backend may legitimately 2xx and keep history as long as the active
  state is correct.

Routes. Published S4 / FRONTEND form actions (bank inbox, package, follow-up
requests, passport, request detail) default to their documented path and may be
overridden via ``base_urls["routes"]``. Unpublished routes (bank/merchant intake,
imports, attachment / receipt / source / active-record reads) carry **no default**
and must be supplied explicitly; an unconfigured one yields NOT_RUN rather than a
guessed path.

Native shapes. Every configured template may use any placeholder whose fixture the
handler knows, not only the endpoint's own id, so the real backends can be
targeted without a compatibility shim:

* SP-19 on the S2 mock bank (``alibi/bank/service.py``, recipient from the token):
  ``attachment_download = "/packages/{package_id}/attachments"`` and
  ``receipt_download = "/packages/{package_id}"``. When an endpoint's own id is not
  in its path it is *package-bound*: the owner control must show that id (the
  Bank B attachment or receipt) in its JSON body, and both reads send
  ``Accept: application/json``. The mock bank has no PDF route, so leave
  ``package_pdf`` unset; that boundary stays NOT_RUN instead of being assumed.
* SP-21 on the S4 app (``alibi/app/main.py``): ``source_record =
  "/sources/{record_id}/{revision}"`` with ``source_record_revision``. The holder
  control must return that exact record revision as JSON. S4 renders HTML unless
  ``Accept`` starts with ``application/json``, so every SP-21 read sends it.
* SP-22..SP-24 on ``POST /rfc``: an optional ``<fixture>_control`` body that
  differs from the attack only in ``request_id`` and the probed fields is posted
  first as a positive control, against a loopback bank only (it issues a real
  request). Once that control is accepted, the native refusal for the probed field
  also counts (SP-22 ``invalid_request`` naming ``subject_ref``; SP-23
  ``scope_wider_than_parent``). Without a control only the documented codes count.

Config contract (all synthetic). ``base_urls`` holds service origins ``"bank"``,
``"merchant"`` and ``"app"`` (holder console), plus ``"routes"`` and ``"fixtures"``
dicts. ``tokens`` holds the ``"bank_a"``, ``"bank_b"``, ``"holder"``,
``"merchant"`` and ``"attacker"`` bearer tokens. Route and fixture keys are named
beside each handler below.
"""
from __future__ import annotations

from string import Formatter
from urllib.parse import urlsplit

from agents.eval.http_probe_runtime import (
    BLOCKED,
    NOT_BLOCKED,
    NOT_RUN,
    ProbeSession,
    ProbeUnavailable,
)

# Service keys into base_urls.
SVC_BANK = "bank"
SVC_MERCHANT = "merchant"
SVC_APP = "app"

# (route key, default). A None default means "unpublished": the route must be
# configured explicitly, else ProbeSession.route raises ProbeUnavailable -> NOT_RUN.
R_BANK_INBOX = ("bank_inbox", "/bank/{recipient}/inbox")
R_BANK_PACKAGE = ("bank_package", "/bank/{recipient}/packages/{package_id}")
R_FOLLOWUP = ("followup_requests", "/bank/{recipient}/requests")
R_PASSPORT = ("passport", "/passport")
R_REQUEST_DETAIL = ("request_detail", "/requests/{request_id}")

R_RFC_INTAKE = ("rfc_intake", None)
R_MERCHANT_EVENTS = ("merchant_events", None)
R_IMPORT = ("import_ingest", None)
R_ATTACHMENT = ("attachment_download", None)
R_PACKAGE_PDF = ("package_pdf", None)
R_RECEIPT = ("receipt_download", None)
R_SOURCE_RECORD = ("source_record", None)
R_ACTIVE_RECORD = ("active_record", None)

# Non-2xx statuses that count as a genuine refusal once a positive control has
# proved the route/resource exists. 400/409/422 are excluded: they signal a
# malformed request, not an authorisation boundary.
ISOLATION_REFUSALS = (401, 403, 404, 410)
NOT_FOUND = (404, 410)
JSON_HEADERS = {"Accept": "application/json"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_success(response) -> bool:
    return 200 <= response.status_code < 300


def _route(session: ProbeSession, spec, **values) -> str:
    key, default = spec
    return session.route(key, default, **values)


def _placeholders(session: ProbeSession, spec) -> set[str]:
    """Placeholder names in the configured (or default) template for ``spec``."""
    key, default = spec
    template = session.routes.get(key, default)
    if not isinstance(template, str):
        return set()
    try:
        return {name for _, name, _, _ in Formatter().parse(template) if name}
    except ValueError:
        return set()  # malformed template: ProbeSession.route reports it


def _loopback(session: ProbeSession, service: str) -> bool:
    origin = session.base_urls.get(service)
    try:
        return isinstance(origin, str) and urlsplit(origin).hostname in LOOPBACK
    except ValueError:
        return False


def _recipient(session: ProbeSession, actor: str) -> str:
    """Path segment for a recipient; defaults to the actor id, config may override."""
    value = session.fixtures.get(f"{actor}_recipient")
    return value if isinstance(value, str) and value else actor


def _codes(session: ProbeSession, tag: str, defaults):
    """Refusal codes that count as a correct refusal; `<tag>_codes` may override."""
    override = session.fixtures.get(f"{tag}_codes")
    return tuple(override) if isinstance(override, (list, tuple)) and override else tuple(defaults)


def _identity(session: ProbeSession, response):
    """Stable representation of a read record, ignoring configured volatile fields."""
    payload = ProbeSession.payload(response)
    if payload:
        drop = set(session.fixtures.get("volatile_read_fields", ()))
        return {k: v for k, v in payload.items() if k not in drop}
    return response.text


def _isolation_read(session: ProbeSession, path: str, owner: str, attacker: str, *,
                    marker: str | None = None, headers=None) -> str:
    """Owner positive control, then the cross-tenant GET.

    The owner's 2xx proves the resource exists in the owner's scope, so an opaque
    refusal to the attacker is genuine isolation and not a missing route. For a
    package-bound read (``marker`` set) the owner must also show that resource id.
    """
    control = session.http("GET", SVC_BANK, path, actor=owner, headers=headers)
    session.require_success(control)
    if marker is not None and marker not in control.text:
        raise ProbeUnavailable("owner control did not show the bound resource")
    attacker_resp = session.http("GET", SVC_BANK, path, actor=attacker, headers=headers)
    if _is_success(attacker_resp):
        return NOT_BLOCKED
    if attacker_resp.status_code in ISOLATION_REFUSALS:
        return BLOCKED
    return NOT_RUN


def _all_boundaries(results: list[str], missing: list[str]) -> str:
    if NOT_BLOCKED in results:
        return NOT_BLOCKED
    if missing:
        raise ProbeUnavailable("; ".join(missing))
    return BLOCKED if all(result == BLOCKED for result in results) else NOT_RUN


def _read_active(session: ProbeSession, record_id: str):
    """Read a record's active state via the configured read route.

    A positive control (``known_active_record_id``) must read back 2xx first, so a
    clean not-found on the target is genuine absence, not a missing read route.
    Returns the httpx response for the target read.
    """
    known = session.require("known_active_record_id")
    control = session.http("GET", SVC_APP, _route(session, R_ACTIVE_RECORD, record_id=known),
                           actor="holder")
    session.require_success(control)
    return session.http("GET", SVC_APP, _route(session, R_ACTIVE_RECORD, record_id=record_id),
                        actor="holder")


# --- Isolation -------------------------------------------------------------

def sp17(session: ProbeSession) -> str:
    """SP-17 bank_a lists bank_b's inbox: refused, or own scope with no bank_b IDs.

    Fixtures: ``bank_b_package_id`` (a package present only in bank_b's inbox).
    Routes: ``bank_inbox`` (published).
    """
    target = _recipient(session, "bank_b")
    marker = session.require("bank_b_package_id")
    path = _route(session, R_BANK_INBOX, recipient=target)

    owner = session.http("GET", SVC_BANK, path, actor="bank_b")
    session.require_success(owner)
    if marker not in owner.text:
        raise ProbeUnavailable("owner control: marker package absent from bank_b's own inbox")

    attacker = session.http("GET", SVC_BANK, path, actor="bank_a")
    if attacker.status_code in ISOLATION_REFUSALS:
        return BLOCKED
    if _is_success(attacker):
        if marker in attacker.text:
            return NOT_BLOCKED
        own_path = _route(session, R_BANK_INBOX, recipient=_recipient(session, "bank_a"))
        own_view = session.http("GET", SVC_BANK, own_path, actor="bank_a")
        session.require_success(own_view)
        return BLOCKED if _identity(session, own_view) == _identity(session, attacker) else NOT_RUN
    return NOT_RUN


def sp18(session: ProbeSession) -> str:
    """SP-18 bank_a fetches bank_b's package by id: refused, no existence leak.

    Fixtures: ``bank_b_package_id``, ``bank_b_absent_package_id`` (a
    package id bank_b does not hold) to check the refusal does not confirm existence.
    Routes: ``bank_package`` (published).
    """
    target = _recipient(session, "bank_b")
    pid = session.require("bank_b_package_id")
    absent = session.require("bank_b_absent_package_id")
    if absent == pid:
        raise ProbeUnavailable("known and absent package IDs must differ")
    path = _route(session, R_BANK_PACKAGE, recipient=target, package_id=pid)

    owner = session.http("GET", SVC_BANK, path, actor="bank_b")
    session.require_success(owner)
    if pid not in owner.text:
        raise ProbeUnavailable("owner control did not show the target package")
    attacker = session.http("GET", SVC_BANK, path, actor="bank_a")
    if _is_success(attacker):
        return NOT_BLOCKED
    if attacker.status_code not in ISOLATION_REFUSALS:
        return NOT_RUN

    decoy = session.http("GET", SVC_BANK,
                         _route(session, R_BANK_PACKAGE, recipient=target, package_id=absent),
                         actor="bank_a")
    if decoy.status_code not in ISOLATION_REFUSALS:
        return NOT_RUN
    observed = (attacker.status_code, attacker.text.replace(pid, "{package_id}"))
    unknown = (decoy.status_code, decoy.text.replace(absent, "{package_id}"))
    return BLOCKED if observed == unknown else NOT_BLOCKED


SP19_IDS = {"package_id": "bank_b_package_id", "attachment_id": "bank_b_attachment_id",
            "receipt_id": "bank_b_receipt_id"}
# (route, the endpoint's own id, whether a package-bound read is JSON).
SP19_ENDPOINTS = ((R_ATTACHMENT, "attachment_id", True), (R_PACKAGE_PDF, "package_id", False),
                  (R_RECEIPT, "receipt_id", True))


def sp19(session: ProbeSession) -> str:
    """SP-19 bank_a downloads a bank_b attachment, PDF or receipt: refused on every one.

    Fixtures: ``bank_b_attachment_id``, ``bank_b_package_id``, ``bank_b_receipt_id``.
    Routes (unpublished, explicit): ``attachment_download``, ``package_pdf`` and
    ``receipt_download``. Each may use ``{recipient}``, ``{package_id}``,
    ``{attachment_id}`` and ``{receipt_id}``; an endpoint always needs its own id
    fixture. Without its own id in the path the read is package-bound (e.g. the
    mock bank's ``/packages/{package_id}/attachments``) and the owner control must
    show that id. A missing endpoint stays NOT_RUN; a leak on any endpoint fails.
    """
    target = _recipient(session, "bank_b")
    results, missing = [], []
    for spec, own, json_read in SP19_ENDPOINTS:
        try:
            values = {"recipient": target, own: session.require(SP19_IDS[own])}
            names = _placeholders(session, spec)
            for name in sorted(names & SP19_IDS.keys() - values.keys()):
                values[name] = session.require(SP19_IDS[name])
            path = _route(session, spec, **values)
            bound = own not in names
            results.append(_isolation_read(
                session, path, owner="bank_b", attacker="bank_a",
                marker=str(values[own]) if bound else None,
                headers=JSON_HEADERS if bound and json_read else None))
        except ProbeUnavailable as exc:
            missing.append(str(exc))
    return _all_boundaries(results, missing)


def _holder_source_control(control, record_id, revision) -> None:
    """The native source read must return exactly the requested record revision."""
    record = ProbeSession.payload(control).get("record")
    if (not isinstance(record, dict) or record.get("record_id") != record_id
            or str(record.get("revision")) != str(revision)):
        raise ProbeUnavailable("holder control did not return the requested source revision")


def sp21(session: ProbeSession) -> str:
    """SP-21 a bank credential calls a holder-only endpoint directly: refused.

    The holder is the positive control: it reaches each endpoint (route exists),
    then bank_b must be refused. Every read sends ``Accept: application/json``
    (S4 renders HTML otherwise). Fixtures: ``holder_request_id`` (holder review),
    ``source_record_id``, and ``source_record_revision`` when the source template
    has ``{revision}`` (S4's ``/sources/{record_id}/{revision}``; the holder must
    then read back that exact revision). Routes: ``passport``, ``request_detail``
    (published), ``source_record`` (unpublished).
    """
    results, missing = [], []
    probes = (
        (R_PASSPORT, {}),
        (R_REQUEST_DETAIL, {"request_id": "holder_request_id"}),
        (R_SOURCE_RECORD, {"record_id": "source_record_id", "revision": "source_record_revision"}),
    )
    for spec, fixtures in probes:
        try:
            names = _placeholders(session, spec)
            values = {param: session.require(fixture) for param, fixture in fixtures.items()
                      if param != "revision" or param in names}
            path = _route(session, spec, **values)
            control = session.http("GET", SVC_APP, path, actor="holder", headers=JSON_HEADERS)
            session.require_success(control)  # holder reaches it -> route exists
            if "revision" in values:
                _holder_source_control(control, values["record_id"], values["revision"])
            attacker = session.http("GET", SVC_APP, path, actor="bank_b", headers=JSON_HEADERS)
            if _is_success(attacker):
                results.append(NOT_BLOCKED)
            elif attacker.status_code in ISOLATION_REFUSALS:
                results.append(BLOCKED)
            else:
                results.append(NOT_RUN)
        except ProbeUnavailable as exc:
            missing.append(str(exc))
    return _all_boundaries(results, missing)


# --- RFC intake ------------------------------------------------------------

def _rfc_fields(body: dict) -> dict:
    """The request object: the mock bank wraps it as ``{"request": {...}}``."""
    inner = body.get("request")
    return inner if isinstance(inner, dict) else body


def _rfc_control(session: ProbeSession, path: str, actor: str, control, attack,
                 varied: tuple[str, ...]) -> None:
    """Post a differential positive control and require the intake to issue it.

    The control may differ from the attack only in ``request_id`` and ``varied``, so
    a later refusal of the attack is attributable to the probed field. It issues a
    real request, so it runs only against a disposable loopback bank.
    """
    if not _loopback(session, SVC_BANK):
        raise ProbeUnavailable("RFC intake controls need a disposable loopback bank")
    if not isinstance(control, dict) or not isinstance(attack, dict):
        raise ProbeUnavailable("RFC intake control and attack must be JSON objects")
    fields, probe = _rfc_fields(control), _rfc_fields(attack)
    ignored = {"request_id", *varied}

    def rest(wrapper: dict, inner: dict):
        outer = {k: v for k, v in wrapper.items() if k != "request"} if inner is not wrapper else None
        return outer, {k: v for k, v in inner.items() if k not in ignored}

    if rest(control, fields) != rest(attack, probe):
        raise ProbeUnavailable(
            f"RFC control must differ from the attack only in request_id and {', '.join(varied)}")
    control_id, attack_id = fields.get("request_id"), probe.get("request_id")
    if not (isinstance(control_id, str) and control_id and isinstance(attack_id, str)
            and attack_id and control_id != attack_id):
        raise ProbeUnavailable("RFC control and attack need distinct request IDs")
    if all(fields.get(name) == probe.get(name) for name in varied):
        raise ProbeUnavailable(f"RFC control does not change {', '.join(varied)}")
    resp = session.http("POST", SVC_BANK, path, actor=actor, json=control)
    session.require_success(resp)
    if ProbeSession.payload(resp).get("request_id") != control_id:
        raise ProbeUnavailable("RFC intake control did not issue the control request")


def _native_refusal(response, refusals) -> bool:
    """A coded refusal from the native intake, optionally naming the probed field."""
    if response.status_code not in (400, 403, 409, 422):
        return False
    code = ProbeSession.code(response)
    reason = ProbeSession.payload(response).get("reason")
    return any(code == want and (fragment is None or (isinstance(reason, str) and fragment in reason))
               for want, fragment in refusals)


def _rfc_probe(session: ProbeSession, tag: str, fixture: str, actor: str, documented,
               varied: tuple[str, ...], native=()) -> str:
    """POST an RFC intake attack. With ``<fixture>_control`` set, a differential
    positive control runs first and the native refusals in ``native`` also count."""
    body = session.require(fixture)
    path = _route(session, R_RFC_INTAKE)
    control = session.fixtures.get(f"{fixture}_control")
    if control is not None:
        _rfc_control(session, path, actor, control, body, varied)
    resp = session.http("POST", SVC_BANK, path, actor=actor, json=body)
    outcome = ProbeSession.denial(resp, _codes(session, tag, documented))
    if outcome == NOT_RUN and control is not None and _native_refusal(resp, native):
        return BLOCKED
    return outcome


def sp22(session: ProbeSession) -> str:
    """SP-22 bank_a RFC for a subject not linked to it: rejected with foreign_subject.

    Fixtures: ``rfc_foreign_subject`` (RFC body), optional
    ``rfc_foreign_subject_control`` (same body, own subject, new request_id). With
    the control accepted, the mock bank's ``invalid_request`` naming ``subject_ref``
    also counts. Routes: ``rfc_intake`` (unpublished; mock bank ``/rfc``).
    """
    return _rfc_probe(session, "sp22", "rfc_foreign_subject", "bank_a", ("foreign_subject",),
                      ("subject_ref",), native=(("invalid_request", "subject_ref"),))


def sp23(session: ProbeSession) -> str:
    """SP-23 bank_a RFC listing txn_ids outside its feed: out-of-scope refs rejected.

    The fixture lists only foreign txn_ids, so a 2xx acceptance is unambiguous.
    Fixtures: ``rfc_out_of_scope_txns`` (RFC body), optional
    ``rfc_out_of_scope_txns_control`` (same body with in-scope txn_ids or period and
    a new request_id). With the control accepted, the mock bank's
    ``scope_wider_than_parent`` also counts. Routes: ``rfc_intake``.
    """
    return _rfc_probe(session, "sp23", "rfc_out_of_scope_txns", "bank_a",
                      ("txn_out_of_scope", "scope_rejected", "out_of_scope"), ("txn_ids", "period"),
                      native=(("scope_wider_than_parent", None),))


def sp24(session: ProbeSession) -> str:
    """SP-24 bank_b RFC claiming issuer/audience bank_a: rejected with issuer_mismatch.

    A server that silently rewrites issuer/audience and accepts still fails: the
    probe requires rejection. Fixtures: ``rfc_claims_bank_a`` (RFC body), optional
    ``rfc_claims_bank_a_control`` (same body without the spoof, new request_id).
    Routes: ``rfc_intake``.
    """
    return _rfc_probe(session, "sp24", "rfc_claims_bank_a", "bank_b", ("issuer_mismatch",),
                      ("issuer", "audience"))


# --- Ingest ----------------------------------------------------------------

def _ingest(session: ProbeSession, actor: str, body) -> "object":
    return session.http("POST", SVC_MERCHANT, _route(session, R_MERCHANT_EVENTS), actor=actor,
                        json=body)


def sp33(session: ProbeSession) -> str:
    """SP-33 webhook claims issuer provenance with a bad/missing signature: nothing ingested.

    Decided by the active-state read first, then the HTTP refusal code. Fixtures:
    ``event_bad_signature`` (event body), ``event_bad_signature_record_id`` (a
    record id not already active), ``known_active_record_id``. Routes:
    ``merchant_events``, ``active_record`` (both unpublished).
    """
    body = session.require("event_bad_signature")
    record_id = session.require("event_bad_signature_record_id")
    resp = _ingest(session, "attacker", body)
    read = _read_active(session, record_id)
    if _is_success(read):
        return NOT_BLOCKED  # the forged record became active
    if read.status_code not in NOT_FOUND:
        raise ProbeUnavailable("active-state read inconclusive after ingest attempt")
    # Record absent: a coded signature refusal passes; a 2xx accept is a regression.
    return ProbeSession.denial(
        resp, _codes(session, "sp33", ("signature_invalid", "bad_signature", "signature_missing")))


def sp34(session: ProbeSession) -> str:
    """SP-34 replay identical bytes, then different bytes, and check active state.

    Fixtures: ``event_original`` (valid event), ``event_same_id_conflicting`` (same
    event_id, different bytes), ``event_record_id``, ``known_active_record_id``.
    Routes: ``merchant_events``, ``active_record``.
    """
    record_id = session.require("event_record_id")
    original = session.require("event_original")
    conflicting = session.require("event_same_id_conflicting")

    session.require_success(_ingest(session, "merchant", original))
    before = _read_active(session, record_id)
    if not _is_success(before):
        raise ProbeUnavailable("original event is not active; cannot test reuse")
    baseline = _identity(session, before)

    retry = _ingest(session, "merchant", original)
    session.require_success(retry)
    after_retry = _read_active(session, record_id)
    session.require_success(after_retry)
    if _identity(session, after_retry) != baseline:
        return NOT_BLOCKED

    resp = _ingest(session, "merchant", conflicting)
    reuse = ProbeSession.denial(resp, _codes(session, "sp34", ("event_id_reused",)))
    after = _read_active(session, record_id)
    if not _is_success(after):
        raise ProbeUnavailable("active-state unreadable after reuse attempt")
    changed = _identity(session, after) != baseline

    if reuse == NOT_BLOCKED or changed:
        return NOT_BLOCKED  # conflicting bytes accepted or overwrote the active record
    if reuse == BLOCKED:
        return BLOCKED      # coded reuse refusal and the active record is unchanged
    return NOT_RUN


def sp35(session: ProbeSession) -> str:
    """SP-35 revision 1 after revision 2 is active: stale revision never becomes active.

    Decided entirely by the active revision (storing stale history is allowed).
    Fixtures: ``event_revision_new`` (rev 2 event), ``event_revision_stale`` (rev 1,
    same record), ``event_record_id_rev``, ``expected_active_revision`` (e.g. 2),
    optional ``revision_field`` (default "revision"), ``known_active_record_id``.
    Routes: ``merchant_events``, ``active_record``.
    """
    record_id = session.require("event_record_id_rev")
    newer = session.require("event_revision_new")
    stale = session.require("event_revision_stale")
    expected = session.require("expected_active_revision")
    field = session.fixtures.get("revision_field", "revision")

    _ingest(session, "merchant", newer)  # precondition: the newer revision is active
    before = _read_active(session, record_id)
    if not _is_success(before) or ProbeSession.payload(before).get(field) != expected:
        raise ProbeUnavailable("newer revision is not active; cannot test staleness")

    _ingest(session, "merchant", stale)
    after = _read_active(session, record_id)
    if not _is_success(after):
        raise ProbeUnavailable("active-state unreadable after stale attempt")
    active_revision = ProbeSession.payload(after).get(field)
    if active_revision == expected:
        return BLOCKED        # stale stored as history at most; active stays newer
    if active_revision is None:
        return NOT_RUN
    return NOT_BLOCKED        # active downgraded to the stale revision


def sp36(session: ProbeSession) -> str:
    """SP-36 self-supplied import claims trust issuer_verified: tagged self_supplied.

    The import is accepted; the persisted record must read back at self_supplied.
    Fixtures: ``import_claims_issuer_verified`` (import body), ``import_record_id``,
    optional ``trust_field`` (default "trust"), ``known_active_record_id``. Routes:
    ``import_ingest`` (unpublished), ``active_record``.
    """
    body = session.require("import_claims_issuer_verified")
    record_id = session.require("import_record_id")
    field = session.fixtures.get("trust_field", "trust")

    resp = session.http("POST", SVC_APP, _route(session, R_IMPORT), actor="holder", json=body)
    session.require_success(resp)  # imports are accepted; this is a sanitisation probe
    read = _read_active(session, record_id)
    if not _is_success(read):
        raise ProbeUnavailable("imported record is not readable")
    trust = ProbeSession.payload(read).get(field)
    if trust == "self_supplied":
        return BLOCKED
    if trust in ("issuer_verified", "bank_observed"):
        return NOT_BLOCKED   # forged elevated trust honoured
    return NOT_RUN


def sp37(session: ProbeSession) -> str:
    """SP-37 webhook for a subject the connector key is not scoped to: rejected, nothing ingested.

    Fixtures: ``event_foreign_subject`` (event body), ``event_foreign_subject_record_id``
    (not already active), ``known_active_record_id``. Routes: ``merchant_events``,
    ``active_record``.
    """
    body = session.require("event_foreign_subject")
    record_id = session.require("event_foreign_subject_record_id")
    resp = _ingest(session, "attacker", body)
    read = _read_active(session, record_id)
    if _is_success(read):
        return NOT_BLOCKED
    if read.status_code not in NOT_FOUND:
        raise ProbeUnavailable("active-state read inconclusive after ingest attempt")
    return ProbeSession.denial(resp, _codes(session, "sp37", ("subject_out_of_scope",)))


# --- Injection / structural scope -----------------------------------------

def _same_scope(txn_ids, period, parent_txn_ids, parent_period) -> bool:
    def norm(value):
        return sorted(value) if isinstance(value, list) else value

    return norm(txn_ids) == norm(parent_txn_ids) and period == parent_period


def sp39(session: ProbeSession) -> str:
    """SP-39 RFC question text asking for 'all accounts': the response stays in scope.

    A follow-up whose free text asks beyond scope must inherit the parent's
    structural scope, not widen it. Fixtures: ``rfc_followup_wide_text`` (follow-up
    body with injection text and empty scope fields), ``parent_txn_ids``, optional
    ``parent_period`` (default None), ``followup_forbidden_markers`` (nonempty list
    of out-of-scope or policy-denied values), ``followup_allowed_marker`` (known
    in-scope evidence). Routes: ``followup_requests``
    (published), ``followup_package`` (optional, keyed by the new request_id).
    """
    target = _recipient(session, "bank_a")
    body = session.require("rfc_followup_wide_text")
    parent_txn_ids = session.require("parent_txn_ids")
    parent_period = session.fixtures.get("parent_period")

    path = _route(session, R_FOLLOWUP, recipient=target)
    resp = session.http("POST", SVC_BANK, path, actor="bank_a", data=body)
    if not _is_success(resp):
        # A coded scope rejection also enforces the boundary structurally.
        refused = ProbeSession.denial(
            resp, _codes(session, "sp39", ("scope_wider_than_parent", "scope_rejected", "out_of_scope")))
        return BLOCKED if refused == BLOCKED else NOT_RUN

    payload = ProbeSession.payload(resp)
    txn_ids, period = payload.get("txn_ids"), payload.get("period")
    if txn_ids is None and period is None:
        return NOT_RUN  # created scope is not observable in the response
    if not _same_scope(txn_ids, period, parent_txn_ids, parent_period):
        return NOT_BLOCKED

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ProbeUnavailable("follow-up intake did not return the new request ID")
    package_path = session.route("followup_package", request_id=request_id)
    forbidden = session.require("followup_forbidden_markers")
    allowed = session.require("followup_allowed_marker")
    if not isinstance(forbidden, list) or not forbidden or not all(
        isinstance(marker, str) and marker for marker in forbidden
    ):
        raise ProbeUnavailable("followup_forbidden_markers must contain nonempty strings")
    package = session.http("GET", SVC_BANK, package_path, actor="bank_a")
    session.require_success(package)
    if any(marker in package.text for marker in forbidden):
        return NOT_BLOCKED
    published = session.payload(package)
    scope = published.get("scope")
    if (published.get("request_id") != request_id or not isinstance(scope, dict)
            or not isinstance(allowed, str) or not allowed or allowed not in package.text):
        raise ProbeUnavailable("follow-up package scope and in-scope evidence were not observable")
    return BLOCKED if _same_scope(
        scope.get("txn_ids"), scope.get("period"), parent_txn_ids, parent_period
    ) else NOT_BLOCKED


HANDLERS = {
    "SP-17": sp17,
    "SP-18": sp18,
    "SP-19": sp19,
    "SP-21": sp21,
    "SP-22": sp22,
    "SP-23": sp23,
    "SP-24": sp24,
    "SP-33": sp33,
    "SP-34": sp34,
    "SP-35": sp35,
    "SP-36": sp36,
    "SP-37": sp37,
    "SP-39": sp39,
}
