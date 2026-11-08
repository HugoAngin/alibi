"""Set every credential the hosted live demo needs, in one run, without printing a value.

    uv run --frozen python -m agents.research.deploy.set_secrets --dry-run    # names only, changes nothing
    uv run --frozen python -m agents.research.deploy.set_secrets              # rotate and set
    uv run --frozen python -m agents.research.deploy.set_secrets --redeploy   # ... then deploy Modal and the 3 Workers

What it writes:

* Modal secret ``alibi-backend-tokens``: ``ALIBI_HOLDER_TOKEN``, ``ALIBI_BANK_A_TOKEN``,
  ``ALIBI_DEMO_TOKEN``. ``modal secret create --force`` replaces the whole secret and neither
  Modal nor Cloudflare can read a secret back, so all three are generated fresh together and set
  on both sides in the same run.
* Worker ``www``: ``ALIBI_HOLDER_TOKEN``, ``ALIBI_DEMO_TOKEN``, ``ALIBI_DEMO_ORIGIN``,
  ``ALIBI_SIMULATE_ACTIONS=1`` (anyone on /simulate/ can act as Maya and reset the demo).
* Worker ``bank``: ``BANK_A_TOKEN`` (the same value as ``ALIBI_BANK_A_TOKEN``),
  ``ALIBI_DEMO_TOKEN``, ``ALIBI_DEMO_ORIGIN``.
* Worker ``merchant``: ``ALIBI_DEMO_TOKEN``, ``ALIBI_DEMO_ORIGIN``.

Left alone: Modal ``alibi-backend`` (``GEMINI_API_KEY``, ``ALIBI_MASTER_KEY``), the bank's
``ANALYST_PASSCODE`` and ``BANK_SESSION_SECRET``, and Clerk's keys on ``www``.

Values go to Modal through a 0600 JSON file in a private temporary directory that is deleted
afterwards, and to Wrangler as JSON on stdin (``wrangler secret bulk``), so none is ever on a
command line, in the repo, or in the output. Needs ``modal`` (via uvx) and ``wrangler`` signed in.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WEB = REPO / "web"
MODAL_SECRET = "alibi-backend-tokens"
DEFAULT_ORIGIN = "https://eliohd--alibi-backend-backend.us-east.modal.run"
#: Worker name -> (app folder under web/apps, pnpm package for --redeploy)
WORKERS = {"www": ("alibi", "@alibi/site"), "bank": ("bank", "@alibi/bank"),
           "merchant": ("merchant", "@alibi/merchant")}


@dataclass(frozen=True)
class Plan:
    modal: dict[str, str]
    workers: dict[str, dict[str, str]]


def plan(origin: str, token=lambda: secrets.token_urlsafe(32)) -> Plan:
    """Fresh tokens and where each one goes. Pure apart from ``token``, so it is unit tested."""
    holder, bank_a, demo = token(), token(), token()
    shared = {"ALIBI_DEMO_TOKEN": demo, "ALIBI_DEMO_ORIGIN": origin.rstrip("/")}
    return Plan(
        modal={"ALIBI_HOLDER_TOKEN": holder, "ALIBI_BANK_A_TOKEN": bank_a, "ALIBI_DEMO_TOKEN": demo},
        workers={
            "www": {"ALIBI_HOLDER_TOKEN": holder, **shared, "ALIBI_SIMULATE_ACTIONS": "1"},
            "bank": {"BANK_A_TOKEN": bank_a, **shared},
            "merchant": dict(shared),
        },
    )


def describe(p: Plan) -> list[str]:
    """What a run sets, by name only."""
    lines = [f"modal secret {MODAL_SECRET}: {', '.join(sorted(p.modal))}"]
    lines += [f"worker {name}: {', '.join(sorted(values))}" for name, values in p.workers.items()]
    return lines


def _run(cmd: list[str], *, cwd: Path, stdin: str | None = None, env: dict | None = None) -> None:
    """Run a step; on failure show its output (the CLIs never echo secret values) and stop."""
    result = subprocess.run(cmd, cwd=cwd, input=stdin, text=True, capture_output=True,
                            env={**os.environ, **(env or {})})
    if result.returncode != 0:
        sys.stderr.write((result.stdout + result.stderr)[-2000:])
        raise SystemExit(f"failed: {' '.join(cmd[:4])} ... (exit {result.returncode})")


def set_modal(values: dict[str, str]) -> None:
    folder = Path(tempfile.mkdtemp(prefix="alibi-secrets-"))
    try:
        path = folder / "tokens.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(values, fh)
        _run(["uvx", "--from", "modal", "modal", "secret", "create", MODAL_SECRET,
              "--from-json", str(path), "--force"], cwd=REPO)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def set_worker(name: str, values: dict[str, str]) -> None:
    app = WEB / "apps" / WORKERS[name][0]
    _run(["pnpm", "exec", "wrangler", "secret", "bulk", "--name", name], cwd=app,
         stdin=json.dumps(values))


def missing_build_env() -> str | None:
    """www's build bakes Clerk's publishable key in from web/apps/alibi/.env (git-ignored, so a
    fresh checkout or worktree has none); without it every www page answers 500."""
    env = WEB / "apps" / "alibi" / ".env"
    return None if env.exists() else (
        f"{env} is missing, so a www build would have no Clerk key and every page would fail. "
        "Copy it from the checkout that has it (or run from there), then rerun.")


def redeploy() -> None:
    print("deploying Modal alibi-backend (warm, one container)", flush=True)
    _run(["uvx", "--from", "modal", "modal", "deploy", "agents/research/deploy/modal_app.py"],
         cwd=REPO, env={"ALIBI_MODAL_WARM": "1"})
    for name, (_, package) in WORKERS.items():
        print(f"deploying worker {name}", flush=True)
        _run(["pnpm", "--filter", package, "run", "deploy"], cwd=WEB)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.research.deploy.set_secrets", description=__doc__.split("\n\n")[0])
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help=f"the Modal address (default {DEFAULT_ORIGIN})")
    parser.add_argument("--dry-run", action="store_true", help="print the names that would be set; change nothing")
    parser.add_argument("--redeploy", action="store_true", help="afterwards, deploy Modal and the three Workers")
    args = parser.parse_args(argv)

    problem = missing_build_env() if args.redeploy else None
    if problem:
        raise SystemExit(problem)          # before anything changes
    p = plan(args.origin)
    for line in describe(p):
        print(("would set " if args.dry_run else "setting ") + line, flush=True)
    if args.dry_run:
        return 0
    set_modal(p.modal)
    print(f"modal secret {MODAL_SECRET}: set", flush=True)
    for name, values in p.workers.items():
        set_worker(name, values)
        print(f"worker {name}: set", flush=True)
    if args.redeploy:
        redeploy()
    else:
        print("Modal picks the new tokens up on its next deploy or container restart; run with "
              "--redeploy (or redeploy by hand) so the backend and the Workers agree.", flush=True)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
