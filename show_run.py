#!/usr/bin/env python3
"""Print one run as a short text summary: what the projection found, without opening a file.

    python show_run.py INPUT.json PROJECTION.json [ENTRIES.json] [--lang CODE] [--width N] [--no-color]
    python show_run.py FOLDER          # a run folder (runs/...) or a saved one (projections/...)

FOLDER holds input.json and projection.json, and entries.json when the run made
trade setups (files named STEM_input.json and so on, as --out-dir writes them,
are found too when there is one of each).

Top to bottom, like the chart: one header line, the zones with the current price
between resistances and supports, the scenarios, the hypothetical trade setups
(only with an entries file), events and confidence, the drivers. Every number
and sentence comes from the files; only the labels are this script's.

The labels follow the input's `reply.language` (English when it has no entry in
TEXT), as the chart's do; --lang overrides. Colour is used only when standard
output is a terminal, and never with --no-color or when NO_COLOR is set; a
program that relays the output to a terminal passes --color.

The result is analysis for study, not investment advice.
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import textwrap
import unicodedata
from datetime import date, datetime
from pathlib import Path

# One entry per language. The values shared with the chart (months, labels, trend, names,
# levels, below/above, separators, footer) must stay equal to plot_projection.TEXT, which
# is not imported because it loads matplotlib; tests/test_show_run.py pins both.
TEXT = {
    "en": {
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
        "labels": {"today": "today", "1-2w": "1–2 weeks", "1-3m": "1–3 months", "6-12m": "6–12 months"},
        "horizon": "{label} to {last}", "trend_is": "trend {trend}",
        "trend": {"down": "down", "up": "up", "sideways": "sideways"},
        "zones": "Zones", "range": "range", "strength": "strength",
        "strengths": {"strong": "strong", "moderate": "moderate", "weak": "weak"},
        "bar": "░ reach █ turn", "price": "price {x}", "inside": "inside {id}",
        "below": "below {x}", "above": "above {x}",
        "scenarios": "Scenarios",
        "names": {"breakdown": "Breakdown", "breakout": "Breakout", "range": "Range", "whipsaw": "Whipsaw"},
        "neither": "neither", "both": "both",
        "setups": "Hypothetical setups", "valid_until": "valid until {last}", "no_setups": "No setups: {reason}",
        "side": "side", "order": "order", "entry": "entry", "stop": "stop", "target": "target",
        "ratio": "reward/risk", "edge": "edge per order", "quality": "quality",
        "sides": {"long": "long", "short": "short"},
        "orders": {"limit": "limit", "stop": "stop", "market": "market"},
        "qualities": {"favourable": "favourable", "marginal": "marginal", "unfavourable": "unfavourable"},
        "events": "Events", "no_date": "no date", "more": "+{n} more",
        "confidence": "Confidence: {level}", "levels": {"low": "low", "medium": "medium"},
        "drivers": "Drivers",
        "footer": "Probabilistic estimates, not a recommendation to buy or sell. Prices are not dividend-adjusted.",
        "decimal": ".", "thousands": ",",
    },
    "pt": {
        "months": "jan fev mar abr mai jun jul ago set out nov dez".split(),
        "labels": {"today": "hoje", "1-2w": "1–2 semanas", "1-3m": "1–3 meses", "6-12m": "6–12 meses"},
        "horizon": "{label}, até {last}", "trend_is": "tendência {trend}",
        "trend": {"down": "de baixa", "up": "de alta", "sideways": "lateral"},
        "zones": "Zonas", "range": "faixa", "strength": "força",
        "strengths": {"strong": "forte", "moderate": "moderada", "weak": "fraca"},
        "bar": "░ reach █ turn", "price": "preço {x}", "inside": "dentro de {id}",
        "below": "abaixo de {x}", "above": "acima de {x}",
        "scenarios": "Cenários",
        "names": {"breakdown": "Queda", "breakout": "Alta", "range": "Faixa", "whipsaw": "Vaivém"},
        "neither": "nenhum dos dois", "both": "os dois",
        "setups": "Setups hipotéticos", "valid_until": "válidos até {last}", "no_setups": "Sem setups: {reason}",
        "side": "lado", "order": "ordem", "entry": "entrada", "stop": "stop", "target": "alvo",
        "ratio": "ganho/risco", "edge": "edge por ordem", "quality": "qualidade",
        "sides": {"long": "comprado", "short": "vendido"},
        "orders": {"limit": "limite", "stop": "stop", "market": "a mercado"},
        "qualities": {"favourable": "favorável", "marginal": "marginal", "unfavourable": "desfavorável"},
        "events": "Eventos", "no_date": "sem data", "more": "mais {n}",
        "confidence": "Confiança: {level}", "levels": {"low": "baixa", "medium": "média"},
        "drivers": "Fatores",
        "footer": "Estimativas probabilísticas, não recomendação de compra ou venda. Preços sem ajuste por dividendos.",
        "decimal": ",", "thousands": ".",
    },
}
FALLBACK_LANG = "en"

DEFAULT_WIDTH, MAX_AUTO_WIDTH, MIN_WIDTH = 80, 100, 30
BAR = 20                 # characters of a full probability bar
LIMITS_SHOWN, EVENTS_SHOWN, DRIVERS_SHOWN = 2, 3, 4
MISSING = "–"
ANSI = {"bold": "1", "dim": "2", "support": "32", "resistance": "31", "good": "32", "bad": "31"}
# For a terminal whose encoding lacks the drawing characters.
ASCII = str.maketrans({"─": "-", "█": "#", "░": ":", "↑": "^", "↓": "v", "·": "|", "–": "-",
                       "−": "-", "…": "~", "•": "*"})


class Problem(Exception):
    """A failure the user should see as one short line, without a traceback."""


# ---- reading the files -----------------------------------------------------------

def load_json(path, what):
    path = Path(path)
    if not path.is_file():
        raise Problem(f"{what} not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError, OSError) as error:
        raise Problem(f"{what} is not valid JSON: {path} ({error})")
    if not isinstance(data, dict):
        raise Problem(f"{what} must be a JSON object: {path}")
    return data


def run_files(paths):
    """(input, projection, entries or None) from two or three files, or from one folder."""
    if len(paths) > 3:
        raise Problem("give INPUT.json PROJECTION.json [ENTRIES.json], or one folder")
    if len(paths) > 1:
        return paths[0], paths[1], paths[2] if len(paths) == 3 else None
    folder = Path(paths[0])
    if not folder.is_dir():
        raise Problem(f"{folder} is not a folder: give INPUT.json and PROJECTION.json, or one folder "
                      "holding input.json and projection.json")
    found = {}
    for kind in ("input", "projection", "entries"):
        plain, stemmed = folder / f"{kind}.json", sorted(folder.glob(f"*_{kind}.json"))
        found[kind] = plain if plain.is_file() else stemmed[0] if len(stemmed) == 1 else None
    for kind in ("input", "projection"):
        if found[kind] is None:
            raise Problem(f"no {kind}.json in {folder}")
    return found["input"], found["projection"], found["entries"]


def language(requested, source):
    """The TEXT entry: --lang if given, else the input's reply.language reduced to its
    primary subtag ("pt-BR" -> "pt"), else English. The same rule as the chart's."""
    primary = lambda text: re.split(r"[-_]", text.strip().lower())[0]
    if requested:
        if primary(requested) not in TEXT:
            raise Problem(f"no labels for language '{requested}' (supported: {', '.join(sorted(TEXT))})")
        return TEXT[primary(requested)]
    reply = source.get("reply") if isinstance(source, dict) else None
    asked = reply.get("language") if isinstance(reply, dict) else None
    return TEXT.get(primary(asked) if isinstance(asked, str) else "", TEXT[FALLBACK_LANG])


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_share(value):
    return is_number(value) and 0 <= value <= 1


