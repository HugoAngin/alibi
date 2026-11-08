"""Start the simulated services for the demo: the mock merchant (8101) and the mock bank (8102).

    uv run --frozen python -m alibi.demo_services --case 1 --as-of 2026-11-02T09:00:00Z --timeout 600

Both apps share one connector keystore (``data/alibi/keys`` by default, five demo keys) and are
seeded with the case at ``--as-of`` (default: the case's first RFC ``issued_at``). The merchant
pushes its webhooks to ``--passport-url`` when given; without it the pushes are recorded as
``not_delivered`` and a passport pulls them with ``GET /admin/deliveries``
(``alibi.connectors.clients.bootstrap`` does that). Bank tokens come from the environment
(``ALIBI_BANK_A_TOKEN``, ``ALIBI_BANK_B_TOKEN``, ``ALIBI_FEED_TOKEN``) or ``data/alibi/bank_tokens.json``;
only the file's path is printed, never a token.

The URL line is printed only once both servers listen; a server that cannot bind its port (a
previous demo still running, say) is reported at once as ``{"stopped": true, "failed": [...]}``
with exit code 1. The process then serves until ``--timeout`` seconds pass (or Ctrl-C, which
is checked every half second so it works on Windows too), stops both servers and exits 0.
Never pass GEMINI_API_KEY or ALIBI_MASTER_KEY on the command line; they are not needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.testclient import TestClient

from alibi.connectors.case_source import MappingError, parse_time
from alibi.core.schemas import format_utc
from alibi.connectors.keys import connector_keystore
from alibi.core.signing import DEFAULT_KEY_DIR

__all__ = ["build_apps", "main", "parse_args", "serve"]

DEFAULT_MERCHANT_PORT = 8101
DEFAULT_BANK_PORT = 8102
DEFAULT_STATE = Path("data/alibi/bank/state.json")
DEFAULT_TOKENS = Path("data/alibi/bank_tokens.json")
STARTUP_DEADLINE = 15.0   # seconds for both servers to bind before the runner gives up
WAIT_SLICE = 0.5          # the main thread wakes this often, so Ctrl-C is seen on Windows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="alibi.demo_services", description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", type=int, default=1, help="case number to seed (default 1)")
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="simulated clock, UTC ISO-8601 (default: the case's first RFC issued_at)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds to serve before exiting (default: until Ctrl-C)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--merchant-port", type=int, default=DEFAULT_MERCHANT_PORT)
    parser.add_argument("--bank-port", type=int, default=DEFAULT_BANK_PORT)
    parser.add_argument("--passport-url", default=None,
                        help="the passport's merchant webhook URL; pushes go there when given")
    parser.add_argument("--keys", default=str(DEFAULT_KEY_DIR), help="key directory")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="bank state file")
    parser.add_argument("--tokens", default=str(DEFAULT_TOKENS), help="bank tokens file")
    parser.add_argument("--no-seed", action="store_true", help="start without seeding a case")
    return parser.parse_args(argv)


def build_apps(args: argparse.Namespace) -> dict[str, Any]:
    """Create both apps with one keystore and seed them. Returns the apps and the seed results.
    Separated from ``serve`` so tests can build without binding a port."""
    from alibi.bank.service import create_bank_app, load_tokens
    from alibi.connectors.merchant import create_merchant_app

    if args.as_of is not None:
        try:
            args.as_of = format_utc(parse_time(args.as_of, "as_of"))
        except MappingError as exc:
            raise SystemExit(f"invalid --as-of: {exc} (invalid_time)") from None
    keystore = connector_keystore(args.keys)
    tokens = load_tokens(args.tokens)
    merchant = create_merchant_app(keystore=keystore, passport_url=args.passport_url)
    bank = create_bank_app(keystore=keystore, registry=keystore.registry(), tokens=tokens,
                           state_path=args.state)
    seeds: dict[str, Any] = {}
    if not args.no_seed:
        params: dict[str, Any] = {"case": args.case}
        if args.as_of:
            params["as_of"] = args.as_of
        with TestClient(merchant) as client:
            response = client.post("/admin/seed", params=params)
            seeds["merchant"] = response.json()
            if response.status_code >= 400:
                raise SystemExit(f"merchant seed failed: {response.json()}")
        with TestClient(bank) as client:
            response = client.post("/admin/seed", params=params)
            seeds["bank"] = response.json()
            if response.status_code >= 400:
                raise SystemExit(f"bank seed failed: {response.json()}")
    return {"merchant": merchant, "bank": bank, "keystore": keystore, "seeds": seeds,
            "tokens_path": str(args.tokens)}


class _Server(threading.Thread):
    """One uvicorn server on its own thread, stoppable with ``stop()``."""

    def __init__(self, app, host: str, port: int, name: str):
        super().__init__(name=name, daemon=True)
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                                     access_log=False)
        self.server = uvicorn.Server(self.config)
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:  # noqa: BLE001 - reported by serve()
            self.error = exc

    def stop(self) -> None:
        self.server.should_exit = True


def _failed(servers: list[_Server]) -> list[str]:
    """Servers that raised (uvicorn exits with ``SystemExit`` when it cannot bind) or whose
    thread ended without being asked to stop."""
    return [s.name for s in servers
            if s.error is not None or (not s.is_alive() and not s.server.should_exit)]


def _shutdown(servers: list[_Server], out) -> int:
    for server in servers:
        server.stop()
    for server in servers:
        server.join(timeout=10)
    failed = [s.name for s in servers if s.error is not None]
    print(json.dumps({"stopped": True, "failed": failed}), file=out, flush=True)
    return 0 if not failed else 1


def serve(args: argparse.Namespace, *, out=sys.stdout) -> int:
    built = build_apps(args)
    merchant_seed = built["seeds"].get("merchant") or {}
    servers = [
        _Server(built["merchant"], args.host, args.merchant_port, "merchant"),
        _Server(built["bank"], args.host, args.bank_port, "bank"),
    ]
    for server in servers:
        server.start()
    try:
        # Announce the URLs only once both servers listen. A bind failure (port in use) ends
        # uvicorn's thread with SystemExit, which _Server keeps as ``error``: report it now
        # rather than after the whole timeout.
        deadline = time.monotonic() + STARTUP_DEADLINE
        while not all(s.server.started for s in servers):
            if _failed(servers) or time.monotonic() >= deadline:
                stuck = [s.name for s in servers if not s.server.started]
                print(json.dumps({"error": "server did not start", "servers": stuck}),
                      file=out, flush=True)
                return _shutdown(servers, out) or 1
            time.sleep(0.05)
        ports = {s.name: s.server.servers[0].sockets[0].getsockname()[1] for s in servers}
        print(json.dumps({
            "merchant": f"http://{args.host}:{ports['merchant']}",
            "bank": f"http://{args.host}:{ports['bank']}",
            "case": args.case,
            "as_of": merchant_seed.get("as_of", args.as_of),
            "merchant_pushed": len(merchant_seed.get("pushed") or []),
            "keys": args.keys,
            "tokens_file": built["tokens_path"],
            "bank_state": args.state,
            "timeout": args.timeout,
        }), file=out, flush=True)
        # Short waits, not one long one: on Windows a blocked wait is not interruptible, so a
        # single ``Event.wait(timeout)`` would defer Ctrl-C until the timeout expired (forever
        # without one). Each wake-up also notices a server that died.
        stop = threading.Event()
        until = None if args.timeout is None else time.monotonic() + args.timeout
        while until is None or time.monotonic() < until:
            remaining = WAIT_SLICE if until is None else min(WAIT_SLICE, until - time.monotonic())
            if remaining > 0:
                stop.wait(remaining)
            if _failed(servers):
                break
    except KeyboardInterrupt:
        pass
    return _shutdown(servers, out)


def main(argv: list[str] | None = None) -> int:
    return serve(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
