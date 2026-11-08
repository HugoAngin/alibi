"""Deck figures about the development data, drawn from the committed cases.

    uv run --frozen python -m agents.data.figures

Writes PNGs to agents/data/out/figures/ (16:9, 1600x900 px, light surface) and prints the numbers
behind them as one JSON object on its last line. Palette: the dataviz reference instance, first
three categorical slots validated all-pairs on the light surface; the low-contrast slot (aqua)
always carries visible labels.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from agents.data import api  # noqa: E402

OUT = Path(__file__).resolve().parent / "out" / "figures"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"          # categorical slots 1-3
SEQ = ["#f0efec", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
FAMILY_LABEL = {
    "demo": "Demo pair (Maya)", "clean": "Clean", "bundled_payment": "Bundled payment",
    "balance_vs_settlement": "Balance release vs settlement", "pending_correction": "Signed corrections",
    "duplicate_record": "Duplicate records", "missing_proof": "Missing proof",
    "self_supplied_import": "Self-supplied import", "hidden_reference": "Hidden reference",
    "injection": "Injection (copies of 1-2)", "alert": "Proactive alerts", "second_platform": "Second platform",
}


def style(ax, title: str, subtitle: str) -> None:
    fig = ax.figure
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    fig.text(0.04, 0.93, title, fontsize=22, fontweight="bold", color=INK, ha="left")
    fig.text(0.04, 0.875, subtitle, fontsize=13, color=INK2, ha="left")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=13, length=0)


def footer(fig, text: str) -> None:
    fig.text(0.04, 0.03, text, fontsize=10.5, color=INK2, ha="left")


def save(fig, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, dpi=200, facecolor=SURFACE)
    plt.close(fig)


def fig_families(cases: list[dict]) -> dict:
    counts = collections.Counter(c["family"] for c in cases)
    fams = list(dict.fromkeys(c["family"] for c in cases))          # case-number order
    labels = [FAMILY_LABEL[f] for f in fams][::-1]
    values = [counts[f] for f in fams][::-1]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.subplots_adjust(left=0.36, right=0.95, top=0.82, bottom=0.1)
    bars = ax.barh(labels, values, color=S1, height=0.62, edgecolor=SURFACE, linewidth=2)
    for b, v in zip(bars, values):
        ax.text(b.get_width() + 0.15, b.get_y() + b.get_height() / 2, str(v), va="center",
                fontsize=12, color=INK)
    ax.set_xlim(0, max(values) + 1.5)
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    style(ax, f"{len(cases)} development cases, {len(fams)} families",
          "Synthetic, one generator (seed 20261107), all below the sealed evaluation set")
    footer(fig, "Source: agents/data/out/cases. Cases in one family share a template, so they are not independent.")
    save(fig, "fig1_families.png")
    return {f: counts[f] for f in fams}


def fig_outcomes(cases: list[dict]) -> dict:
    fams = list(dict.fromkeys(c["family"] for c in cases))
    order = ["supported", "contradicted", "unresolved"]
    colors = {"supported": S1, "contradicted": S2, "unresolved": S3}
    table = {f: collections.Counter(c["gold"]["claim_status"] for c in cases if c["family"] == f) for f in fams}
    fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.subplots_adjust(left=0.36, right=0.95, top=0.76, bottom=0.1)
    ys = list(range(len(fams)))[::-1]
    left = [0] * len(fams)
    for status in order:
        widths = [table[f][status] for f in fams]
        ax.barh(ys, widths, left=left, color=colors[status], height=0.62, edgecolor=SURFACE,
                linewidth=2, label=status.capitalize())
        for y, l, w in zip(ys, left, widths):
            if w:
                ax.text(l + w / 2, y, str(w), ha="center", va="center", fontsize=11,
                        color="#ffffff" if status != "unresolved" else INK, fontweight="bold")
        left = [a + b for a, b in zip(left, widths)]
    ax.set_yticks(ys, [FAMILY_LABEL[f] for f in fams])
    ax.xaxis.set_visible(False)
    ax.spines["bottom"].set_visible(False)
    total = collections.Counter(c["gold"]["claim_status"] for c in cases)
    style(ax, "Gold claim status by family",
          f"{total['supported']} supported · {total['contradicted']} contradicted · "
          f"{total['unresolved']} unresolved (holder's explanation of the credit)")
    ax.legend(loc="lower left", bbox_to_anchor=(-0.02, 1.0), ncol=3, frameon=False, fontsize=12,
              labelcolor=INK, handlelength=1.2, borderaxespad=0.2)
    footer(fig, "Unresolved means the evidence is unavailable; contradicted means it conflicts with the claim.")
    save(fig, "fig2_outcomes.png")
    return {f: dict(table[f]) for f in fams}


def fig_hidden_reference(cases: list[dict]) -> dict:
    from agents.data.test_ledgers import naive_regex_probe
    groups = {"Cases 1-40\nreference written out": [c for c in cases if c["case_number"] <= 40],
              "Cases 41-50\nreference hidden": [c for c in cases if c["family"] == "hidden_reference"]}
    probe = {g: (sum(naive_regex_probe(c) == c["gold"]["links"][0]["state"] for c in cs), len(cs))
             for g, cs in groups.items()}
    fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.subplots_adjust(left=0.1, right=0.95, top=0.70, bottom=0.21)
    xs = range(len(groups))
    width = 0.34
    for i, (name, color, vals) in enumerate([
            ("Regex retrieval, then the pinned rules", S1, probe),
            ("Pinned rules with every record retrieved (upper bound)", S2,
             {g: (len(cs), len(cs)) for g, cs in groups.items()})]):
        heights = [100 * vals[g][0] / vals[g][1] for g in groups]
        bars = ax.bar([x + (i - 0.5) * width for x in xs], heights, width=width - 0.02, color=color,
                      edgecolor=SURFACE, linewidth=2, label=name)
        for b, g in zip(bars, groups):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2, f"{vals[g][0]}/{vals[g][1]}",
                    ha="center", va="bottom", fontsize=13, color=INK, fontweight="bold")
    ax.set_xticks(list(xs), list(groups))
    ax.set_ylim(0, 115)
    ax.set_yticks([0, 50, 100], ["0%", "50%", "100%"])
    ax.yaxis.grid(True, color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    style(ax, "Finding the payout takes more than a regex",
          "Link state right, development cases")
    ax.legend(loc="lower left", bbox_to_anchor=(-0.02, 1.0), ncol=1, frameon=False, fontsize=11.5,
              labelcolor=INK, handlelength=1.2, borderaxespad=0.2)
    footer(fig, "Regex probe: data's guard (agents/data/test_ledgers.py), not the team's rules baseline.\n"
                "The upper bound equals gold by construction. Sealed-set results come from eval.")
    save(fig, "fig3_hidden_reference.png")
    return {g.replace("\n", " "): f"{a}/{b}" for g, (a, b) in probe.items()}


SCENARIOS = [  # (row label, predicate on a case)
    ("Paid credit, record pushed", lambda c: c["gold"]["links"][0]["state"] == "linked"
     and not c["gold"]["requires_retrieval"] and c["gold"]["claim_status"] == "supported"
     and c["family"] in ("clean", "bundled_payment", "balance_vs_settlement", "second_platform", "self_supplied_import")),
    ("Settlement only by lookup", lambda c: bool(c["gold"]["requires_retrieval"])),
    ("Pending or scheduled at booking", lambda c: (c["gold"]["answers"][c["rfcs"][0]["request_id"]]
                                                    ["q.source_of_funds"]["gap"] == "settlement_pending_at_booking")),
    ("Amount mismatch", lambda c: c["gold"]["answers"][c["rfcs"][0]["request_id"]]["q.source_of_funds"]["gap"]
     == "amount_mismatch"),
    ("Holder's breakdown wrong", lambda c: c["gold"]["claim_status"] == "contradicted"
     and c["gold"]["links"][0]["state"] == "linked"),
    ("Correction by a newer revision", lambda c: any(d["event"]["revision"] > 1 for d in c["merchant_deliveries"])),
    ("Duplicate delivery", lambda c: any(i["outcome"] != "accepted" for i in c["gold"]["ingest"])),
    ("Second bank, different question", lambda c: len(c["rfcs"]) > 1),
]


def platform(c: dict) -> str:
    issuers = {d["event"]["issuer_id"] for d in c["merchant_deliveries"]} | \
              {d["event"]["issuer_id"] for d in c["merchant_lookup"]}
    if issuers >= {api.SECOND_PLATFORM, "mock-marketplace"}:
        return "Both platforms"
    return "Second platform" if api.SECOND_PLATFORM in issuers else "Mock marketplace"


def fig_second_platform(cases: list[dict]) -> dict:
    cases = [c for c in cases if c["family"] != "injection"]          # copies of 1-2
    cols = ["Mock marketplace", "Second platform", "Both platforms"]
    col_labels = ["Mock\nmarketplace", "Second\nplatform", "Both\nplatforms"]
    grid = [[sum(1 for c in cases if platform(c) == col and pred(c)) for col in cols] for _, pred in SCENARIOS]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.subplots_adjust(left=0.40, right=0.96, top=0.70, bottom=0.1)
    top = max(max(r) for r in grid)
    for i, row in enumerate(grid):
        for j, v in enumerate(row):
            step = 0 if v == 0 else min(len(SEQ) - 1, 1 + round((len(SEQ) - 2) * (v - 1) / max(1, top - 1)))
            ax.add_patch(plt.Rectangle((j, len(grid) - 1 - i), 0.96, 0.9, color=SEQ[step], linewidth=0))
            ax.text(j + 0.48, len(grid) - 1 - i + 0.45, "–" if v == 0 else str(v), ha="center",
                    va="center", fontsize=13, color=INK if step < 4 else "#ffffff", fontweight="bold")
    ax.set_xlim(0, len(cols))
    ax.set_ylim(0, len(grid))
    ax.set_xticks([j + 0.48 for j in range(len(cols))], col_labels)
    ax.xaxis.tick_top()
    ax.set_yticks([len(grid) - 1 - i + 0.45 for i in range(len(grid))], [s for s, _ in SCENARIOS])
    for s in ax.spines.values():
        s.set_visible(False)
    n_sp = sum(1 for c in cases if platform(c) != "Mock marketplace")
    style(ax, "Same checks, different schema",
          f"Development cases per scenario and platform ({n_sp} use the second platform)")
    ax.title.set_visible(False)
    footer(fig, "Second platform: weekly statements, paid/scheduled, its own references and field names.\n"
                "Injection copies of cases 1-2 are excluded.")
    save(fig, "fig4_second_platform.png")
    return {s: dict(zip(cols, row)) for (s, _), row in zip(SCENARIOS, grid)}


def main() -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    cases = api.load_cases()
    out = {"families": fig_families(cases), "outcomes": fig_outcomes(cases),
           "hidden_reference_regex_probe": fig_hidden_reference(cases),
           "second_platform_coverage": fig_second_platform(cases)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
