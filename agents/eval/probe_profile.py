"""Generate a private run_probes profile from a disposable loopback Alibi stack.

Reads two already-dispatched public Case 1 receipts from the native mock bank
(``alibi/bank/service.py``: ``/``, ``/inbox``, ``/packages/{id}``, ``/packages/{id}/raw``,
``/rfc/{id}``) and the holder's request from the S4 app (``alibi/app/main.py``: ``/health``,
``/requests/{id}``, ``/sources/{id}/{revision}``). After checking them it writes a
run_probes config that adds native routes and fixtures for SP-17, SP-18 and SP-20..SP-32,
each under that probe's own ``probes`` entry. SP-22..SP-24 use public RFC inputs rather
than receipts. Run them with the bank's clock inside the Case 1 RFC window.

    uv run --frozen python -m agents.eval.probe_profile --config data/alibi/probe-base.json \\
        --out data/alibi/live-probes.json

Input: a run_probes config with loopback ``base_urls.bank`` (native bank) and
``base_urls.app`` (S4 holder app) and synthetic ``tokens`` for ``bank_a``, ``bank_b`` and
``holder``. Credentials come only from that file. They are copied into the profile and are
never printed. The profile is written owner-only (0600), outside Git or at a git-ignored path.

Selection. The bank_a and bank_b receipts are the packages named by ``--bank-a-package`` /
``--bank-b-package`` (or an explicit ``native_verifier_package_id`` / ``bank_b_package_id``
in ``base_urls.fixtures``). Without an explicit ID, the inbox must hold exactly one
verified receipt for a Case 1 RFC of that recipient. Each receipt must agree with its
signed envelope and its signed RFC, and both signatures must verify against the public
``trust_registry.json`` in ``--key-dir``. The PEM paths for SP-25..SP-32 come from the
package's key ID and its first full issuer-signed record. The PEM files are checked with
``lstat`` only and are never opened. The holder request defaults to the bank_a receipt's
request. The SP-21 source is the first record it cites that reads back at its revision.

Explicit configuration wins: a route or fixture already set in ``base_urls`` or in the
probe's own ``base_urls`` is kept, and a probe with its own origin or token override is left
untouched. The report on stdout names the ready, generated, explicit and missing keys per
probe, with no credential or fixture values. Exit status 0 means every target probe is ready.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlsplit

from agents.eval.http_probe_runtime import ProbeSession, ProbeUnavailable
from agents.eval.run_probes import build_probe_harness
from alibi.connectors.case_source import load_case_without_gold
from alibi.core import canon
from alibi.core.package import PackageError, package_signing_payload
from alibi.core.schemas import SigningDomain
from alibi.core.signing import DEFAULT_KEY_DIR, REGISTRY_FILE, SignatureError, TrustRegistry, verify

CASE = 1
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
JSON_HEADERS = {"Accept": "application/json"}
ACTORS = ("bank_a", "bank_b", "holder")
SAFE_KID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
VERIFIER = tuple(f"SP-{n}" for n in range(25, 33))
VERIFIER_FIXTURES = ("native_verifier_package_id", "native_verifier_package_pem",
                     "native_verifier_record_pem")
# probe -> (source section, actors it uses, route keys, fixture keys)
NEEDS = {
    "SP-17": ("bank_b", ("bank_a", "bank_b"), ("bank_inbox",), ("bank_b_package_id",)),
    "SP-18": ("bank_b", ("bank_a", "bank_b"), ("bank_package",),
              ("bank_b_package_id", "bank_b_absent_package_id")),
    "SP-20": ("bank_b", ("bank_a", "bank_b"), (),
              ("consent_bank_service", "recipient_bank_a", "recipient_bank_b", "bank_b_package_id")),
    "SP-21": ("holder", ("holder", "bank_b"), ("source_record",),
              ("holder_request_id", "source_record_id", "source_record_revision")),
    "SP-22": ("rfc_intake", ("bank_a",), ("rfc_intake",),
              ("rfc_foreign_subject", "sp22_codes")),
    "SP-23": ("rfc_intake", ("bank_a",), ("rfc_intake",),
              ("rfc_out_of_scope_txns", "sp23_codes")),
    "SP-24": ("rfc_intake", ("bank_b",), ("rfc_intake",),
              ("rfc_claims_bank_a", "rfc_claims_bank_a_control")),
    **{pid: ("bank_a", ("bank_a", "bank_b"), (), VERIFIER_FIXTURES) for pid in VERIFIER},
}


class ProfileError(Exception):
    """A lookup or check failed; the message names ids and fields only."""


def _q(value: str) -> str:
    return quote(value, safe="")


def _loopback(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and host in LOOPBACK and parts.username is None
            and parts.password is None and parts.path in ("", "/") and not parts.query
            and not parts.fragment)


class Stack:
    """Read-only JSON lookups against loopback origins only."""

    def __init__(self, config: dict, key_dir: Path):
        base, tokens = config.get("base_urls", {}), config.get("tokens", {})
        origins = {}
        for service in ("bank", "app"):
            if service in base:
                if not _loopback(base[service]):
                    raise ProfileError(f"base_urls.{service} must be a loopback origin")
                origins[service] = base[service]
        if "bank" not in origins:
            raise ProfileError("base_urls.bank must name the disposable loopback bank")
        self.session = ProbeSession(origins, {a: tokens[a] for a in ACTORS if a in tokens})
        try:
            self.registry = TrustRegistry.load(key_dir / REGISTRY_FILE)
            self.registry_error = None
        except (OSError, ValueError, SignatureError):
            self.registry, self.registry_error = None, "public trust registry is missing or invalid"
        self.key_dir = key_dir

    def get(self, service: str, path: str, actor: str | None) -> dict:
        try:
            response = self.session.http("GET", service, path, actor, headers=JSON_HEADERS)
            self.session.require_success(response)
        except ProbeUnavailable as exc:
            raise ProfileError(f"GET {service} {path}: {exc}") from None
        payload = ProbeSession.payload(response)
        if not payload:
            raise ProfileError(f"GET {service} {path} did not return a JSON object")
        return payload

    def verify(self, payload, envelope, domain: SigningDomain) -> str:
        if self.registry is None:
            raise ProfileError(self.registry_error)
        try:
            return verify(payload, envelope, self.registry, domain=domain).kid
        except (SignatureError, PackageError, TypeError, ValueError):
            raise ProfileError(f"{domain.value} signature does not verify against the public "
                               "registry") from None


def _receipt(stack: Stack, case: dict, recipient: str, explicit: str | None) -> dict:
    """One verified, signed, RFC-bound Case 1 receipt in ``recipient``'s inbox."""
    rfcs = {r["request_id"]: r for r in case["rfcs"] if r.get("audience") == recipient}
    inbox = stack.get("bank", "/inbox", recipient)
    listed = [row for row in inbox.get("packages") or [] if isinstance(row, dict)]
    rows = [row for row in listed
            if row.get("status") == "verified" and row.get("request_id") in rfcs]
    if explicit is not None:
        rows = [row for row in rows if row.get("package_id") == explicit]
        if not rows:
            raise ProfileError(f"{recipient}: the explicit package is not a verified Case 1 receipt")
    elif len(rows) != 1:
        flag = f"--{recipient.replace('_', '-')}-package"
        raise ProfileError(f"{recipient}: {len(rows)} verified Case 1 receipts; pass {flag}")
    row = rows[0]
    pid, rid = row["package_id"], row["request_id"]
    if not isinstance(pid, str) or not pid:
        raise ProfileError(f"{recipient}: inbox row has no package ID")
    view = stack.get("bank", f"/packages/{_q(pid)}", recipient)
    raw = stack.get("bank", f"/packages/{_q(pid)}/raw", recipient)
    signed_rfc = stack.get("bank", f"/rfc/{_q(rid)}", recipient).get("rfc")
    receipt = view.get("receipt") if isinstance(view.get("receipt"), dict) else {}
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    try:
        digests = canon.digest(raw), canon.digest(body)
    except (canon.CanonError, RecursionError):
        raise ProfileError(f"{recipient}: signed package cannot be canonicalised") from None
    if not (view.get("status") == "verified"
            and (view.get("authentication") or {}).get("valid") is True
            and receipt.get("accepted") is True and receipt.get("package_id") == pid
            and receipt.get("recipient") == recipient
            and receipt.get("receipt_id") == row.get("receipt_id")
            and receipt.get("package_sha256") == digests[0] == row.get("package_sha256")
            and raw.get("package_id") == pid and raw.get("body_sha256") == digests[1]
            and body.get("audience") == recipient and body.get("request_id") == rid):
        raise ProfileError(f"{recipient}: receipt, inbox row and signed package do not agree")
    request = signed_rfc.get("request") if isinstance(signed_rfc, dict) else None
    if not (isinstance(request, dict) and request.get("request_id") == rid
            and request.get("issuer") == recipient and request.get("audience") == recipient
            and request.get("subject_ref") == rfcs[rid].get("subject_ref")):
        raise ProfileError(f"{recipient}: signed RFC does not match Case 1 request {rid}")
    try:
        signed_part = package_signing_payload(raw)
    except PackageError:
        raise ProfileError(f"{recipient}: signed package is missing signed fields") from None
    stack.verify(signed_part, raw.get("signature"), SigningDomain.PACKAGE)
    stack.verify(request, signed_rfc.get("signature"), SigningDomain.REQUEST)
    return {"package_id": pid, "request_id": rid, "raw": raw,
            "listed": {r.get("package_id") for r in listed}}


