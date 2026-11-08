"""Boot the public synthetic S5 stack, then open its hosted router."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import uvicorn

from agents.eval.deploy.router import build_app

CLOCK = "2026-11-08T10:00:00Z"
REQUIRED = ("ALIBI_HOLDER_TOKEN", "ALIBI_BANK_A_TOKEN", "ALIBI_BANK_B_TOKEN", "ALIBI_MASTER_KEY")


def _wait_seeded(stack: subprocess.Popen, log: Path) -> None:
    deadline = monotonic() + 45
    while stack.poll() is None and monotonic() < deadline:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seeds = row.get("seeds") if isinstance(row, dict) else None
            if not isinstance(seeds, dict):
                continue
            merchant = seeds.get("merchant_outcomes") or {}
            if (seeds.get("merchant_pushed", 0) > 0 and not merchant.get("error")
                    and not merchant.get("refused") and not merchant.get("not_delivered")
                    and seeds["bank"].get("ok") and seeds["feed"].get("ok")):
                return
            raise SystemExit("S5 connector or feed seed failed")
        sleep(0.1)
    raise SystemExit("S5 did not finish seeding before router startup")


def main() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(f"Missing hosted settings: {', '.join(missing)}")
    if len({os.environ[name] for name in REQUIRED[:3]}) != 3:
        raise SystemExit("Holder and bank credentials must be distinct")

    command = [sys.executable, "-m", "alibi.demo"]
    env = {**os.environ, "ALIBI_NOW": CLOCK}
    with TemporaryDirectory(prefix="alibi-hosted-") as work:
        root = Path(work)
        tokens = str(root / "bank_tokens.json")
        log = root / "startup.log"
        with log.open("wb") as output:
            stack = subprocess.Popen(
                [*command, "--case", "1", "--as-of", CLOCK, "--root", str(root / "state"),
                 "--keys", str(root / "keys"), "--tokens", tokens],
                env=env, stdout=output, stderr=subprocess.STDOUT,
            )
            try:
                _wait_seeded(stack, log)
                for recipient in ("bank_a", "bank_b"):
                    result = subprocess.run(
                        [*command, "rfc", recipient, "--tokens", tokens],
                        env=env, capture_output=True, timeout=15,
                    )
                    if result.returncode:
                        raise SystemExit(f"S5 did not issue and forward the {recipient} RFC")
                print("S5 seeded Case 1 and forwarded both bank requests", flush=True)
                uvicorn.run(build_app(hosted=True), host=os.environ.get("ALIBI_ROUTER_BIND", "127.0.0.1"),
                            port=int(os.environ.get("PORT", "8000")), access_log=False)
            finally:
                if stack.poll() is None:
                    stack.terminate()
                    try:
                        stack.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        stack.kill()
                        stack.wait()


if __name__ == "__main__":
    main()
