"""Live probe: list models, make one tiny structured call on FLASH and one on FRONTIER, report.

    uv run --frozen python -m alibi.llm.probe

Needs GEMINI_API_KEY (environment or repo-root .env). Capped at 4 real calls. Appends a
section to alibi/REPORTS/S3a.md and prints the same text. Cached answers are bypassed so
the probe always measures the live service.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from alibi.llm import config
from alibi.llm.client import CallBudget, GeminiClient, LLMError, load_api_key

REPORT = Path(__file__).resolve().parents[1] / "REPORTS" / "S3a.md"
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "word": {"type": "string", "maxLength": 20}},
    "required": ["ok", "word"],
}
PROBE_SYSTEM = "Answer with JSON only."
PROBE_USER = "Set ok to true and word to a single English word for 'ready'."


def list_models(api_key: str) -> list[str]:
    from google import genai

    client = genai.Client(api_key=api_key)
    names = []
    for m in client.models.list():
        name = getattr(m, "name", "") or ""
        names.append(name.removeprefix("models/"))
    return sorted(names)


def main() -> int:
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"## Probe ({started})", ""]
    try:
        key = load_api_key()
    except LLMError as exc:
        lines.append(f"- Key: **missing** ({exc})")
        return finish(lines, 1)

    try:
        models = list_models(key)
        lines.append(f"- Models visible on this key: {len(models)}")
        for wanted in (config.FLASH, config.FRONTIER):
            lines.append(f"  - `{wanted}`: {'listed' if wanted in models else 'NOT listed'}")
        gemini = [m for m in models if m.startswith("gemini")]
        lines.append(f"  - all gemini ids: {', '.join(gemini) if gemini else 'none'}")
    except Exception as exc:  # noqa: BLE001 - report and continue to the calls
        lines.append(f"- Model listing failed: {type(exc).__name__}: {exc}")

    budget = CallBudget(4, name="probe")
    client = GeminiClient(api_key=key, use_cache=False)
    lines.append("")
    lines.append("| model | thinking | outcome | attempts | latency s | prompt tok | output tok | thinking tok | answer |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    failures = 0
    for model in (config.FLASH, config.FRONTIER):
        t0 = time.perf_counter()
        try:
            r = client.generate(model, PROBE_SYSTEM, PROBE_USER, PROBE_SCHEMA, budget)
            dt = time.perf_counter() - t0
            u = r.usage
            lines.append(
                f"| `{model}` | {config.thinking_level(model)} | ok | {r.attempts} | {dt:.1f} | "
                f"{u.prompt_tokens} | {u.output_tokens} | {u.thinking_tokens} | `{r.data}` |"
            )
        except LLMError as exc:
            dt = time.perf_counter() - t0
            failures += 1
            lines.append(f"| `{model}` | {config.thinking_level(model)} | {type(exc).__name__} | - | {dt:.1f} | - | - | - | {exc} |")
    lines.append("")
    lines.append(f"- Real calls spent by the probe: {budget.used} of {budget.cap}; process cap {client.process_budget.cap}.")
    lines.append(f"- Log: `data/alibi/llm_log.jsonl` (hashes and token counts only).")
    return finish(lines, 1 if failures else 0)


def finish(lines: list[str], code: int) -> int:
    text = "\n".join(lines) + "\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    if not REPORT.exists():
        REPORT.write_text("# S3a: Gemini client and prompt drafts\n\n", encoding="utf-8")
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