def _pem(key_dir: Path, kid) -> str:
    """The derived ``<kid>.pem`` path, checked with lstat only (never opened)."""
    if not isinstance(kid, str) or not SAFE_KID.fullmatch(kid):
        raise ProfileError("signing key ID is not a safe file name")
    path = (key_dir / f"{kid}.pem").absolute()
    try:
        info = os.lstat(path)
    except OSError:
        raise ProfileError(f"key file for {kid} is missing from the key directory") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProfileError(f"key file for {kid} must be an owner-only regular file")
    return str(path)


def _verifier(stack: Stack, a: dict) -> dict:
    """SP-25..SP-32 fixtures for the bank_a receipt, matching the native verifier's choice."""
    raw = a["raw"]
    full = next((att for att in raw["body"].get("attachments") or []
                 if isinstance(att, dict) and att.get("form") == "full_record"
                 and att.get("issuer_signature")), None)
    if full is None:
        raise ProfileError("bank_a: package has no full issuer-signed record")
    record_kid = stack.verify(full.get("content"), full["issuer_signature"], SigningDomain.RECORD)
    package_kid = (raw.get("signature") or {}).get("kid")
    return {"native_verifier_package_id": a["package_id"],
            "native_verifier_package_pem": _pem(stack.key_dir, package_kid),
            "native_verifier_record_pem": _pem(stack.key_dir, record_kid)}


