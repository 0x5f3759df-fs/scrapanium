#!/usr/bin/env python3
"""Render README benchmark figures from the published raw measurements.

Requires matplotlib (tested with 3.11.2). Run from any directory:
    python scripts/render_readme_charts.py
    python scripts/render_readme_charts.py --font build/Archivo.ttf

An optional Archivo variable font is instantiated at 500/750 weight with
Matplotlib's fontTools dependency. Without it, use bundled DejaVu Sans.
SVG lettering is outlined; exported images never require an installed font.
No benchmarks are run and no input measurements are modified.
"""

import argparse
import json
from pathlib import Path
from statistics import median
import tempfile
import xml.etree.ElementTree as ET

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
INK = "#211b2f"
PAPER = "#fff4e9"
ORANGE = "#ef8446"
LAVENDER = "#b5a5c3"
MUTED = "#63586b"
RULE = "#ded2cf"
WORKLOADS = [
    ("http1-small", "HTTP/1.1", "Small response"),
    ("http1-64KiB", "HTTP/1.1", "64 KiB response"),
    ("https1-small", "HTTPS/1.1", "Small response"),
    ("https2-small", "HTTPS/2", "Small response"),
    ("http1-batch16-delay10ms", "HTTP/1.1 · batch of 16", "10 ms server delay"),
    ("https2-batch16", "HTTPS/2 · batch of 16", "Small responses"),
]


def read(name):
    return json.loads((ROOT / "benchmarks" / "results" / name).read_text())


def font_pair(path, directory):
    if path is None:
        return FontProperties(family="DejaVu Sans"), FontProperties(
            family="DejaVu Sans", weight="bold"
        )
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    result = []
    for weight in (500, 750):
        source = TTFont(path)
        if "fvar" in source:
            axes = {axis.axisTag: axis.defaultValue for axis in source["fvar"].axes}
            if "wght" in axes:
                axes["wght"] = weight
            source = instantiateVariableFont(source, axes, inplace=True)
        destination = Path(directory) / f"chart-{weight}.ttf"
        source.save(destination)
        result.append(FontProperties(fname=str(destination)))
    return result


def label(fig, x, y, text, size=14, bold=False, color=INK, **kwargs):
    return fig.text(
        x, y, text, fontsize=size, color=color,
        fontproperties=FONTS[1 if bold else 0], **kwargs
    )


def canvas(width, height):
    return plt.figure(figsize=(width / 100, height / 100), facecolor=PAPER)


def axes(fig, bounds, maximum, ticks, tick_size=12):
    ax = fig.add_axes(bounds, facecolor=PAPER)
    ax.set_xlim(0, maximum)
    ax.set_xticks(ticks, [f"{tick:,}" for tick in ticks])
    ax.tick_params(axis="x", colors=MUTED, labelsize=tick_size, length=0, pad=9)
    for item in ax.get_xticklabels():
        item.set_fontproperties(FONTS[0])
        item.set_fontsize(tick_size)
    ax.set_yticks([])
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=RULE, linewidth=0.7)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.axvline(0, color=MUTED, linewidth=0.9)
    return ax


def legend(fig, y, mobile=False):
    positions = [(0.06, "Scrapanium · Bend", ORANGE),
                 (0.51 if mobile else 0.35, "curl_cffi · matched", LAVENDER)]
    for x, text, color in positions:
        fig.add_artist(Rectangle((x, y - .007), .022, .013,
                                 transform=fig.transFigure, color=color, linewidth=0))
        label(fig, x + .035, y, text, size=11.4 if mobile else 13, va="center")


def save(fig, stem, description):
    ASSETS.mkdir(parents=True, exist_ok=True)
    fig.savefig(ASSETS / f"{stem}.png", dpi=200,
                metadata={"Description": description,
                          "Software": "scripts/render_readme_charts.py / matplotlib"})
    path = ASSETS / f"{stem}.svg"
    fig.savefig(path, metadata={"Date": None, "Description": description,
                               "Creator": "scripts/render_readme_charts.py / matplotlib"})
    # Preserve Matplotlib's vector plot while exposing a useful accessible name.
    text = path.read_text()
    opening_end = text.index(">", text.index("<svg"))
    desc_element = ET.Element("desc", {"id": f"{stem}-description"})
    desc_element.text = description
    description_xml = ET.tostring(desc_element, encoding="unicode")
    text = (text[:opening_end] + f' role="img" aria-labelledby="{stem}-description"'
            + text[opening_end:opening_end + 1] + "\n" + description_xml
            + text[opening_end + 1:])
    path.write_text("\n".join(line.rstrip() for line in text.splitlines()) + "\n")
    plt.close(fig)
    print(path.relative_to(ROOT))


