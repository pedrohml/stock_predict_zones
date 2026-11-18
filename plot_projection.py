#!/usr/bin/env python3
"""Plot a reversal-zones projection over its candles and export it as an image.

    pip install matplotlib
    python plot_projection.py input.json projection.json -o petr4.png
    python plot_projection.py input.json projection.json -o petr4.svg --theme dark --lang en

INPUT is the JSON written by build_prompt.py (it holds the candles).
PROJECTION is the JSON returned by the reversal-zones-projection skill.
The image format follows the extension of -o: png, svg, pdf or jpg.
"""
import argparse
import json
import re
import sys
import textwrap
from datetime import date, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as effects
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e7e6e1",
              "support": "#2a78d6", "resistance": "#e34948", "down": "#52514e",
              "scenario": ["#8a8982", "#a9a8a1", "#c6c5be", "#dddcd6"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#2e2e2c",
             "support": "#3987e5", "resistance": "#e66767", "down": "#c3c2b7",
             "scenario": ["#6f6e69", "#5b5a56", "#484845", "#383835"]},
}

TEXT = {
    "pt": {
        "months": "jan fev mar abr mai jun jul ago set out nov dez".split(),
        "title": "{ticker} · zonas de reversão · horizonte: {label}",
        "labels": {"today": "hoje", "1-2w": "1–2 semanas", "1-3m": "1–3 meses", "6-12m": "6–12 meses"},
        "price_at": "Preço {price} {currency} em {when}", "trend_is": "tendência {trend}",
        "off_high": "{drop} desde o topo de {high}",
        "trend": {"down": "de baixa", "up": "de alta", "sideways": "lateral"},
        "horizon": "horizonte até {last}",
        "prob_title": "Probabilidade no horizonte",
        "legend_turn": "reverte nesta zona", "legend_reach": "chega à zona",
        "reach": "chega {p}", "below": "abaixo de {x}", "above": "acima de {x}",
        "swing_high": "topo {x}", "swing_low": "fundo {x}",
        "scenarios": "Cenários",
        "names": {"breakdown": "Queda", "breakout": "Alta", "range": "Faixa", "whipsaw": "Vaivém"},
        "scenario_note": "Queda = fecha abaixo de {s} · Alta = fecha acima de {r} · Faixa = nenhum dos dois · Vaivém = os dois",
        "drivers": "Fatores", "events": "Eventos", "no_date": "sem data",
        "confidence": "Confiança: {level}", "levels": {"low": "baixa", "medium": "média"},
        "footer": "Estimativas probabilísticas, não recomendação de compra ou venda. Preços sem ajuste por dividendos.",
        "decimal": ",",
    },
    "en": {
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
        "title": "{ticker} · reversal zones · horizon: {label}",
        "labels": {"today": "today", "1-2w": "1–2 weeks", "1-3m": "1–3 months", "6-12m": "6–12 months"},
        "price_at": "Price {price} {currency} at {when}", "trend_is": "trend {trend}",
        "off_high": "{drop} from the {high} high",
        "trend": {"down": "down", "up": "up", "sideways": "sideways"},
        "horizon": "horizon to {last}",
        "prob_title": "Probability within the horizon",
        "legend_turn": "reverses in this zone", "legend_reach": "reaches the zone",
        "reach": "reaches {p}", "below": "below {x}", "above": "above {x}",
        "swing_high": "high {x}", "swing_low": "low {x}",
        "scenarios": "Scenarios",
        "names": {"breakdown": "Breakdown", "breakout": "Breakout", "range": "Range", "whipsaw": "Whipsaw"},
        "scenario_note": "Breakdown = closes below {s} · Breakout = closes above {r} · Range = neither · Whipsaw = both",
        "drivers": "Drivers", "events": "Events", "no_date": "no date",
        "confidence": "Confidence: {level}", "levels": {"low": "low", "medium": "medium"},
        "footer": "Probabilistic estimates, not a recommendation to buy or sell. Prices are not dividend-adjusted.",
        "decimal": ".",
    },
}


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def parse_day(text):
    return datetime.fromisoformat(text).date() if "T" in text else date.fromisoformat(text[:10])


