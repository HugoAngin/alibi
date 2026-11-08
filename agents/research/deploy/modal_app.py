"""The Alibi backend on Modal (web/docs/hosting.md).

One CPU container runs S5's whole demo stack on loopback (``python -m alibi.demo``: holder app
8100, mock merchant 8101, mock bank 8102) and publishes a single HTTPS address through
``agents/research/deploy/router.py``: ``/merchant/*`` goes to the mock merchant, ``/bank/*`` to the
mock bank, everything else to the app. Internal routes (admin, seed, reset, debug) are refused by
the router before any upstream call.

Deploy from the repo root, with Modal signed in (``modal setup``):

    ALIBI_MODAL_WARM=1 uvx --from modal modal deploy agents/research/deploy/modal_app.py

Two Modal Secrets must exist in the workspace (``SECRETS`` below):

* ``alibi-backend``: ``GEMINI_API_KEY`` and ``ALIBI_MASTER_KEY``.
* ``alibi-backend-tokens``: the credentials the Workers share:
  ``ALIBI_HOLDER_TOKEN`` (the Alibi Worker's holder bearer) and ``ALIBI_BANK_A_TOKEN`` (the bank
  Worker's ``BANK_A_TOKEN``). The S5 services read both from the environment, which wins over
  their tokens file; every other token is random per container.
* ``ALIBI_MODAL_WARM=1`` keeps one container running, so the case's SQLite store persists between
  requests (it lives in the container's /tmp and resets when the container restarts).

The stack starts case 1 fresh at a clock inside both of its RFC windows (``ALIBI_DEMO_NOW``,
default 2026-11-05T09:00:00Z), with the passport and the bank on the same instant (eval,
agents/eval/CASE_FORMAT.md). Bank B's request is issued at boot so Maya's dashboard has one to
review; Bank A's is left for the bank console's "Request customer's alibi" button.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path

import modal

# The repo root on the deploying machine. Inside the container this file sits
# at /root/modal_app.py with no repo above it, and the local paths below are
# only read at deploy time, so the container gets a placeholder.
REPO = Path(__file__).resolve().parents[3] if modal.is_local() else Path("/")
REMOTE = "/root/repo"
STATE = "/tmp/alibi"

DEMO_NOW = os.environ.get("ALIBI_DEMO_NOW", "2026-11-05T09:00:00Z")
APP_PORT, MERCHANT_PORT, BANK_PORT, ROUTER_PORT = 8100, 8101, 8102, 8000
TOKENS_FILE = f"{STATE}/bank_tokens.json"

#: S5's one-process demo stack (alibi/RUNBOOK.md), seeded with case 1 at DEMO_NOW.
STACK = [
    "python", "-m", "alibi.demo",
    "--case", "1", "--reset", "--as-of", DEMO_NOW, "--host", "127.0.0.1",
    "--app-port", str(APP_PORT), "--merchant-port", str(MERCHANT_PORT), "--bank-port", str(BANK_PORT),
    "--root", f"{STATE}/demo", "--keys", f"{STATE}/keys", "--tokens", TOKENS_FILE,
]
#: Requests issued once the stack is up.
BOOT_RFCS = [r for r in os.environ.get("ALIBI_DEMO_RFCS", "bank_b").split(",") if r]

# Only tracked source and public synthetic fixtures go into the image: never
# .env files, keys, local databases or scratch data.
IGNORE = [
    "**/.env",
    "**/.env.*",
    "**/*.db",
    "**/*.key",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/node_modules",
    "**/data/**",
]

image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(uv_project_dir=str(REPO), frozen=True)
    .env({"PYTHONPATH": REMOTE, "ALIBI_DATA_DIR": f"{STATE}/app", "ALIBI_NOW": DEMO_NOW})
    .add_local_dir(REPO / "alibi", f"{REMOTE}/alibi", ignore=IGNORE)
    .add_local_dir(REPO / "tools", f"{REMOTE}/tools", ignore=IGNORE)
    .add_local_dir(REPO / "shared", f"{REMOTE}/shared", ignore=IGNORE)
    .add_local_dir(REPO / "agents" / "data", f"{REMOTE}/agents/data", ignore=IGNORE)
    .add_local_dir(REPO / "agents" / "execution", f"{REMOTE}/agents/execution", ignore=IGNORE)
    .add_local_dir(REPO / "agents" / "research" / "deploy", f"{REMOTE}/agents/research/deploy", ignore=IGNORE)
)

app = modal.App("alibi-backend")

# Fixed names, not read from the environment: the container re-imports this file without the
# deploy machine's environment, and its object list must match the deployed one exactly.
SECRETS = ("alibi-backend", "alibi-backend-tokens")
secrets = [modal.Secret.from_name(name) for name in SECRETS]
warm = 1 if os.environ.get("ALIBI_MODAL_WARM") == "1" else 0


def _wait_for(port: int, seconds: float = 120.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # the service answered, even if not with 200
        except OSError:
            time.sleep(0.5)
    return False


def _issue(recipient: str, attempts: int = 20) -> None:
    """Have the bank issue and forward its next case RFC, once the app has finished seeding."""
    command = [
        "python", "-m", "alibi.demo", "rfc", recipient,
        "--app-url", f"http://127.0.0.1:{APP_PORT}",
        "--merchant-url", f"http://127.0.0.1:{MERCHANT_PORT}",
        "--bank-url", f"http://127.0.0.1:{BANK_PORT}",
        "--tokens", TOKENS_FILE,
    ]
    for _ in range(attempts):
        if subprocess.run(command, cwd=REMOTE, capture_output=True).returncode == 0:
            print(f"issued the case RFC for {recipient}", flush=True)
            return
        time.sleep(3)
    print(f"could not issue the case RFC for {recipient}", flush=True)


@app.function(
    image=image,
    secrets=secrets,
    cpu=1.0,
    memory=2048,
    min_containers=warm,
    max_containers=1,  # one container, so one SQLite store
    scaledown_window=15 * 60,
    timeout=24 * 60 * 60,
)
@modal.concurrent(max_inputs=50)
@modal.web_server(ROUTER_PORT, startup_timeout=240)
def backend() -> None:
    os.makedirs(STATE, exist_ok=True)
    subprocess.Popen(STACK, cwd=REMOTE)
    for name, port in (("app", APP_PORT), ("merchant", MERCHANT_PORT), ("bank", BANK_PORT)):
        if not _wait_for(port):
            raise RuntimeError(f"{name} did not start on port {port}")
    for recipient in BOOT_RFCS:
        _issue(recipient)
    subprocess.Popen(
        ["python", "-m", "uvicorn", "agents.research.deploy.router:app", "--host", "0.0.0.0", "--port", str(ROUTER_PORT)],
        cwd=REMOTE,
    )
