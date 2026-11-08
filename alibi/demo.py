"""The whole Alibi demo in one process: mock merchant (8101), mock bank (8102) and the holder
app (8100), wired to each other over HTTP, seeded with a case at a simulated clock.

    uv run --frozen python -m alibi.demo --timeout 600 [--case 1] [--reset] [--as-of <utc>] [--replay]

Wiring: the merchant pushes signed records to ``{app}/connect/merchant/webhook``; the bank
forwards every issued Request for Context to ``{app}/connect/bank/rfc`` with the recipient's
bearer; the app is started with ``ALIBI_MERCHANT_URL``, ``ALIBI_BANK_URL``, ``ALIBI_API_ORIGIN``,
the shared tokens file (``ALIBI_TOKENS_FILE``, default ``data/alibi/bank_tokens.json``: ``bank_a``,
``bank_b``, ``feed``, ``admin``, with the ``ALIBI_*_TOKEN`` environment names overriding, so the app
and the bank service always agree) and its data directory ``data/alibi/demo/case_<n>/``. The
passport starts with no case records preloaded: every record arrives through the connect routes,
in this order once all three servers listen: merchant seed at ``--as-of`` (the pushes land in the
app through the webhook route), bank seed, then ``POST {app}/connect/bank/sync`` so the feed rows
arrive. Then one JSON line names the URLs, the tokens file path and the seed counts; a token
value is never printed.

The default clock is ``2026-10-25T00:00:00Z``: Maya's history is in, the balance release
``rec-0001-020`` (received 2026-10-26T08:05Z), the credit ``txn-0001-004`` (2026-10-28T07:30Z)
and both RFCs (2026-11-02, 2026-11-04) are not. Without ``--reset`` the app data directory and
the bank state file are kept and the stack resumes at the clock the bank had reached (or
``--as-of`` if later), so a restart shows persistence: every merchant push a duplicate, the
bank's inboxes intact. ``--reset`` deletes both first and says so. ``--replay`` sets ``ALIBI_REPLAY=1`` for the app: every
page carries the banner and recorded Gemini answers are served; everything else stays live.

Driver subcommands talk to the running stack and print one JSON line each (exit 0, or 1 on a
refusal or an unreachable service):

    python -m alibi.demo advance --to 2026-10-26T18:05:00Z   # merchant and bank clocks, then feed sync
    python -m alibi.demo rfc bank_a [--request-id rfc-0001-01]  # the bank issues and forwards the RFC
    python -m alibi.demo rfc bank_b
    python -m alibi.demo status                                # app /health plus both identities

Never pass GEMINI_API_KEY or ALIBI_MASTER_KEY on the command line; they are not needed here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from alibi import demo_services
from alibi.connectors.case_source import MappingError, parse_time
from alibi.connectors.http import ADMIN, TOKEN_ENV, load_tokens
from alibi.core.schemas import format_utc
from alibi.core.signing import DEFAULT_KEY_DIR

__all__ = ["Stack", "announce", "app_environment", "bank_state_path", "case_dir", "main", "parse_args",
           "reset", "resume_clock", "seed_mode", "serve"]

DEFAULT_APP_PORT = 8100
DEFAULT_MERCHANT_PORT = demo_services.DEFAULT_MERCHANT_PORT
DEFAULT_BANK_PORT = demo_services.DEFAULT_BANK_PORT
DEFAULT_DIRECTOR_PORT = 8103
DEFAULT_AS_OF = "2026-10-25T00:00:00Z"
DEFAULT_ROOT = Path("data/alibi/demo")
DEFAULT_TOKENS = demo_services.DEFAULT_TOKENS
STARTUP_DEADLINE = demo_services.STARTUP_DEADLINE
WAIT_SLICE = demo_services.WAIT_SLICE
RECIPIENTS = ("bank_a", "bank_b")
# What the URL line says about a console credential: the token's name, never its value.
TOKEN_NOTE = {name: f"{TOKEN_ENV[name]} or tokens file entry {name}" for name in RECIPIENTS}
# Keys of the app environment that are safe to print or assert on (no token among them).
APP_ENV_PUBLIC = ("ALIBI_CASE", "ALIBI_DATA_DIR", "ALIBI_KEY_DIR", "ALIBI_MERCHANT_URL",
                  "ALIBI_BANK_URL", "ALIBI_API_ORIGIN", "ALIBI_TOKENS_FILE", "ALIBI_REPLAY")
HTTP_TIMEOUT = 60.0


# --------------------------------------------------------------------------- arguments


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="alibi.demo", description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", type=int, default=1, help="case number to seed (default 1)")
    parser.add_argument("--as-of", dest="as_of", default=DEFAULT_AS_OF,
                        help=f"simulated clock of the services, UTC ISO-8601 (default {DEFAULT_AS_OF})")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds to serve before exiting (default: until Ctrl-C)")
    parser.add_argument("--reset", action="store_true",
                        help="delete the app data directory and the bank state file first")
    parser.add_argument("--replay", action="store_true",
                        help="ALIBI_REPLAY=1 for the app: banner on every page, recorded Gemini answers")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--app-port", type=int, default=DEFAULT_APP_PORT)
    parser.add_argument("--merchant-port", type=int, default=DEFAULT_MERCHANT_PORT)
    parser.add_argument("--bank-port", type=int, default=DEFAULT_BANK_PORT)
    parser.add_argument("--director-port", type=int, default=DEFAULT_DIRECTOR_PORT,
                        help=f"demo director (alibi/demo_director.py); -1 turns it off (default {DEFAULT_DIRECTOR_PORT})")
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help="demo state root; the app data dir is <root>/case_<n>/ (default data/alibi/demo)")
    parser.add_argument("--keys", default=str(DEFAULT_KEY_DIR), help="shared key directory")
    parser.add_argument("--tokens", default=str(DEFAULT_TOKENS), help="shared tokens file")

    sub = parser.add_subparsers(dest="command", metavar="{advance,rfc,status}")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--app-url", default=f"http://127.0.0.1:{DEFAULT_APP_PORT}")
        p.add_argument("--merchant-url", default=f"http://127.0.0.1:{DEFAULT_MERCHANT_PORT}")
        p.add_argument("--bank-url", default=f"http://127.0.0.1:{DEFAULT_BANK_PORT}")
        p.add_argument("--tokens", default=str(DEFAULT_TOKENS), help="shared tokens file")

    advance = sub.add_parser("advance", help="move both service clocks forward, then sync the app's feed")
    advance.add_argument("--to", required=True, help="new clock, UTC ISO-8601")
    common(advance)
    rfc = sub.add_parser("rfc", help="have the bank issue (and forward) a Request for Context")
    rfc.add_argument("recipient", choices=RECIPIENTS)
    rfc.add_argument("--request-id", dest="request_id", default=None,
                     help="a case RFC id (default: the recipient's next unissued case RFC)")
    common(rfc)
    status = sub.add_parser("status", help="the app's /health and both services' identities")
    common(status)
    return parser.parse_args(argv)


def _normalise_time(value: str, flag: str) -> str:
    try:
        return format_utc(parse_time(value, flag.lstrip("-").replace("-", "_")))
    except MappingError as exc:
        raise SystemExit(f"invalid {flag}: {exc} (invalid_time)") from None


# --------------------------------------------------------------------------- state and env


def case_dir(root: Path | str, case: int) -> Path:
    return Path(root) / f"case_{case}"


def bank_state_path(root: Path | str, case: int) -> Path:
    return case_dir(root, case) / "bank_state.json"


def reset(root: Path | str, case: int) -> dict[str, Any]:
    """Delete the app data directory (database, sidecar) and the bank state file of a case."""
    removed: list[str] = []
    state = bank_state_path(root, case)
    if state.exists():
        state.unlink()
        removed.append(str(state))
    folder = case_dir(root, case)
    if folder.exists():
        shutil.rmtree(folder)
        removed.append(str(folder))
    return {"reset": True, "removed": removed}


def resume_clock(root: Path | str, case: int, as_of: str) -> tuple[str, str | None]:
    """The clock the stack starts at. A kept bank state file for this case (no ``--reset``) names
    the clock the previous run had reached; the stack resumes at the later of that and
    ``as_of``, so the passport's feed cursor stays valid and the bank keeps its inboxes. Returns
    ``(clock, resumed_from)`` with ``resumed_from`` None on a fresh start."""
    path = bank_state_path(root, case)
    if not path.exists():
        return as_of, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return as_of, None
    if not isinstance(data, dict) or data.get("case_number") != case:
        return as_of, None
    kept = data.get("as_of")
    if not isinstance(kept, str) or not kept:
        return as_of, None
    return (kept if kept > as_of else as_of), kept


def app_environment(args: argparse.Namespace) -> dict[str, str]:
    """The environment the holder app is started with. Only paths and URLs: the credentials
    come from the shared tokens file (``ALIBI_TOKENS_FILE``) that the services read too, so no
    token value is ever placed in the environment or printed."""
    app = f"http://{args.host}:{args.app_port}"
    env = {
        "ALIBI_CASE": str(args.case),
        "ALIBI_DATA_DIR": str(case_dir(args.root, args.case)),
        "ALIBI_KEY_DIR": str(args.keys),
        "ALIBI_MERCHANT_URL": f"http://{args.host}:{args.merchant_port}",
        "ALIBI_BANK_URL": f"http://{args.host}:{args.bank_port}",
        "ALIBI_API_ORIGIN": app,
        "ALIBI_TOKENS_FILE": str(args.tokens),
    }
    if args.replay:
        env["ALIBI_REPLAY"] = "1"
    return env


def build_passport(args: argparse.Namespace, env: dict[str, str], clock=None):
    """Create the holder app through its public factory. The environment is applied to this
    process before the settings are read, so modules that read ``os.environ`` directly (replay,
    the store key) see it too. With both service URLs set the runtime seeds in ``connectors``
    mode and replays nothing from the case file: every record arrives through the connect routes."""
    from alibi.app.config import settings_from_env
    from alibi.app.main import create_app

    os.environ.update(env)
    return create_app(settings_from_env(clock=clock) if clock is not None else settings_from_env())


def seed_mode(passport) -> str | None:
    """How the running app fills its passport (``connectors`` or ``case_adapter``), when the
    runtime says."""
    runtime = getattr(passport.state, "runtime", None)
    return getattr(runtime, "seed", None)


# --------------------------------------------------------------------------- HTTP helpers


def _safe_body(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return {"code": "non_json", "reason": f"HTTP {response.status_code} without a JSON body"}
    return body


def _call(client: httpx.Client, method: str, url: str, *, headers: dict[str, str] | None = None,
          params: dict[str, Any] | None = None, json_body: Any = None) -> tuple[int | None, Any]:
    """One request; connection failures come back as ``(None, {"error": ...})`` naming only
    the exception type."""
    try:
        response = client.request(method, url, headers=headers, params=params, json=json_body)
    except httpx.HTTPError as exc:
        return None, {"error": f"{url} unreachable ({type(exc).__name__})"}
    return response.status_code, _safe_body(response)


def _ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def _outcome(status: int | None, body: Any) -> dict[str, Any]:
    """A failed call as data: the status and the refusal body (code and reason, never a token)."""
    if isinstance(body, dict) and "error" in body:
        return {"ok": False, "status": status, "error": body["error"]}
    detail = {k: body[k] for k in ("code", "reason") if isinstance(body, dict) and k in body}
    return {"ok": False, "status": status, **detail}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _holder_headers() -> dict[str, str]:
    token = os.environ.get("ALIBI_HOLDER_TOKEN", "").strip()
    return _bearer(token) if token else {}


def _feed_sync(client: httpx.Client, app_url: str) -> dict[str, Any]:
    status, body = _call(client, "POST", f"{app_url}/connect/bank/sync", headers=_holder_headers(),
                         json_body={})
    if _ok(status) and isinstance(body, dict):
        return {"ok": True, **{k: v for k, v in body.items() if k != "version"}}
    return _outcome(status, body)


def _tokens_for_commands(path: str) -> dict[str, str] | None:
    """The shared tokens for a driver subcommand. Unlike the runner, a subcommand never creates
    the file: without it (and without the four environment names) the stack is not running."""
    if not Path(path).exists() and not all(os.environ.get(var) for var in TOKEN_ENV.values()):
        return None
    return load_tokens(path)


# --------------------------------------------------------------------------- the runner


def announce(args: argparse.Namespace, ports: dict[str, int], seeds: dict[str, Any],
             reset_info: dict[str, Any] | None, tokens_path: str,
             passport_seed: str | None = None, resumed_from: str | None = None) -> dict[str, Any]:
    """The one URL line. Contains names of credentials, never their values."""
    app = f"http://{args.host}:{ports['app']}"
    merchant_seed = seeds.get("merchant") if isinstance(seeds.get("merchant"), dict) else {}
    pushed = merchant_seed.get("pushed") if merchant_seed.get("ok", True) else None
    return {
        "app": app,
        "merchant": f"http://{args.host}:{ports['merchant']}",
        "bank": f"http://{args.host}:{ports['bank']}",
        "holder": f"{app}/passport",
        "consoles": {name: {"url": f"{app}/bank/{name}/inbox", "token": TOKEN_NOTE[name]}
                     for name in RECIPIENTS},
        "tokens_file": tokens_path,
        "case": args.case,
        "as_of": args.as_of,
        "resumed_from": resumed_from,
        "data_dir": str(case_dir(args.root, args.case)),
        "bank_state": str(bank_state_path(args.root, args.case)),
        "keys": str(args.keys),
        "passport_seed": passport_seed,
        "seeds": {
            "merchant_pushed": len(pushed) if isinstance(pushed, list) else None,
            "merchant_outcomes": merchant_seed.get("counts") if pushed is not None else merchant_seed,
            "bank": seeds.get("bank"),
            "feed": seeds.get("feed"),
        },
        "replay": bool(args.replay),
        "reset": reset_info or {"reset": False},
        "timeout": args.timeout,
    }


def _seed(client: httpx.Client, args: argparse.Namespace, urls: dict[str, str],
          tokens: dict[str, str], resumed_from: str | None = None) -> dict[str, Any]:
    """Merchant seed at the clock (pushes through the webhook route), bank seed, then the app's
    feed sync. On a resume the bank already holds the case from its state file and is only
    advanced to the clock, so its inboxes and dispositions survive the restart."""
    admin = _bearer(tokens[ADMIN])
    params = {"case": args.case, "as_of": args.as_of}
    seeds: dict[str, Any] = {}
    status, body = _call(client, "POST", f"{urls['merchant']}/admin/seed", headers=admin, params=params)
    seeds["merchant"] = ({"ok": True, **body} if _ok(status) and isinstance(body, dict)
                         else _outcome(status, body))
    if resumed_from is None:
        status, body = _call(client, "POST", f"{urls['bank']}/admin/seed", headers=admin, params=params)
        seeds["bank"] = ({"ok": True, "as_of": body.get("as_of"), "recipients": body.get("recipients"),
                          "subject_ref": body.get("subject_ref")}
                         if _ok(status) and isinstance(body, dict) else _outcome(status, body))
    elif args.as_of > resumed_from:
        status, body = _call(client, "POST", f"{urls['bank']}/admin/advance", headers=admin,
                             params={"as_of": args.as_of})
        seeds["bank"] = ({"ok": True, "resumed": True, "as_of": body.get("as_of")}
                         if _ok(status) and isinstance(body, dict) else _outcome(status, body))
    else:
        seeds["bank"] = {"ok": True, "resumed": True, "as_of": resumed_from}
    seeds["feed"] = _feed_sync(client, urls["app"])
    if resumed_from is not None and not seeds["feed"].get("ok"):
        seeds["feed"]["hint"] = ("the kept passport is ahead of the bank's clock (its feed cursor "
                                 "names rows the re-seeded feed does not serve yet); start with "
                                 "--reset, or with --as-of at the clock the passport had reached")
    return seeds


def _shutdown(servers: list, passport, out) -> int:
    code = demo_services._shutdown(servers, out)
    try:
        passport.state.runtime.close()
    except Exception:  # noqa: BLE001 - closing a store on exit must not hide the exit code
        pass
    return code


class Stack:
    """The three servers of one case: holder app, mock merchant, mock bank. ``start`` builds,
    binds and seeds them; ``stop`` closes them; ``restart(reset=True)`` is the director's reset
    (same ports, fresh state, about two seconds). The holder app reads the bank's simulated
    clock in-process, so both always agree on "now"."""

    def __init__(self, args: argparse.Namespace, tokens: dict[str, str], out=sys.stdout):
        self.args, self.tokens, self.out = args, tokens, out
        self.start_as_of = args.as_of
        self.servers: list = []
        self.passport = None
        self.services: dict[str, Any] = {}
        self.urls: dict[str, str] = {}
        self.ports: dict[str, int] = {}
        self.seeds: dict[str, Any] = {}
        self.lock = threading.RLock()

    # the director's view
    def bank_service(self):
        bank = self.services.get("bank")
        return getattr(getattr(bank, "state", None), "bank", None)

    @staticmethod
    def holder_headers() -> dict[str, str]:
        return _holder_headers()

    def _clock(self):
        fallback = parse_time(self.args.as_of, "as_of")

        def now():
            bank = self.bank_service()
            clock = getattr(bank, "clock", None)
            return clock.as_of if clock is not None else fallback
        return now

    def start(self, resumed_from: str | None = None) -> bool:
        args = self.args
        env = app_environment(args)
        app_url = env["ALIBI_API_ORIGIN"]
        self.services = demo_services.build_apps(demo_services.parse_args([
            "--case", str(args.case), "--as-of", args.as_of, "--no-seed",
            "--keys", str(args.keys), "--tokens", str(args.tokens),
            "--state", str(bank_state_path(args.root, args.case)),
            "--passport-url", f"{app_url}/connect/merchant/webhook",
            "--passport-rfc-url", f"{app_url}/connect/bank/rfc",
        ]))
        self.passport = build_passport(args, env, clock=self._clock())
        self.servers = [
            demo_services._Server(self.passport, args.host, self.ports.get("app", args.app_port), "app"),
            demo_services._Server(self.services["merchant"], args.host,
                                  self.ports.get("merchant", args.merchant_port), "merchant"),
            demo_services._Server(self.services["bank"], args.host,
                                  self.ports.get("bank", args.bank_port), "bank"),
        ]
        for server in self.servers:
            server.start()
        deadline = time.monotonic() + STARTUP_DEADLINE
        while not all(s.server.started for s in self.servers):
            if demo_services._failed(self.servers) or time.monotonic() >= deadline:
                stuck = [s.name for s in self.servers if not s.server.started]
                print(json.dumps({"error": "server did not start", "servers": stuck}),
                      file=self.out, flush=True)
                return False
            time.sleep(0.05)
        self.ports = {s.name: s.server.servers[0].sockets[0].getsockname()[1] for s in self.servers}
        self.urls = {name: f"http://{args.host}:{port}" for name, port in self.ports.items()}
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            self.seeds = _seed(client, args, self.urls, self.tokens, resumed_from)
        return True

    def stop(self) -> int:
        return _shutdown(self.servers, self.passport, self.out) if self.servers else 0

    def restart(self, *, reset: bool = True) -> None:
        with self.lock:
            self.stop()
            if reset:
                print(json.dumps(globals()["reset"](self.args.root, self.args.case)), file=self.out, flush=True)
                self.args.as_of = self.start_as_of
            if not self.start():
                raise RuntimeError("the demo stack did not restart")

    def failed(self) -> list[str]:
        with self.lock:
            return demo_services._failed(self.servers)


def _director_server(args: argparse.Namespace, stack: Stack, tokens: dict[str, str]):
    from alibi.demo_director import Director, create_director_app, demo_token

    director = Director(stack)
    app = create_director_app(director, lambda: demo_token(tokens))
    return demo_services._Server(app, args.host, args.director_port, "director")


def serve(args: argparse.Namespace, *, out=sys.stdout) -> int:
    args.as_of = _normalise_time(args.as_of, "--as-of")
    # ``ALIBI_REPLAY=1 python -m alibi.demo`` (the demo script's fallback) means --replay too.
    args.replay = bool(args.replay or os.environ.get("ALIBI_REPLAY", "").strip() in ("1", "true", "yes"))
    reset_info = reset(args.root, args.case) if args.reset else None
    if reset_info:
        print(json.dumps(reset_info), file=out, flush=True)
    args.as_of, resumed_from = resume_clock(args.root, args.case, args.as_of)
    tokens = load_tokens(args.tokens)            # one file: bank_a, bank_b, feed, admin
    stack = Stack(args, tokens, out)
    director = None
    try:
        if not stack.start(resumed_from):
            return stack.stop() or 1
        if getattr(args, "director_port", -1) >= 0:
            director = _director_server(args, stack, tokens)
            director.start()
            deadline = time.monotonic() + STARTUP_DEADLINE
            while not director.server.started:
                if director.error is not None or time.monotonic() >= deadline:
                    print(json.dumps({"error": "server did not start", "servers": ["director"]}),
                          file=out, flush=True)
                    director.stop()
                    return stack.stop() or 1
                time.sleep(0.05)
        line = announce(args, stack.ports, stack.seeds, reset_info, stack.services["tokens_path"],
                        seed_mode(stack.passport), resumed_from)
        if director is not None:
            line["director"] = f"http://{args.host}:{director.server.servers[0].sockets[0].getsockname()[1]}"
        print(json.dumps(line), file=out, flush=True)
        stop = threading.Event()
        until = None if args.timeout is None else time.monotonic() + args.timeout
        while until is None or time.monotonic() < until:
            remaining = WAIT_SLICE if until is None else min(WAIT_SLICE, until - time.monotonic())
            if remaining > 0:
                stop.wait(remaining)
            if stack.failed():
                break
    except KeyboardInterrupt:
        pass
    if director is not None:
        director.stop()
        director.join(timeout=10)
    return stack.stop()


# --------------------------------------------------------------------------- driver commands


def cmd_status(args: argparse.Namespace, *, out=sys.stdout) -> int:
    result: dict[str, Any] = {}
    failed = False
    with httpx.Client(timeout=15.0) as client:
        for name, url in (("app", f"{args.app_url}/health"), ("merchant", f"{args.merchant_url}/"),
                          ("bank", f"{args.bank_url}/")):
            status, body = _call(client, "GET", url)
            if _ok(status):
                result[name] = body
            else:
                result[name] = _outcome(status, body)
                failed = True
    print(json.dumps(result), file=out, flush=True)
    return 1 if failed else 0


def cmd_advance(args: argparse.Namespace, *, out=sys.stdout) -> int:
    to = _normalise_time(args.to, "--to")
    tokens = _tokens_for_commands(args.tokens)
    if tokens is None:
        print(json.dumps({"error": f"tokens file {args.tokens} not found; is the stack running?"}),
              file=out, flush=True)
        return 1
    admin = _bearer(tokens[ADMIN])
    result: dict[str, Any] = {"advanced_to": to}
    failed = False
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        status, body = _call(client, "POST", f"{args.merchant_url}/admin/advance", headers=admin,
                             params={"as_of": to})
        if _ok(status) and isinstance(body, dict):
            pushed = body.get("pushed") or []
            counts: dict[str, int] = {}
            for entry in pushed:
                counts[entry.get("outcome", "?")] = counts.get(entry.get("outcome", "?"), 0) + 1
            result["merchant"] = {"ok": True, "as_of": body.get("as_of"), "pushed": len(pushed),
                                  "outcomes": counts,
                                  "records": [f"{e.get('record_id')}@{e.get('revision')}" for e in pushed]}
        else:
            result["merchant"] = _outcome(status, body)
            failed = True
        status, body = _call(client, "POST", f"{args.bank_url}/admin/advance", headers=admin,
                             params={"as_of": to})
        if _ok(status) and isinstance(body, dict):
            result["bank"] = {"ok": True, "as_of": body.get("as_of")}
        else:
            result["bank"] = _outcome(status, body)
            failed = True
        result["feed"] = _feed_sync(client, args.app_url)
        failed = failed or not result["feed"].get("ok")
    print(json.dumps(result), file=out, flush=True)
    return 1 if failed else 0


def cmd_rfc(args: argparse.Namespace, *, out=sys.stdout) -> int:
    tokens = _tokens_for_commands(args.tokens)
    if tokens is None:
        print(json.dumps({"error": f"tokens file {args.tokens} not found; is the stack running?"}),
              file=out, flush=True)
        return 1
    recipient = args.recipient
    headers = _bearer(tokens[recipient])
    body_in: dict[str, Any] = {"request_id": args.request_id} if args.request_id else {}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        status, body = _call(client, "POST", f"{args.bank_url}/rfc", headers=headers, json_body=body_in)
        if not (_ok(status) and isinstance(body, dict)):
            print(json.dumps({"recipient": recipient, "request_id": args.request_id,
                              **_outcome(status, body)}), file=out, flush=True)
            return 1
        request = (body.get("rfc") or {}).get("request") or {}
        result: dict[str, Any] = {
            "recipient": recipient, "request_id": body.get("request_id"),
            "reissued": status == 200, "issued_at": request.get("issued_at"),
            "expires_at": request.get("expires_at"),
            "scope": {k: request.get(k) for k in ("txn_ids", "period") if request.get(k) is not None},
            "forwarded": body.get("forwarded"),
            "holder_url": f"{args.app_url}/requests/{body.get('request_id')}",
        }
        if result["forwarded"] is None:
            # The bank has no forwarder configured: hand the stored request to the app ourselves.
            status_b, bank = _call(client, "GET", f"{args.bank_url}/")
            now = bank.get("as_of") if _ok(status_b) and isinstance(bank, dict) else None
            status_f, forwarded = _call(client, "POST", f"{args.app_url}/connect/bank/rfc", headers=headers,
                                        json_body={"rfc": body.get("rfc"), "recipient": recipient,
                                                   "account_refs": body.get("account_refs") or [],
                                                   "now": now or request.get("issued_at")})
            result["forwarded"] = ({"forwarded_by": "runner", **forwarded}
                                   if _ok(status_f) and isinstance(forwarded, dict)
                                   else {"forwarded_by": "runner", **_outcome(status_f, forwarded)})
    forwarded_status = (result["forwarded"] or {}).get("status")
    print(json.dumps(result), file=out, flush=True)
    return 0 if forwarded_status == "accepted" else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "advance":
        return cmd_advance(args)
    if args.command == "rfc":
        return cmd_rfc(args)
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
