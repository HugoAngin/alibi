"""Modal entry point for Elio's existing alibi-backend URL. Deploy from a public stage."""

import os
from pathlib import Path
import subprocess
import sys

import modal

REPO = Path(__file__).resolve().parents[3] if modal.is_local() else Path("/")
if modal.is_local() and (REPO / ".git").exists():
    raise RuntimeError("Deploy from the public staging directory, not a working tree")
REMOTE = "/root/repo"
PORT = 8000
MODE = os.environ.get("ALIBI_MODE", "rules")
REPLAY = os.environ.get("ALIBI_REPLAY", "0")
required = ["ALIBI_HOLDER_TOKEN", "ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_MASTER_KEY"]
if MODE != "rules" and REPLAY != "1":
    required.append("GEMINI_API_KEY")

image = (modal.Image.debian_slim(python_version="3.12")
         .uv_sync(uv_project_dir=str(REPO), frozen=True)
         .env({"PYTHONPATH": REMOTE, "PORT": str(PORT), "ALIBI_ROUTER_BIND": "0.0.0.0",
               "ALIBI_MODE": MODE, "ALIBI_REPLAY": REPLAY,
               "ALIBI_DATA_DIR": "/tmp/alibi-llm"}))
for path in ("alibi", "agents/data", "agents/execution", "agents/eval/deploy", "tools", "shared"):
    image = image.add_local_dir(REPO / path, f"{REMOTE}/{path}", ignore=["**/__pycache__/**"])

app = modal.App("alibi-backend")
secret = modal.Secret.from_name(os.environ.get("ALIBI_MODAL_SECRET", "alibi-backend"), required_keys=required)


@app.function(image=image, secrets=[secret], cpu=1.0, memory=1024,
              min_containers=int(os.environ.get("ALIBI_MODAL_WARM", "0") == "1"),
              max_containers=1, scaledown_window=15 * 60, timeout=24 * 60 * 60)
@modal.concurrent(max_inputs=50)
@modal.web_server(PORT, startup_timeout=90)
def backend() -> None:
    subprocess.Popen([sys.executable, "-m", "agents.eval.deploy.s5_host"], cwd=REMOTE)