def parse_day(text):
    """A date, or a datetime when the text carries a time. None when it is neither."""
    try:
        return datetime.fromisoformat(text) if "T" in text else date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def objects(node, key):
    """node[key] as a list of objects; anything else in it is skipped. Optional sections
    are written by a language model, so a malformed one is left out, not fatal."""
    found = node.get(key) if isinstance(node, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def required(proj):
    """Raise Problem unless the projection has what the summary cannot do without: the
    same fields plot_projection.py requires for the header and the zones."""
    def need(node, path, key, test, expected):
        value = node.get(key) if isinstance(node, dict) else None
        if not test(value):
            raise Problem(f"projection.{path}{key}: expected {expected}")
        return value
    text = lambda v: isinstance(v, str) and bool(v.strip())
    obj = lambda v: isinstance(v, dict)
    if "supports" not in proj and "data" in proj:
        raise Problem("the second file is a build_prompt.py output, not a projection "
                      "(pass the input first, then the projection)")
    need(need(proj, "", "asset", obj, "an object"), "asset.", "ticker", text, "a non-empty string")
    now = need(proj, "", "as_of", obj, "an object")
    need(now, "as_of.", "price", is_number, "a number")
    need(now, "as_of.", "time", lambda v: text(v) and parse_day(v), "an ISO date or date-time")
    horizon = need(proj, "", "horizon", obj, "an object")
    need(horizon, "horizon.", "label", text, "a non-empty string")
    need(horizon, "horizon.", "last", lambda v: text(v) and parse_day(v), "an ISO date")
    for side, tail_at in (("supports", 0), ("resistances", 1)):
        entries = need(proj, "", side, lambda v: isinstance(v, list), "a list")
        for i, entry in enumerate(entries):
            here = f"{side}[{i}]."
            if not isinstance(entry, dict):
                raise Problem(f"projection.{side}[{i}]: expected an object")
            need(entry, here, "id", text, "a non-empty string")
            need(entry, here, "reach", is_share, "a number from 0 to 1")
            need(entry, here, "turn", is_share, "a number from 0 to 1")
            zone = entry.get("zone")
            good = (isinstance(zone, list) and len(zone) == 2 and zone != [None, None]
                    and all(edge is None or is_number(edge) for edge in zone)
                    and (None not in zone or zone[tail_at] is None))
            if not good:
                raise Problem(f"projection.{here}zone: expected [low, high], or one number and one null for a tail")


# ---- text on a terminal ----------------------------------------------------------

def wlen(text):
    """Columns the text takes on a terminal: wide characters count two, combining marks none."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def clip(text, width):
    """The text cut to `width` columns, ending in an ellipsis when something was cut."""
    if wlen(text) <= width:
        return text
    kept, used = [], 0
    for ch in text:
        used += wlen(ch)
        if used > width - 1:
            break
        kept.append(ch)
    return ("".join(kept).rstrip() + "…") if width > 0 else ""


def one_line(value):
    """Text from a file, safe on one terminal line: no line breaks, tabs or control characters."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())


def pad(text, width, align="<"):
    space = " " * max(0, width - wlen(text))
    return space + text if align == ">" else text + space


def cell(value):
    """A table cell is a string or a (string, style) pair."""
    return value if isinstance(value, tuple) else (value, None)


class Rule:
    """A table row that is a line across the block, with a label on it."""
    def __init__(self, label, style=None):
        self.label, self.style = label, style


def table(columns, rows, width, title=None, groups=(), indent=2, gap=2):
    """Lines (lists of (text, style)) of an aligned table.

    columns: (header, align) pairs; the first is the key column. When the columns do
    not fit in `width` the table continues in a second block that repeats the key
    column, so nothing is cut that a narrower terminal can still show. `groups` are
    runs of column numbers that belong together: a run moves to the next block as a
    whole when it fits there. `title`, when given, takes the place of the key
    column's header and starts at the margin."""
    rows = [row if isinstance(row, Rule) else [cell(value) for value in row] for row in rows]
    data = [row for row in rows if not isinstance(row, Rule)]
    headers = [header for header, _ in columns]
    widths = [max([wlen(header)] + [wlen(row[i][0]) for row in data]) for i, header in enumerate(headers)]
    key = min(widths[0], max(4, (width - indent) // 2))
    if title is not None:
        key = max(key, min(wlen(title) - indent, width - indent))
    room = width - indent - key
    blocks, used = [[]], 0
    for i in range(1, len(columns)):
        widths[i] = min(widths[i], max(1, room - gap))
        group = next((g for g in groups if g[0] == i), (i,))   # a run starting here
        whole = sum(gap + min(widths[j], max(1, room - gap)) for j in group)
        if blocks[-1] and (used + gap + widths[i] > room or used + whole > room >= whole):
            blocks.append([])
            used = 0
        blocks[-1].append(i)
        used += gap + widths[i]
    lines = []
    for block in blocks:
        span = key + sum(gap + widths[i] for i in block)
        first = [(pad(clip(title, indent + key), indent + key), "bold")] if title is not None else \
                [(" " * indent + pad(clip(headers[0], key), key), "dim")]
        if any(headers[i] for i in block) or title is not None:
            lines.append(first + [(" " * gap + pad(clip(headers[i], widths[i]), widths[i], columns[i][1]), "dim")
                                  for i in block])
        for row in rows:
            if isinstance(row, Rule):
                label = f"── {row.label} " if wlen(row.label) + 6 <= span else ""   # repeated blocks may be narrow
                lines.append([(" " * indent + label + "─" * (span - wlen(label)), row.style)])
                continue
            line = [(" " * indent + pad(clip(row[0][0], key), key, columns[0][1]), row[0][1])]
            for i in block:
                line.append((" " * gap, None))
                line.append((pad(clip(row[i][0], widths[i]), widths[i], columns[i][1]), row[i][1]))
            lines.append(line)
    return lines


def joined(parts, width, separator=" · ", indent=2):
    """The parts on as few lines as fit the width, broken only between parts."""
    lines, current = [], ""
    for part in parts:
        if current and wlen(current + separator + part) > width:
            lines.append(current)
            current = " " * indent + part
        else:
            current = current + separator + part if current else part
    return lines + [current] if current else lines


def paint(text, style):
    """The text in a style, with the blanks around it left alone."""
    core = text.strip()
    if not core:
        return text
    start = len(text) - len(text.lstrip())
    return text[:start] + f"\033[{ANSI[style]}m{core}\033[0m" + text[start + len(core):]


def render(lines, width, color=False, ascii_only=False):
    """The final text: no line wider than `width`, styles as ANSI codes only when asked."""
    out = []
    for line in lines:
        segments = [(line, None)] if isinstance(line, str) else line
        left, shown = width, []
        for text, style in segments:
            text = clip(text, left)
            left -= wlen(text)
            shown.append((text, style))
        while shown and not shown[-1][0].strip():   # no trailing blanks
            shown.pop()
        if shown:
            shown[-1] = (shown[-1][0].rstrip(), shown[-1][1])
        text = "".join(paint(part, style) if color and style else part for part, style in shown)
        out.append(text.translate(ASCII) if ascii_only else text)
    return "\n".join(out) + "\n"


# ---- the summary -----------------------------------------------------------------

def format_number(value, digits, t):
    """1234.5 -> "1,234.50" or "1.234,50": the separators are the language's (as on the chart)."""
    return f"{value:,.{digits}f}".translate({ord(","): t["thousands"], ord("."): t["decimal"]})


def price_digits(price):
    """Decimals for this asset's prices, as on the chart: 2 as a rule, none from 10 000 up,
    four significant digits below 1."""
    size = abs(price)
    if size >= 10_000:
        return 0
    if size >= 1 or size == 0:
        return 2
    return min(8, 3 - math.floor(math.log10(size)))


def bar(reach, turn=None, width=BAR):
    """A probability as a bar: the `turn` part solid, the rest of `reach` light."""
    full = round(reach * width)
    solid = full if turn is None else min(full, round(turn * width))
    return "█" * solid + "░" * (full - solid)


def summary(source, proj, entries=None, lang=None, width=DEFAULT_WIDTH):
    """The whole summary as lines of (text, style) segments. Raises Problem on a
    projection without the fields `required()` names."""
    required(proj)
    t = language(lang, source)
    width = max(MIN_WIDTH, width)
    price = proj["as_of"]["price"]
    digits = price_digits(price)
    num = lambda x, d=digits: format_number(x, d, t) if is_number(x) else MISSING
    pct = lambda p: f"{round(p * 100):d}%" if is_number(p) else MISSING
    known = lambda names, value: MISSING if value is None else t[names].get(value, one_line(value)) \
        if isinstance(value, str) else one_line(value)

    def day(text, clock=False):
        when = parse_day(text) if isinstance(text, str) else None
        if when is None:
            return one_line(text)
        shown = f"{when.day} {t['months'][when.month - 1]} {when.year}"
        return shown + f" {when:%H:%M}" if clock and isinstance(when, datetime) else shown

    lines = []
    blank = lambda: lines.append("")
    item = lambda mark, text, style=None: lines.append(
        [("  ", None), (mark + " ", style), (clip(one_line(text), width - 2 - wlen(mark) - 1), None)])

    # 1. header
    structure = proj.get("structure") if isinstance(proj.get("structure"), dict) else {}
    horizon = proj["horizon"]
    currency = proj["asset"].get("currency")
    parts = [f"{one_line(proj['asset']['ticker'])}  {num(price)}" + (f" {one_line(currency)}" if currency else ""),
             day(proj["as_of"]["time"], clock=True),
             t["horizon"].format(label=t["labels"].get(horizon["label"], one_line(horizon["label"])),
                                 last=day(horizon["last"]))]
    if isinstance(structure.get("trend"), str) and structure["trend"].strip():
        parts.append(t["trend_is"].format(trend=known("trend", structure["trend"])))
    lines += [[(text, "bold")] for text in joined(parts, width)]

    # 2. zones, top to bottom as on the chart
    def zone_rows(side, style):
        bounded = sorted((z for z in proj[side] if None not in z["zone"]), key=lambda z: -z["zone"][0])
        tails = [z for z in proj[side] if None in z["zone"]]
        ordered = bounded + tails if side == "supports" else tails + bounded
        rows = []
        for z in ordered:
            low, high = z["zone"]
            span = t["below"].format(x=num(high)) if low is None else t["above"].format(x=num(low)) \
                if high is None else f"{num(low)} – {num(high)}"
            rows.append([(one_line(z["id"]), style), span, known("strengths", z.get("strength")) if z.get("strength") else "",
                         pct(z["reach"]), pct(z["turn"]), (bar(z["reach"], z["turn"]), style)])
        return rows

    inside = next((z for side in ("supports", "resistances") for z in proj[side]
                   if None not in z["zone"] and z["zone"][0] <= price <= z["zone"][1]), None)
    marker = t["price"].format(x=num(price)) + (f" ({t['inside'].format(id=one_line(inside['id']))})" if inside else "")
    rows = zone_rows("resistances", "resistance") + [Rule(marker, "bold")] + zone_rows("supports", "support")
    columns = [("", "<"), (t["range"], ">"), (t["strength"], "<"), ("reach", ">"), ("turn", ">"), (t["bar"], "<")]
    natural = 2 + sum(2 + max([wlen(h)] + [wlen(cell(r[i])[0]) for r in rows if not isinstance(r, Rule)])
                      for i, (h, _) in enumerate(columns)) - 2
    if natural > width:   # the bars repeat reach and turn: the first thing to go on a narrow terminal
        columns, rows = columns[:-1], [r if isinstance(r, Rule) else r[:-1] for r in rows]
    blank()
    lines += table(columns, rows, width, title=t["zones"], groups=[(1, 2), (3, 4)])

    # 3. scenarios
    scenarios = [s for s in objects(proj, "scenarios") if isinstance(s.get("name"), str) and is_share(s.get("p"))]
    if scenarios:
        by_name = {s["name"]: s for s in scenarios}
        number = lambda text: (re.findall(r"\d+(?:\.\d+)?", text if isinstance(text, str) else "") or [None])[0]
        s_mid = number(by_name.get("breakdown", {}).get("low"))
        r_mid = number(by_name.get("breakout", {}).get("high"))
        meaning = {"breakdown": t["below"].format(x=num(float(s_mid))), "breakout": t["above"].format(x=num(float(r_mid))),
                   "range": t["neither"], "whipsaw": t["both"]} if s_mid and r_mid else {}
        own = lambda s: " · ".join(one_line(s[k]) for k in ("low", "high") if isinstance(s.get(k), str) and s[k].strip())
        rows = [[known("names", s["name"]), pct(s["p"]), bar(s["p"]), meaning.get(s["name"]) or own(s)]
                for s in scenarios]
        blank()
        lines.append([(t["scenarios"], "bold")])
        lines += table([("", "<"), ("", ">"), ("", "<"), ("", "<")], rows, width)

    # 4. trade setups, as the entries file states them
    if isinstance(entries, dict):
        blank()
        setups = objects(entries, "entries")
        if "error" in entries or not setups:
            lines.append([(clip(t["no_setups"].format(reason=one_line(entries.get("error", MISSING))), width), "bold")])
        else:
            title = [t["setups"]]
            if entries.get("valid_until"):
                title.append(t["valid_until"].format(last=day(entries["valid_until"])))
            lines += [[(text, "bold")] for text in joined(title, width)]
            part = lambda e, key: e.get(key) if isinstance(e.get(key), dict) else {}
            ratio = lambda x: num(x, 2)
            tone = {"favourable": "good", "unfavourable": "bad"}
            rows = [[one_line(e.get("rank", MISSING)), one_line(e.get("id", MISSING)),
                     known("sides", e.get("side")), known("orders", part(e, "entry").get("order")),
                     num(part(e, "entry").get("price")), num(part(e, "stop").get("price")),
                     num(part(e, "target").get("price")), ratio(part(e, "payoff").get("ratio")),
                     pct(part(e, "odds").get("fill")), pct(part(e, "odds").get("win_if_filled")),
                     ratio(part(e, "edge").get("per_order")),
                     (known("qualities", e.get("quality")), tone.get(e.get("quality")))] for e in setups]
            lines += table([("#", ">"), ("id", "<"), (t["side"], "<"), (t["order"], "<"), (t["entry"], ">"),
                            (t["stop"], ">"), (t["target"], ">"), (t["ratio"], ">"), ("fill", ">"),
                            ("win_if_filled", ">"), (t["edge"], ">"), (t["quality"], "<")], rows, width,
                           groups=[(1, 2, 3), (4, 5, 6), (7, 8, 9, 10, 11)])
        if isinstance(entries.get("note"), str) and entries["note"].strip():   # verbatim, so wrapped and never cut
            lines += textwrap.wrap(one_line(entries["note"]), width - 2, initial_indent="  ", subsequent_indent="  ")

    # 5. events inside the horizon, confidence
    start, end = parse_day(proj["as_of"]["time"]), parse_day(horizon["last"])
    as_date = lambda when: when.date() if isinstance(when, datetime) else when
    events = []
    for event in objects(proj, "events"):
        when = parse_day(event["date"]) if isinstance(event.get("date"), str) else None
        if isinstance(event.get("what"), str) and (when is None or as_date(start) <= as_date(when) <= as_date(end)):
            events.append((day(event["date"]) if when else t["no_date"], event))
    blank()
    if events:
        lines.append([(t["events"], "bold")])
        for when, event in events[:EVENTS_SHOWN]:
            effect = event.get("effect")
            item(when, event["what"] + (f": {effect}" if isinstance(effect, str) and effect.strip() else ""))
        if len(events) > EVENTS_SHOWN:
            lines.append("  " + t["more"].format(n=len(events) - EVENTS_SHOWN))
    confidence = proj.get("confidence") if isinstance(proj.get("confidence"), dict) else {}
    level = confidence.get("level")
    lines.append([(t["confidence"].format(level=known("levels", level) if level else "?"), "bold")])
    limits = confidence.get("limits") if isinstance(confidence.get("limits"), list) else []
    for limit in [text for text in limits if isinstance(text, str)][:LIMITS_SHOWN]:
        item("•", limit)

    # 6. drivers
    drivers = [d for d in objects(proj, "drivers") if isinstance(d.get("what"), str)]
    if drivers:
        lines.append([(t["drivers"], "bold")])
        for driver in drivers[:DRIVERS_SHOWN]:
            down = driver.get("pull") == "down"
            item("↓" if down else "↑" if driver.get("pull") == "up" else "•", driver["what"],
                 "bad" if down else "good" if driver.get("pull") == "up" else None)
        if len(drivers) > DRIVERS_SHOWN:
            lines.append("  " + t["more"].format(n=len(drivers) - DRIVERS_SHOWN))

    if not (isinstance(entries, dict) and isinstance(entries.get("note"), str) and entries["note"].strip()):
        blank()   # the setups' own note says it already; without one the chart's footer does
        lines += [[(text, "dim")] for text in textwrap.wrap(t["footer"], width)]
    return lines


# ---- command line ----------------------------------------------------------------

def use_color(args, stream, environ=None):
    """Colour only for a terminal (or with --color, from a program that relays the output
    to one), and never with --no-color, NO_COLOR or a dumb terminal."""
    environ = os.environ if environ is None else environ
    if args.no_color or environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return False
    return bool(args.color or stream.isatty())


def encodable(stream):
    try:
        "─█░↑↓·–…•".encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeError, LookupError):
        return False


def parse(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage="%(prog)s INPUT.json PROJECTION.json [ENTRIES.json] [--lang CODE] [--width N] [--no-color]\n"
              "       %(prog)s FOLDER [--lang CODE] [--width N] [--no-color]")
    parser.add_argument("files", nargs="+", metavar="FILE",
                        help="the input and the projection, then optionally the entries file; "
                             "or one folder holding input.json, projection.json and optionally entries.json")
    parser.add_argument("--lang", metavar="CODE",
                        help=f"language of the labels: {', '.join(sorted(TEXT))} "
                             "(default: the input's reply.language, English when it has no labels)")
    parser.add_argument("--width", type=int, metavar="N",
                        help=f"widest line (default: the terminal's width, at most {MAX_AUTO_WIDTH}; "
                             f"{DEFAULT_WIDTH} when the output is not a terminal)")
    parser.add_argument("--no-color", action="store_true",
                        help="plain text even on a terminal (the NO_COLOR environment variable does the same)")
    parser.add_argument("--color", action="store_true",
                        help="colour although the output is not a terminal: for a program that relays "
                             "it to one. --no-color and NO_COLOR still win")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(argv)
    out = sys.stdout
    try:
        input_path, projection_path, entries_path = run_files(args.files)
        source = load_json(input_path, "Input JSON")
        proj = load_json(projection_path, "Projection JSON")
        entries = load_json(entries_path, "Entries JSON") if entries_path else None
        if "supports" in source and "data" in proj:
            raise Problem("the two files are swapped: pass the input (the build_prompt.py output) first, "
                          "then the projection")
        width = args.width
        if width is None:
            width = min(shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns, MAX_AUTO_WIDTH) \
                if out.isatty() else DEFAULT_WIDTH
        text = render(summary(source, proj, entries, args.lang, width), max(MIN_WIDTH, width),
                      use_color(args, out), not encodable(out))
    except Problem as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:   # files written by a language model: never a traceback
        print(f"Error: cannot summarise these files ({type(error).__name__}: {error})", file=sys.stderr)
        return 1
    try:
        if hasattr(out, "reconfigure"):
            out.reconfigure(errors="replace")   # text from the files in an encoding the terminal lacks
        out.write(text)
        out.flush()
    except BrokenPipeError:   # e.g. `| head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