def _records(obj):
    """(record_id, revision) pairs a holder view cites, in order."""
    if isinstance(obj, dict):
        rid, rev = obj.get("record_id"), obj.get("revision")
        href = obj.get("href")
        if (isinstance(rid, str) and rid and type(rev) is int and obj.get("ref_type") != "txn"
                and (href is None or str(href).startswith("/sources/"))):
            yield rid, rev
        for value in obj.values():
            yield from _records(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _records(value)


def _holder(stack: Stack, case: dict, request_id: str | None) -> dict:
    """SP-21 fixtures: a Case 1 holder request and one source revision it cites."""
    if not request_id:
        raise ProfileError("holder: no request ID; pass --holder-request")
    if request_id not in {r["request_id"] for r in case["rfcs"]}:
        raise ProfileError("holder: the request is not a Case 1 RFC")
    if stack.get("app", "/health", None).get("case") != CASE:
        raise ProfileError("holder: the app is not serving Case 1")
    detail = stack.get("app", f"/requests/{_q(request_id)}", "holder")
    if (detail.get("request") or {}).get("request_id") != request_id:
        raise ProfileError("holder: request detail did not return the request")
    for record_id, revision in dict.fromkeys(_records(detail)):
        try:
            source = stack.get("app", f"/sources/{_q(record_id)}/{revision}", "holder")
        except ProfileError:
            continue
        record = source.get("record") if isinstance(source.get("record"), dict) else {}
        if record.get("record_id") == record_id and record.get("revision") == revision:
            return {"holder_request_id": request_id, "source_record_id": record_id,
                    "source_record_revision": revision}
    raise ProfileError("holder: no cited source record read back at its revision")


def _explicit(config: dict, key: str, cli: str | None) -> str | None:
    value = config.get("base_urls", {}).get("fixtures", {}).get(key)
    if value is not None and cli is not None and value != cli:
        raise ProfileError(f"the command-line selection conflicts with base_urls.fixtures.{key}")
    return value if value is not None else cli


def _section(errors: dict, name: str, build):
    try:
        return build()
    except ProfileError as exc:
        errors[name] = str(exc)
        return None


def _plan(b: dict | None, a: dict | None, h: dict | None) -> dict:
    """Generated routes and fixtures per probe, only from verified sections."""
    plan = {}
    if b is not None:
        pid = b["package_id"]
        absent = "pkg_absent_" + hashlib.sha256(pid.encode("utf-8")).hexdigest()[:24]
        if absent in b["listed"]:
            absent = None
        plan["SP-17"] = {"routes": {"bank_inbox": "/inbox"}, "fixtures": {"bank_b_package_id": pid}}
        plan["SP-18"] = {"routes": {"bank_package": "/packages/{package_id}"},
                         "fixtures": {"bank_b_package_id": pid,
                                      **({"bank_b_absent_package_id": absent} if absent else {})}}
        plan["SP-20"] = {"routes": {}, "fixtures": {
            "consent_bank_service": "bank", "recipient_bank_a": "bank_a",
            "recipient_bank_b": "bank_b", "bank_b_package_id": pid}}
    if a is not None:
        for probe in VERIFIER:
            plan[probe] = {"routes": {}, "fixtures": dict(a)}
    if h is not None:
        plan["SP-21"] = {"routes": {"source_record": "/sources/{record_id}/{revision}"},
                         "fixtures": dict(h)}
    return plan


def _rfc_intake_plan(case: dict) -> dict:
    """Build native Case 1 attack bodies using only public inputs."""
    rfc_a, rfc_b = (
        next((rfc for rfc in case["rfcs"]
              if rfc["audience"] == recipient and not rfc.get("parent_request_id")), None)
        for recipient in ("bank_a", "bank_b")
    )
    if not rfc_a or not rfc_b or not rfc_a.get("txn_ids"):
        raise ProfileError("public Case 1 needs a transaction RFC for bank_a and an RFC for bank_b")
    own_txns = {delivery["row"].get("txn_id") for delivery in case["bank_deliveries"]}
    foreign_subject = foreign_txn = None
    for number in range(2, 81):
        try:
            other = load_case_without_gold(number)
        except KeyError:
            continue
        if other["subject_ref"] != case["subject_ref"]:
            for delivery in other["bank_deliveries"]:
                txn_id = delivery["row"].get("txn_id")
                if txn_id and txn_id not in own_txns:
                    foreign_subject, foreign_txn = other["subject_ref"], txn_id
                    break
        if foreign_txn:
            break
    if foreign_subject is None or foreign_txn is None:
        raise ProfileError("public RFC inputs do not supply a foreign subject and transaction")
    tag = secrets.token_hex(6)

    def request(rfc: dict, label: str, **changes) -> dict:
        fields = {key: rfc[key] for key in ("subject_ref", "questions", "issued_at", "expires_at")}
        scope = "txn_ids" if rfc.get("txn_ids") else "period"
        return {"request": {**fields, scope: rfc[scope], "request_id": f"probe-{label}-{tag}", **changes}}

    fixtures = {
        "SP-22": {"rfc_foreign_subject": request(rfc_a, "sp22", subject_ref=foreign_subject),
                  "sp22_codes": ["subject_mismatch"]},
        "SP-23": {"rfc_out_of_scope_txns": request(rfc_a, "sp23", txn_ids=[foreign_txn]),
                  "sp23_codes": ["txn_out_of_feed"]},
        "SP-24": {"rfc_claims_bank_a": request(rfc_b, "sp24", issuer="bank_a", audience="bank_b"),
                  "rfc_claims_bank_a_control": request(rfc_b, "sp24-control", issuer="bank_b", audience="bank_b")},
    }
    return {probe: {"routes": {"rfc_intake": "/rfc"}, "fixtures": values} for probe, values in fixtures.items()}


def build(config: dict, args) -> tuple[dict, dict]:
    """Return (profile, report). The report holds key names and reasons only."""
    stack = Stack(config, Path(args.key_dir))
    case = load_case_without_gold(CASE)
    errors: dict[str, str] = {}
    try:
        explicit_a = _explicit(config, "native_verifier_package_id", args.bank_a_package)
        explicit_b = _explicit(config, "bank_b_package_id", args.bank_b_package)
        explicit_h = _explicit(config, "holder_request_id", args.holder_request)
        identity = stack.get("bank", "/", None)
        if identity.get("service") != "mock-bank" or identity.get("case_number") != CASE:
            raise ProfileError("the bank is not the native mock bank seeded with Case 1")
        receipt_a = _section(errors, "bank_a", lambda: _receipt(stack, case, "bank_a", explicit_a))
        receipt_b = _section(errors, "bank_b", lambda: _receipt(stack, case, "bank_b", explicit_b))
        fixtures_a = (_section(errors, "bank_a", lambda: _verifier(stack, receipt_a))
                      if receipt_a is not None else None)
        holder_rid = explicit_h or (receipt_a or {}).get("request_id")
        fixtures_h = _section(errors, "holder", lambda: _holder(stack, case, holder_rid))
    finally:
        stack.session.close()
    plan = _plan(receipt_b, fixtures_a, fixtures_h)
    plan.update(_section(errors, "rfc_intake", lambda: _rfc_intake_plan(case)) or {})

    profile = deepcopy(config)
    base, tokens = config.get("base_urls", {}), config.get("tokens", {})
    report = {}
    for probe, (section, actors, route_keys, fixture_keys) in NEEDS.items():
        local = config.get("probes", {}).get(probe, {})
        local_urls = local.get("base_urls", {})
        explicit = {s: {**base.get(s, {}), **local_urls.get(s, {})} for s in ("routes", "fixtures")}
        values = plan.get(probe)
        entry = {"generated": [], "explicit": [], "missing": []}
        if local.get("tokens") or set(local_urls) - {"routes", "fixtures"}:
            values, entry["reason"] = None, "probe has its own origin or token override"
        elif values is None:
            entry["reason"] = errors.get(section, f"{section} inputs were not verified")
        for kind, keys in (("routes", route_keys), ("fixtures", fixture_keys)):
            for key in keys:
                if key in explicit[kind]:
                    entry["explicit"].append(key)
                elif values is not None and key in values[kind]:
                    target = profile.setdefault("probes", {}).setdefault(probe, {})
                    target.setdefault("base_urls", {}).setdefault(kind, {})[key] = values[kind][key]
                    entry["generated"].append(key)
                else:
                    entry["missing"].append(key)
        probe_tokens = {**tokens, **local.get("tokens", {})}
        missing_tokens = [actor for actor in actors
                          if not isinstance(probe_tokens.get(actor), str) or not probe_tokens[actor]]
        if missing_tokens:
            entry["missing_tokens"] = missing_tokens
        entry["ready"] = not entry["missing"] and not missing_tokens
        report[probe] = entry
    try:
        build_probe_harness(profile)
    except ValueError as exc:
        raise ProfileError(f"generated profile is not a valid run_probes config: {exc}") from None
    return profile, {"probes": report}


def _load(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ProfileError("cannot read the config as JSON") from None
    try:
        build_probe_harness(config)
    except ValueError as exc:
        raise ProfileError(f"config: {exc}") from None
    return config


def _check_out(out: Path, config_path: Path) -> Path:
    if out.is_symlink():
        raise ProfileError("--out must not be a symlink")
    out = out.parent.resolve() / out.name
    if out.exists() and out.resolve() == config_path.resolve():
        raise ProfileError("--out must not overwrite the input config")
    if not out.parent.is_dir():
        raise ProfileError("--out directory does not exist")
    for parent in out.parents:
        if (parent / ".git").exists():
            try:
                ignored = subprocess.run(["git", "-C", str(parent), "check-ignore", "-q", "--", str(out)],
                                         capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ignored = False
            if not ignored:
                raise ProfileError("--out must be outside Git or at a git-ignored path")
            break
    return out


def _write(out: Path, profile: dict) -> None:
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _scrub(text: str, tokens: dict) -> str:
    for token in tokens.values():
        if isinstance(token, str) and token:
            text = text.replace(token, "[REDACTED]")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True,
                        help="run_probes config with loopback origins and synthetic tokens")
    parser.add_argument("--out", type=Path, required=True,
                        help="owner-only profile path, outside Git or git-ignored")
    parser.add_argument("--key-dir", type=Path, default=DEFAULT_KEY_DIR,
                        help="demo key directory: its public registry is read, PEMs only checked")
    parser.add_argument("--bank-a-package", help="bank_a Case 1 package for SP-25..SP-32")
    parser.add_argument("--bank-b-package", help="bank_b Case 1 package for SP-17, SP-18, SP-20")
    parser.add_argument("--holder-request", help="holder request for SP-21 (default: bank_a's)")
    args = parser.parse_args(argv)
    tokens: dict = {}
    try:
        config = _load(args.config)
        tokens = config.get("tokens", {})
        out = _check_out(args.out, args.config)
        profile, report = build(config, args)
        _write(out, profile)
    except ProfileError as exc:
        print(_scrub(f"probe_profile: {exc}", tokens), file=sys.stderr)
        return 1
    except OSError:
        print("probe_profile: cannot write the profile at --out", file=sys.stderr)
        return 1
    report["out"] = str(out)
    print(_scrub(json.dumps(report, indent=1, sort_keys=True), tokens))
    return 0 if all(entry["ready"] for entry in report["probes"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