def http_chart(data, mobile=False):
    records = {(item["workload"], item["client"]): item for item in data["results"]}
    values = [(records[(key, "scrapanium-bend")]["requests_per_second"],
               records[(key, "curl_cffi-matched")]["requests_per_second"])
              for key, _, _ in WORKLOADS]
    description = (
        "Median warm HTTP throughput in requests per second. Scrapanium Bend versus "
        "matched curl_cffi, both using curl-impersonate 2.2.3 and Chrome 146. "
        + "; ".join(f"{a}, {b}: {v[0]:,.0f} versus {v[1]:,.0f}, {v[0]/v[1]:.2f} times"
                    for (_, a, b), v in zip(WORKLOADS, values))
        + ". Five shuffled runs on Linux/WSL2, i5-13420H. Local loopback only. "
        "Source: benchmarks/results/local.json."
    )
    if mobile:
        fig = canvas(480, 1110)
        label(fig, .06, .956, "Warm HTTP throughput", 21.5, True)
        label(fig, .06, .928, "Requests/s · higher is better", 13.2, color=MUTED)
        legend(fig, .890, mobile=True)
        for i, ((_, title, detail), (bend, baseline)) in enumerate(zip(WORKLOADS, values)):
            top = .833 - i * .119
            label(fig, .06, top, title, 14.2, True)
            label(fig, .06, top - .021, detail, 12, color=MUTED)
            label(fig, .93, top, f"{bend / baseline:.2f}×", 15, True, ha="right")
            ax = axes(fig, [.06, top - .091, .88, .058], 8200, [0, 2000, 4000, 6000], 10.5)
            ax.set_ylim(-.45, 1.45)
            ax.barh([1, 0], [bend, baseline], color=[ORANGE, LAVENDER], height=.68)
            for row, value in zip([1, 0], [bend, baseline]):
                ax.text(value + 130, row, f"{value:,.0f}", va="center", color=INK,
                        fontsize=13, fontproperties=FONTS[1])
            if i < 5:
                ax.set_xticklabels([])
                ax.tick_params(axis="x", pad=0)
        label(fig, .06, .071, "Multipliers compare Bend with curl_cffi.", 11.3, color=MUTED)
        label(fig, .06, .049, "5 shuffled runs · Chrome 146 · same backend", 11.3, color=MUTED)
        label(fig, .06, .027, "Linux/WSL2 · local loopback measurements", 11.3, color=MUTED)
    else:
        fig = canvas(1000, 790)
        label(fig, .06, .933, "Warm HTTP throughput", 27, True)
        label(fig, .06, .886, "Requests/s · higher is better", 15, color=MUTED)
        legend(fig, .831)
        label(fig, .94, .831, "Bend / curl_cffi", 12, color=MUTED, ha="right")
        plot_bottom, plot_height = .178, .588
        ax = axes(fig, [.29, plot_bottom, .59, plot_height], 7600,
                  [0, 2000, 4000, 6000], 12)
        ax.set_ylim(-.5, 5.5)
        for i, ((_, title, detail), (bend, baseline)) in enumerate(zip(WORKLOADS, values)):
            row = 5 - i
            y = plot_bottom + plot_height * ((row + .5) / 6)
            ax.barh([row + .17, row - .17], [bend, baseline],
                    color=[ORANGE, LAVENDER], height=.27)
            label(fig, .06, y + .009, title, 14.2, True)
            label(fig, .06, y - .020, detail, 12.2, color=MUTED)
            label(fig, .94, y, f"{bend / baseline:.2f}×", 15, True, ha="right", va="center")
            for offset, value in [(.17, bend), (-.17, baseline)]:
                ax.text(value + 95, row + offset, f"{value:,.0f}", va="center",
                        color=INK, fontsize=12.8, fontproperties=FONTS[1])
        label(fig, .06, .086, "5 shuffled runs · Chrome 146 · same curl-impersonate 2.2.3 backend", 12, color=MUTED)
        label(fig, .06, .052, "Linux/WSL2 · i5-13420H · local loopback · startup and warmup excluded", 12, color=MUTED)
    save(fig, "performance-http" + ("-mobile" if mobile else ""), description)


