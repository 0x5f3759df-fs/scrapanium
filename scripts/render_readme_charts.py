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
import math
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
ORANGE = "#d96528"
LAVENDER = "#776187"
BUFFER = "#393243"
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


def legend(fig, y, mobile=False, items=None, mobile_step=.036):
    items = items or [("Scrapanium (Bend)", ORANGE), ("curl_cffi (Python)", LAVENDER)]
    for i, (text, color) in enumerate(items):
        x = .06 if mobile else .06 + i * .39
        row = y - i * mobile_step if mobile else y
        fig.add_artist(Rectangle((x, row - .009), .026, .018,
                                 transform=fig.transFigure, color=color, linewidth=0))
        label(fig, x + .043, row, text, size=15 if mobile else 16.5, bold=True, va="center")


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
            top = .805 - i * .116
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
    fig = canvas(480 if mobile else 1000, 610 if mobile else 510)
    label(fig, .06, .916, title, 21 if mobile else 27, True)
    label(fig, .06, .852, subtitle, 12.5 if mobile else 15, color=MUTED)
    legend(fig, .784, mobile, [(name, color) for name, _, _, color in rows], mobile_step=.052)
    ax = axes(fig, [.06, .268, .87 if mobile else .88, .347], maximum, ticks, 11.5 if mobile else 13)
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


