"""The Alibi backend on Modal (web/docs/hosting.md).

One CPU container runs the backend services on loopback and publishes a
single HTTPS address through ``agents/research/deploy/router.py``:
``/merchant/*`` goes to the mock merchant, ``/bank/*`` to the mock bank,
everything else to the app. This follows eval's topology
(``agents/eval/out/MODAL_REPLAY.md``), but runs live.

Deploy from the repo root, with Modal signed in (``modal setup``):

    uvx --from modal modal deploy agents/research/deploy/modal_app.py

Optional at deploy time:

* ``ALIBI_MODAL_SECRET=alibi-backend`` attaches that Modal Secret (created in
  the Modal dashboard) holding ``GEMINI_API_KEY`` and ``ALIBI_MASTER_KEY``.
* ``ALIBI_MODAL_WARM=1`` keeps one container running (judging window), so the
  container's SQLite store persists between requests.

``SERVICES`` is empty until S5 publishes the start commands in
``alibi/RUNBOOK.md``. Until then the deploy is a health check that proves the
account, image and URL work; every proxied route answers 503.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[3]
REMOTE = "/root/repo"

#: (name, port, command) for each backend service, from S5's alibi/RUNBOOK.md.
#: Commands run from the repo root inside the container.
SERVICES: list[tuple[str, int, list[str]]] = []

ROUTER_PORT = 8000

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
    .env({"PYTHONPATH": REMOTE, "ALIBI_DATA_DIR": "/tmp/alibi"})
    .add_local_dir(REPO / "alibi", f"{REMOTE}/alibi", ignore=IGNORE)
    .add_local_dir(REPO / "tools", f"{REMOTE}/tools", ignore=IGNORE)
    .add_local_dir(REPO / "shared", f"{REMOTE}/shared", ignore=IGNORE)
    .add_local_dir(REPO / "agents" / "data", f"{REMOTE}/agents/data", ignore=IGNORE)
    .add_local_dir(REPO / "agents" / "research" / "deploy", f"{REMOTE}/agents/research/deploy", ignore=IGNORE)
)

app = modal.App("alibi-backend")

secret_name = os.environ.get("ALIBI_MODAL_SECRET")
secrets = [modal.Secret.from_name(secret_name)] if secret_name else []
warm = 1 if os.environ.get("ALIBI_MODAL_WARM") == "1" else 0


def _wait_for(port: int, seconds: float = 45.0) -> bool:
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


@app.function(
    image=image,
    secrets=secrets,
    cpu=1.0,
    memory=1024,
    min_containers=warm,
    max_containers=1,  # one container, so one SQLite store
    scaledown_window=15 * 60,
    timeout=24 * 60 * 60,
)
@modal.concurrent(max_inputs=50)
@modal.web_server(ROUTER_PORT, startup_timeout=90)
def backend() -> None:
    for name, port, command in SERVICES:
        subprocess.Popen(command, cwd=REMOTE)
        if not _wait_for(port):
            raise RuntimeError(f"{name} did not start on port {port}")
    subprocess.Popen(
        ["python", "-m", "uvicorn", "agents.research.deploy.router:app", "--host", "0.0.0.0", "--port", str(ROUTER_PORT)],
        cwd=REMOTE,
    )