def compact_chart(stem, title, subtitle, rows, maximum, ticks, notes, description, mobile=False):
    fig = canvas(480 if mobile else 1000, 495 if mobile else 440)
    label(fig, .06, .892, title, 21 if mobile else 27, True)
    label(fig, .06, .828, subtitle, 12.5 if mobile else 15, color=MUTED)
    ax = axes(fig, [.06, .288, .87 if mobile else .88, .413], maximum, ticks, 11.5 if mobile else 13)
    ax.set_ylim(-.23, 2.45)
    for row, (name, value, formatted, color) in zip([1.6, .15], rows):
        ax.barh(row, value, height=.52, color=color)
        ax.text(0, row + .40, name, color=INK,
                fontsize=14.5 if mobile else 16,
                fontproperties=FONTS[0], va="bottom")
        ax.text(value + maximum * .018, row, formatted, color=INK,
                fontsize=14.5 if mobile else 17,
                fontproperties=FONTS[1], va="center")
    for i, text in enumerate(notes):
        label(fig, .06, .143 - i * .046, text, 11.2 if mobile else 12.7, color=MUTED)
    save(fig, stem + ("-mobile" if mobile else ""), description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, help="Optional local Archivo TTF")
    args = parser.parse_args()
    matplotlib.rcParams.update({"svg.fonttype": "path", "svg.hashsalt": "scrapanium-benchmarks",
                                "axes.unicode_minus": False})
    with tempfile.TemporaryDirectory(prefix="scrapanium-chart-fonts-") as directory:
        global FONTS
        FONTS = font_pair(args.font, directory)
        local, websocket, resources = read("local.json"), read("websocket.json"), read("resources.json")
        clients = websocket["clients"]
        bend = clients["scrapanium-bend"]["round_trips_per_second"]
        baseline = clients["curl_cffi-matched"]["round_trips_per_second"]
        memory = {mode: median(item["peak_rss_kib"] / 1024 for item in resources["results"]
                               if item["body_bytes"] == 64 * 1024**2 and item["mode"] == mode)
                  for mode in ("download", "buffer")}
        for mobile in (False, True):
            http_chart(local, mobile)
            compact_chart(
                "performance-wss", "WSS: a near tie", "Round trips/s · higher is better",
                [("Scrapanium · Bend", bend, f"{bend:,.0f}", ORANGE),
                 ("curl_cffi · matched", baseline, f"{baseline:,.0f}", LAVENDER)],
                7400, [0, 2000, 4000, 6000],
                (["30-byte payload · 1,000 warm round trips",
                  "5 runs · same backend · setup excluded",
                  "Loopback only; Python also checks payloads."] if mobile else
                 ["30-byte payload · 1,000 warm round trips × 5 runs · Chrome 146 · same backend",
                  "Loopback WSS · setup excluded · Python also compares returned payloads"]),
                f"Warm WSS median round trips per second: Scrapanium Bend {bend:.0f}; "
                f"matched curl_cffi {baseline:.0f}. Five shuffled runs of 1,000 round trips, "
                "30-byte binary payload. Both curl-impersonate 2.2.3, Chrome 146. "
                "Python also compares returned payloads. Linux/WSL2 loopback only; startup, "
                "TLS/HTTP upgrade and close excluded. Source: benchmarks/results/websocket.json.", mobile)
            compact_chart(
                "performance-memory", "Stream a 64 MiB body to file", "Whole-process peak RSS (MiB) · lower is better",
                [("Download to file", memory["download"], f'{memory["download"]:.1f} MiB', ORANGE),
                 ("Buffer in memory", memory["buffer"], f'{memory["buffer"]:.1f} MiB', LAVENDER)],
                89, [0, 20, 40, 60, 80],
                (["Same 64 MiB HTTP/1 response · Bend",
                  "Median of 3 runs · Linux temporary file",
                  "Observed RSS, not a process memory cap."] if mobile else
                 ["Same 64 MiB HTTP/1 response · Scrapanium Bend · median of 3 runs",
                  "Download writes a Linux temporary file · observed RSS, not a process memory cap"]),
                f"Median whole-process peak resident memory for the same 64 MiB HTTP/1 body: "
                f'download to file {memory["download"]:.1f} MiB; buffered {memory["buffer"]:.1f} MiB. '
                "Three shuffled runs in Bend on Linux/WSL2. Total process RSS, not incremental "
                "memory or a process memory cap. File output and buffering differ in semantics; "
                "this is not a throughput comparison. Source: benchmarks/results/resources.json.", mobile)


if __name__ == "__main__":
    main()