def wss_chart(data, mobile=False, streaming=False):
    """Show every measured workload, including the retained Python-peer case."""
    if data.get("smoke_only"):
        raise ValueError("Smoke runs are correctness checks, not chart data")
    workloads = data["workloads"]
    keys = ("scrapanium-bend", "curl_cffi-matched")
    rate_key = "messages_per_second" if streaming else "round_trips_per_second"
    low_key, high_key = ("mps_min", "mps_max") if streaming else ("rps_min", "rps_max")
    peak = max(w["clients"][key][high_key] for w in workloads for key in keys)
    scale = 10 ** math.floor(math.log10(peak))
    step = next(x * scale for x in (.2, .5, 1, 2) if peak / (x * scale) <= 5)
    maximum = math.ceil(peak * 1.25 / step) * step
    ticks = list(range(0, int(maximum + 1), int(step)))
    count = len(workloads)
    has_inconclusive = any(
        w.get("paired_bend_speed_ratio", {}).get("bootstrap_95_ci", [2, 2])[0] <= 1
        <= w.get("paired_bend_speed_ratio", {}).get("bootstrap_95_ci", [2, 2])[1]
        for w in workloads
    )
    fig = canvas(480 if mobile else 1000, 390 + count * 132 if mobile else 330 + count * 89)
    title = "TLS WebSocket streaming" if streaming else "TLS WebSocket round trips"
    units = "Inbound messages/s" if streaming else "Round trips/s"
    label(fig, .06, .95 if mobile else .932, title, 21 if mobile else 27, True)
    label(fig, .06, .925 if mobile else .891, f"{units} · higher is better", 12.5 if mobile else 15, color=MUTED)
    legend(fig, .886 if mobile else .838, mobile, mobile_step=.031)
    if not mobile:
        label(fig, .94, .838, "Bend / curl_cffi", 12, color=MUTED, ha="right")
    for i, workload in enumerate(workloads):
        peer = "Python peer" if workload.get("peer") == "python" else "Go peer"
        size = workload["bytes"]
        size_label = f"{size // 1024} KiB" if size >= 1024 else f"{size} B"
        connections = workload["connections"]
        detail = "1 connection" if connections == 1 else f"{connections} processes / connections"
        row_title = f"{peer} · {size_label}"
        if streaming:
            batch = workload["peer_flush_frames"]
            detail = f'{batch} frame{"s" if batch != 1 else ""} / server write'
            row_title = f"{size_label} messages"
        clients = [workload["clients"][key] for key in keys]
        values = [client[rate_key] for client in clients]
        ratio_interval = workload.get("paired_bend_speed_ratio", {}).get("bootstrap_95_ci")
        inconclusive = ratio_interval is not None and ratio_interval[0] <= 1 <= ratio_interval[1]
        ratio_label = f"{values[0] / values[1]:.2f}×" + ("*" if inconclusive else "")
        if mobile:
            top = .810 - i * (.700 / count)
            label(fig, .06, top, row_title, 14.2, True)
            label(fig, .06, top - .019, detail, 11.8, color=MUTED)
            label(fig, .94, top, ratio_label, 15, True, ha="right")
            ax = axes(fig, [.06, top - .080, .88, .049], maximum, ticks, 10.5)
        else:
            top = .765 - i * (.634 / count)
            label(fig, .06, top - .014, row_title, 14.2, True)
            label(fig, .06, top - .041, detail, 11.8, color=MUTED)
            label(fig, .94, top - .026, ratio_label, 15, True, ha="right")
            ax = axes(fig, [.30, top - .059, .57, .062], maximum, ticks, 12)
        if streaming:
            ax.set_xticks(ticks, [f"{tick / 1_000_000:g}M" if tick >= 1_000_000 else
                                 f"{tick / 1000:g}k" if tick >= 1000 else str(tick)
                                 for tick in ticks])
        # Edge ticks stay within the image even in GitHub's narrow mobile column.
        ax.get_xticklabels()[0].set_ha("left")
        ax.get_xticklabels()[-1].set_ha("right")
        ax.set_ylim(-.5, 1.5)
        ax.barh([1, 0], values, color=[ORANGE, LAVENDER], height=.60)
        for row, value, client in zip([1, 0], values, clients):
            low, high = client[low_key], client[high_key]
            ax.errorbar(value, row, xerr=[[value - low], [high - value]],
                        fmt="none", ecolor=INK, elinewidth=1.0, capsize=2.5)
            ax.text(high + maximum * .015, row, f"{value:,.0f}", va="center",
                    color=INK, fontsize=12 if mobile else 12.5, fontproperties=FONTS[1])
        if i != count - 1:
            ax.set_xticklabels([])
            ax.tick_params(axis="x", pad=0)
    label(fig, .06, .074, f'{data["runs"]} shuffled runs · bars: medians · lines: min–max', 10.9 if mobile else 12.2, color=MUTED)
    label(fig, .06, .055,
          "* Inconclusive: paired ratio interval crosses 1×." if has_inconclusive else
          ("Go peer · 1 connection · every sequence checked" if streaming else
           "Multipliers compare Bend with curl_cffi."), 10.9 if mobile else 12.2, color=MUTED)
    label(fig, .06, .036, "Both clients check every byte and opcode in timing.", 10.9 if mobile else 12.2, color=MUTED)
    label(fig, .06, .017, "Verified loopback TLS · same backend · setup excluded", 10.9 if mobile else 12.2, color=MUTED)
    description = (
        f"TLS WebSocket {units.lower()}. Orange: Scrapanium Bend; purple: "
        "matched curl_cffi Python. Bars are medians; whiskers span every run. "
        + "; ".join(f'{w.get("peer", "go")} peer, {w["bytes"]} bytes, '
                    + (f'{w["peer_flush_frames"]} frames per server write: ' if streaming else
                       f'{w["connections"]} connections: ')
                    + f'{w["clients"][keys[0]][rate_key]:,.0f} versus '
                    f'{w["clients"][keys[1]][rate_key]:,.0f}' for w in workloads)
        + f'. {data["runs"]} shuffled repeats. Both clients check every byte and opcode '
        "inside timing. Verified loopback TLS, matched backend, Chrome 146. Multiple "
        "connections use independent client processes. Startup, upgrade, warmup and "
        "close excluded. Source: benchmarks/results/"
        + ("websocket-streaming.json." if streaming else "websocket.json.")
    )
    stem = "performance-wss-streaming" if streaming else "performance-wss"
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
            if (ROOT / "benchmarks/results/websocket-streaming.json").exists():
                wss_chart(read("websocket-streaming.json"), mobile, streaming=True)
            if websocket.get("schema") == 2:
                wss_chart(websocket, mobile)
            else:
                compact_chart(
                "performance-wss", "TLS WebSocket throughput", "Round trips/s · higher is better",
                [("Scrapanium (Bend)", bend, f"{bend:,.0f}", ORANGE),
                 ("curl_cffi (Python)", baseline, f"{baseline:,.0f}", LAVENDER)],
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
                "performance-memory", "Memory: stream vs buffer", "Peak process RSS · MiB · lower is better",
                [("Stream to file", memory["download"], f'{memory["download"]:.1f} MiB', ORANGE),
                 ("Buffer response", memory["buffer"], f'{memory["buffer"]:.1f} MiB', BUFFER)],
                89, [0, 20, 40, 60, 80],
                (["Both modes: Scrapanium (Bend) · 64 MiB",
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
