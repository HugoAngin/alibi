"""Serve the Alibi app: ``uv run --frozen python -m alibi.app --timeout 10``.

``--timeout`` stops the server after that many seconds (for smoke tests). Without it the server
runs until interrupted. Settings come from the environment (see ``alibi/app/config.py``).
"""

from __future__ import annotations

import argparse
import threading

import uvicorn

from alibi.app.main import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m alibi.app", description="Run the Alibi demo app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--timeout", type=float, default=None,
                        help="stop after this many seconds (smoke tests)")
    args = parser.parse_args(argv)
    try:
        application = create_app()
    except ValueError as exc:
        print(f"configuration error: {exc}", flush=True)
        return 2
    config = uvicorn.Config(application, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    if args.timeout:
        def stop() -> None:
            server.should_exit = True
        threading.Timer(args.timeout, stop).start()
    print(f"Alibi app on http://{args.host}:{args.port} (health: /health, passport: /passport, "
          f"bank inbox: /bank/bank_a/inbox?token=<credential>)", flush=True)
    server.run()
    application.state.runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