def build(args):
    source, proj = load_json(args.input), load_json(args.projection)
    theme, t = THEMES[args.theme], TEXT[args.lang]
    if source["asset"]["ticker"].upper() != proj["asset"]["ticker"].upper():
        print(f"Warning: candles are {source['asset']['ticker']} but the projection is "
              f"{proj['asset']['ticker']}.", file=sys.stderr)

    num = lambda x, digits=2: f"{x:,.{digits}f}".replace(",", " ").replace(".", t["decimal"])
    pct = lambda p: f"{round(p * 100):d}%"
    day = lambda d, year=True: f"{d.day} {t['months'][d.month - 1]}" + (f" {d.year}" if year else "")
    plain = lambda text: str(text).replace("$", r"\$")   # keep matplotlib from reading $...$ as maths

    # ---- candles of the series the projection was judged on -----------------
    basis = proj["horizon"]["close_basis"]
    series = next((s for s in source["data"]["series"] if s["candles"] == basis),
                  source["data"]["series"][-1])
    cols = source["data"]["columns"]
    rows = series["rows"]
    closes = int(proj["horizon"]["closes"])
    shown = args.candles or min(len(rows), max(40, 4 * closes))
    rows = rows[-shown:]
    col = lambda name: [row[cols.index(name)] for row in rows]
    starts, opens, highs, lows, ends = col("start"), col("open"), col("high"), col("low"), col("close")
    n = len(rows)
    price = proj["as_of"]["price"]
    as_of = parse_day(proj["as_of"]["time"])
    last_day = parse_day(proj["horizon"]["last"])

    # The future occupies one slot per close still to come.
    # It is drawn at least 28% as wide as the history so the zone labels always fit.
    future = max(1, closes - (1 if series["last_row"] == "forming" else 0), round(0.28 * n))
    x_now, x_end = n - 0.5, n - 0.5 + future

    def x_of(day_value):
        span = max((last_day - as_of).days, 1)
        share = min(max((day_value - as_of).days / span, 0), 1)
        return x_now + share * future

    supports = [z for z in proj["supports"] if None not in z["zone"]]
    resistances = [z for z in proj["resistances"] if None not in z["zone"]]
    zones = supports + resistances
    typical = sorted(z["zone"][1] - z["zone"][0] for z in zones)[len(zones) // 2]
    tail_low = next((z for z in proj["supports"] if z["zone"][0] is None), None)
    tail_high = next((z for z in proj["resistances"] if z["zone"][1] is None), None)

    lo = min(min(lows), min(z["zone"][0] for z in zones))
    hi = max(max(highs), max(z["zone"][1] for z in zones))
    pad = 0.04 * (hi - lo)
    y_min = lo - pad - (typical * 1.6 if tail_low else 0)
    y_max = hi + pad + (typical * 1.6 if tail_high else 0)

    # ---- figure --------------------------------------------------------------
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "text.color": theme["text"], "axes.edgecolor": theme["grid"],
                         "axes.labelcolor": theme["muted"], "xtick.color": theme["muted"],
                         "ytick.color": theme["muted"], "svg.fonttype": "none"})
    fig = plt.figure(figsize=(14, 9), facecolor=theme["surface"])
    grid = fig.add_gridspec(3, 2, width_ratios=[3.3, 1.25], height_ratios=[1, 0.075, 0.25],
                            left=0.045, right=0.985, top=0.895, bottom=0.045, wspace=0.045, hspace=0.2)
    ax = fig.add_subplot(grid[0, 0])
    px = fig.add_subplot(grid[0, 1], sharey=ax)
    sx = fig.add_subplot(grid[1, 0])
    notes = fig.add_subplot(grid[2, 0])
    side = fig.add_subplot(grid[1:, 1])
    for axis in (ax, px, sx, notes, side):
        axis.set_facecolor(theme["surface"])

    structure = proj.get("structure", {})
    swing = structure.get("swing", {})
    fig.text(0.045, 0.955, t["title"].format(ticker=proj["asset"]["ticker"],
                              label=t["labels"].get(proj["horizon"]["label"], proj["horizon"]["label"])),
             fontsize=17, fontweight="bold", va="center")
    parts = [t["price_at"].format(price=num(price), currency=proj["asset"].get("currency", ""), when=day(as_of))]
    if structure.get("trend"):
        parts.append(t["trend_is"].format(trend=t["trend"].get(structure["trend"], structure["trend"])))
    if swing.get("high") and "from_high" in structure:
        parts.append(t["off_high"].format(drop=pct(structure["from_high"]).replace("-", "−"), high=num(swing["high"][0])))
    fig.text(0.045, 0.922, plain(" · ".join(parts)), fontsize=10.5, color=theme["muted"], va="center")

    # ---- main panel: zones, candles, price, events ---------------------------
    ax.axvspan(x_now, x_end, color=theme["text"], alpha=0.045, lw=0)
    ax.text(x_now + 0.35, y_max - 0.012 * (y_max - y_min), t["horizon"].format(last=day(last_day)),
            ha="left", va="top", fontsize=8.5, color=theme["muted"])
    halo = [effects.withStroke(linewidth=2.2, foreground=theme["surface"])]

    for zone in zones:
        color = theme["support"] if zone in supports else theme["resistance"]
        low, high = zone["zone"]
        ax.add_patch(Rectangle((-0.5, low), x_now + 0.5, high - low, color=color, alpha=0.10, lw=0))
        ax.add_patch(Rectangle((x_now, low), future, high - low, color=color, alpha=0.30, lw=0))
        ax.hlines([low, high], -0.5, x_end, color=theme["surface"], lw=1.6, zorder=1)   # gap between touching zones
        ax.text(x_now + 0.35, (low + high) / 2, zone["id"], fontweight="bold", fontsize=8.5,
                va="center", ha="left", zorder=7, path_effects=halo)
        ax.text(x_now + 0.35 + 0.14 * future, (low + high) / 2, f"{num(low)} – {num(high)}",
                fontsize=8.5, va="center", ha="left", zorder=7, path_effects=halo)

    body = 0.62
    for i in range(n):
        up = ends[i] >= opens[i]
        ax.vlines(i, lows[i], highs[i], color=theme["text"] if up else theme["down"], lw=0.9, zorder=3)
        bottom, height = min(opens[i], ends[i]), max(abs(ends[i] - opens[i]), (y_max - y_min) * 0.0012)
        ax.add_patch(Rectangle((i - body / 2, bottom), body, height, zorder=4, lw=0.9,
                               facecolor=theme["surface"] if up else theme["down"],
                               edgecolor=theme["text"] if up else theme["down"]))

    # The price line starts after the zone labels when the price sits inside a zone, so it never strikes them out.
    in_zone = any(z["zone"][0] - typical * 0.3 <= price <= z["zone"][1] + typical * 0.3 for z in zones)
    ax.hlines(price, x_now + 0.62 * future if in_zone else n - 1, x_end, color=theme["text"], lw=1.0,
              linestyles=(0, (4, 3)), zorder=5)
    ax.annotate(num(price), (x_end, price), xytext=(-4, 0), textcoords="offset points",
                ha="right", va="center", fontsize=8.5, fontweight="bold", zorder=6,
                bbox={"boxstyle": "round,pad=0.25", "fc": theme["surface"], "ec": theme["text"], "lw": 0.8})

    for key, marker, offset in (("high", "v", 1), ("low", "^", -1)):
        point = swing.get(key)
        if point and point[1] in starts:
            i = starts.index(point[1])
            y = (highs[i] if key == "high" else lows[i]) + offset * 0.018 * (y_max - y_min)
            ax.plot(i, y, marker=marker, ms=6, color=theme["text"], zorder=6)
            ax.text(i + 1.2, y, t[f"swing_{key}"].format(x=num(point[0])), fontsize=8.5,
                    va="center", ha="left", color=theme["muted"])

    event_ticks = {}
    for event in proj.get("events", []):
        when = parse_day(event["date"]) if event.get("date") else None
        if when and as_of <= when <= last_day:
            x = x_of(when)
            ax.vlines(x, y_min, y_max - 0.06 * (y_max - y_min), color=theme["muted"], lw=0.9,
                      linestyles=(0, (1, 2)), zorder=2)
            ax.plot(x, y_min, marker="D", ms=5, color=theme["muted"], clip_on=False, zorder=8)
            if x_now + 0.1 * future < x < x_end - 0.2 * future:   # room for a date between the neighbouring labels
                event_ticks[x] = day(when, False)

    intraday = basis in ("15m", "1h")
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    labels = []
    for i in ticks:
        stamp = datetime.fromisoformat(starts[i])
        labels.append(stamp.strftime("%d/%m %H:%M") if intraday else
                      day(stamp.date(), False) if basis == "1D" else
                      f"{t['months'][stamp.month - 1]} {stamp.year % 100:02d}")
    ax.set_xticks(ticks + list(event_ticks) + [x_end], labels + list(event_ticks.values()) + [day(last_day, False)])
    ax.get_xticklabels()[-1].set_ha("right")
    ax.set_xlim(-1, x_end + 0.3)
    ax.set_ylim(y_min, y_max)
    spread = y_max - y_min
    ax.yaxis.set_major_formatter(lambda value, _: num(value, 0 if spread > 12 else 1 if spread > 1.5 else 2))
    ax.grid(axis="y", color=theme["grid"], lw=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0, labelsize=8.5)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)

    # ---- probability panel: one bar per zone, on the same price scale --------
    def bar(entry, low, high, color, label):
        px.add_patch(Rectangle((0, low), entry["reach"], high - low, fill=False, ec=color, lw=1.2))
        px.add_patch(Rectangle((0, low), entry["turn"], high - low, color=color, alpha=0.9, lw=0))
        px.text(entry["reach"] + 0.035, (low + high) / 2, pct(entry["turn"]), fontweight="bold",
                fontsize=9.5, va="center", ha="left")
        px.text(entry["reach"] + 0.035 + 0.16, (low + high) / 2, plain(label), fontsize=8.5,
                va="center", ha="left", color=theme["muted"])

    for zone in zones:
        color = theme["support"] if zone in supports else theme["resistance"]
        bar(zone, zone["zone"][0], zone["zone"][1], color, t["reach"].format(p=pct(zone["reach"])))
    gap = typical * 0.35
    if tail_low:
        edge = tail_low["zone"][1]
        bar(tail_low, edge - gap - typical, edge - gap, theme["support"], t["below"].format(x=num(edge)))
    if tail_high:
        edge = tail_high["zone"][0]
        bar(tail_high, edge + gap, edge + gap + typical, theme["resistance"], t["above"].format(x=num(edge)))

    px.set_xlim(0, 1.72)
    px.set_xticks([0, 0.5, 1.0], ["0%", "50%", "100%"])
    px.tick_params(length=0, labelsize=8.5, labelleft=False)
    px.grid(axis="y", color=theme["grid"], lw=0.8)
    px.axvline(0, color=theme["muted"], lw=0.8)
    px.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        px.spines[spine].set_visible(False)
    px.set_title(t["prob_title"], fontsize=10.5, fontweight="bold", loc="left", pad=22)
    for x, filled, label in ((0.0, True, t["legend_turn"]), (0.52, False, t["legend_reach"])):
        px.add_patch(Rectangle((x, 1.025), 0.035, 0.022, transform=px.transAxes, clip_on=False, lw=1.1,
                               fc=theme["muted"] if filled else "none", ec=theme["muted"]))
        px.text(x + 0.05, 1.036, label, transform=px.transAxes, fontsize=8.5, va="center", color=theme["muted"])

    # ---- scenarios: one segmented bar -----------------------------------------
    scenarios = sorted(proj.get("scenarios", []), key=lambda s: -s["p"])
    left = 0.0
    for scenario, shade in zip(scenarios, theme["scenario"]):
        sx.barh(0, scenario["p"], left=left, color=shade, edgecolor=theme["surface"], lw=2, height=0.9)
        name = t["names"].get(scenario["name"], scenario["name"])
        wide = scenario["p"] >= 0.13
        if scenario["p"] >= 0.04:
            sx.text(left + scenario["p"] / 2, 0, f"{name} {pct(scenario['p'])}" if wide else pct(scenario["p"]),
                    ha="center", va="center", fontsize=9, fontweight="bold")
        if not wide:   # too narrow for its name: write it under the segment
            at_end = left + scenario["p"] > 0.93
            sx.text(left + scenario["p"] if at_end else left + scenario["p"] / 2, -0.62,
                    name if scenario["p"] >= 0.04 else f"{name} {pct(scenario['p'])}",
                    ha="right" if at_end else "center", va="top", fontsize=8.5, color=theme["muted"], clip_on=False)
        left += scenario["p"]
    sx.set_xlim(0, max(left, 1))
    sx.set_ylim(-0.5, 0.5)
    sx.axis("off")
    sx.set_title(t["scenarios"], fontsize=10.5, fontweight="bold", loc="left", pad=4)

    by_name = {s["name"]: s for s in scenarios}
    number = lambda text: (re.findall(r"\d+(?:\.\d+)?", text or "") or ["?"])[0]
    notes.axis("off")
    lines = []
    if {"breakdown", "breakout"} <= set(by_name):
        s_mid, r_mid = number(by_name["breakdown"].get("low")), number(by_name["breakout"].get("high"))
        lines.append(t["scenario_note"].format(s=s_mid.replace(".", t["decimal"]), r=r_mid.replace(".", t["decimal"])))
        lines.append("")
    drivers = proj.get("drivers", [])[:4]
    if drivers:
        lines.append(t["drivers"] + ":")
        for driver in drivers:
            arrow = "↓" if driver.get("pull") == "down" else "↑"
            lines.append(textwrap.shorten(f"  {arrow}  {driver['what']}", 132, placeholder="…"))
    notes.text(0, 1.06, plain("\n".join(lines)), va="top", ha="left", fontsize=8.8, color=theme["muted"],
               linespacing=1.45, clip_on=False)

    # ---- confidence and events ------------------------------------------------
    side.axis("off")
    confidence = proj.get("confidence", {})
    block = [t["confidence"].format(level=t["levels"].get(confidence.get("level"), confidence.get("level", "?")))]
    side.text(0, 1.0, block[0], va="top", fontsize=10.5, fontweight="bold")
    body_lines = []
    for limit in confidence.get("limits", [])[:3]:
        body_lines += textwrap.wrap(limit, 50, initial_indent="•  ", subsequent_indent="    ")
    events = proj.get("events", [])
    if events:
        body_lines += ["", "◆  " + t["events"] + ":"]
        for event in events[:3]:
            when = day(parse_day(event["date"])) if event.get("date") else t["no_date"]
            body_lines += textwrap.wrap(f"{when}: {event['what']}", 50, initial_indent="•  ", subsequent_indent="    ")
    side.text(0, 0.88, plain("\n".join(body_lines)), va="top", fontsize=8.8, color=theme["muted"], linespacing=1.45)

    fig.text(0.045, 0.014, t["footer"], fontsize=8, color=theme["muted"], va="bottom")
    return fig


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("input", help="JSON from build_prompt.py (the candles)")
    parser.add_argument("projection", help="JSON from the reversal-zones-projection skill")
    parser.add_argument("-o", "--output", default="projection.png",
                        help="image file; the extension picks the format (png, svg, pdf, jpg)")
    parser.add_argument("--theme", choices=THEMES, default="light")
    parser.add_argument("--lang", choices=TEXT, default="pt")
    parser.add_argument("--candles", type=int, help="how many past candles to show")
    parser.add_argument("--dpi", type=int, default=160)
    args = parser.parse_args()

    fig = build(args)
    fig.savefig(args.output, dpi=args.dpi, facecolor=fig.get_facecolor())
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
