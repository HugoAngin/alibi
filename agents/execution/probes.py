"""Render out/probes.json to out/PROBES.md.

    uv run --frozen python -m agents.execution.probes
"""
from __future__ import annotations

from agents.execution.api import OUT, load_probes

PROBES_MD = OUT / "PROBES.md"


def render() -> str:
    probes = load_probes()
    core = sum(p["core"] for p in probes)
    lines = [
        "# Security probes",
        "",
        "Generated from [`probes.json`](probes.json) by `uv run --frozen python -m agents.execution.probes`. Do not edit by hand.",
        "",
        f"{len(probes)} probes, {core} core. **Refusal** probes pass when the backend refuses them; "
        "**control** probes pass when a valid action is accepted. Core probes must pass by the 00:15 gate. "
        "Demo probes are shown live. All targets are the simulated Alibi services.",
        "",
        "Report as `probes_blocked` / `probes_attempted` (refusal probes) plus controls allowed, using "
        "`agents.execution.api.summarise`.",
        "",
    ]
    area = None
    for p in probes:
        if p["area"] != area:
            area = p["area"]
            lines += [f"## {area.capitalize()}", "", "| | ID | Actor | Attempt | Must happen | Audit |", "| --- | --- | --- | --- | --- | --- |"]
        tags = []
        if p["core"]:
            tags.append("core")
        if p["demo"]:
            tags.append("demo")
        if p["kind"] == "control":
            tags.append("control")
        cell = lambda s: s.replace("|", "\\|")
        lines.append(
            f"| [ ] {' '.join(tags)} | {p['id']} | {p['actor']} | {cell(p['attack'])} | "
            f"{cell(p['expected'])} | {cell(p['audit'])} |"
        )
        nxt = probes[probes.index(p) + 1] if p is not probes[-1] else None
        if nxt is None or nxt["area"] != area:
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    PROBES_MD.write_text(render(), encoding="utf-8")
    print(f"wrote {PROBES_MD.relative_to(OUT.parent.parent.parent)}")


if __name__ == "__main__":
    main()
