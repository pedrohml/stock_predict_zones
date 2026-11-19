#!/usr/bin/env python3
"""Backtest the ranked trade setups on past dates: stated odds against what the price did. No Claude.

    python backtest_entries.py PETR4.SA NVDA --horizon 1-3m             # the last three ended years
    python backtest_entries.py NVDA SPY --horizon 1-2w 1-3m 6-12m        # several horizons, one report
    python backtest_entries.py PETR4.SA --optimistic                     # the other bound of the ordering
    python backtest_entries.py PETR4.SA --json                           # one JSON object instead
    python backtest_entries.py PETR4.SA --csv setups.csv                 # every setup, one row each

For each ticker, horizon and as-of date (backtest.py's grid: `--start/--end/--step`, `--method`)
it does what a live `zones TICKER --engine mechanical` run does after that day's close, with the
same code: the input from backtest.input_as_of(), the projection from mechanical.build(), the
three setups from trade_entries.build(). Then it walks the DAILY candles after the as-of day, up
to the setups' valid_until (the horizon's last close), and plays each setup by the rules in
walk(): fill, then stop, target or expiry. The result is in units of the setup's own risk (R).

The report compares the stated `fill`, `win_if_filled` and edge with the observed fill rate,
win rate and mean R, overall and by side, kind, order type, quality, rank and ticker, per
horizon, with Brier scores, reliability buckets, a bootstrap over cases and the per-ticker
spread. It checks the calibration of the stated odds of hypothetical setups. It is not a
strategy, says nothing about returns you could have had, and is not advice.
"""
import argparse
import csv
import json
import random
import statistics
import sys
import textwrap
import time
from datetime import date, timedelta

import backtest
import build_prompt
import mechanical
import score_projections as sp
import trade_entries

rz = backtest.rz
BOOTSTRAP = 2000          # resamples of whole cases
BOOTSTRAP_SEED = 20261119
GROUPS = (("side", "side"), ("setup", "kind"), ("order", "order"), ("quality", "quality"), ("rank", "rank"))
QUALITY_ORDER = ("favourable", "marginal", "unfavourable")

RULES = [
    "The setups are made after the as-of day's close; the walk starts with the next daily candle "
    "(that day's own candle is never used) and ends with the candle of valid_until, the "
    "horizon's last close. Daily candles are used whatever the horizon's close basis.",
    "Fill: a limit order fills when the candle reaches its entry (a long's low at or below it, a "
    "short's high at or above it); a stop order fills when the candle reaches its entry in the "
    "order's direction (a long's high at or above it, a short's low at or below it). A touch is "
    "enough. When the open is already past the entry, the order fills at the open (better than "
    "the entry for a limit, worse for a stop). A market order fills at the next candle's open, "
    "not at the as-of close it was priced at. An unfilled order stays in place until "
    "valid_until, even if the price runs to the target first.",
    "After the fill: the stop or the target closes the setup at its price; an open past the "
    "stop or the target closes it at that open (a gap through the stop can lose more than 1R). "
    "Still open after the last candle: closed at that candle's close (expiry).",
    "Ordering inside a candle is unknown. On the fill candle, a level the price must have "
    "crossed after the entry (the entry lies between the open and the level) is hit after "
    "the fill; any other level the candle reaches may have come before it. When the candle "
    "allows more than one result (stop or target, stop or still open, target or still open), "
    "the default is the pessimistic one (stop first, target not yet); --optimistic takes the "
    "other. Each setup is played both ways, and 'ambiguous' counts those whose result differs.",
    "R = (exit - fill price) / risk for a long, (fill price - exit) / risk for a short, where "
    "risk is the setup's own entry-to-stop distance. An order that never fills is 0R. A win is "
    "an exit at the target.",
]

CAVEATS = [
    "Mechanical zones and unadjusted probabilities (mechanical.py), as a live `--engine mechanical` "
    "run makes them: not Claude's zones or adjustments. The inputs carry no events.",
    "No commissions, spread, slippage, borrowing costs or dividends; prices are split-adjusted and "
    "not dividend-adjusted, so a short does not pay the dividend and a long does not receive it.",
    "The ordering inside a daily candle is unknown and counted pessimistically by default "
    "(--optimistic gives the other bound). Touch fills are generous for limit orders.",
    "Tickers chosen today are survivors, in what was mostly a rising market for them.",
    "Cases overlap whenever the step is shorter than the horizon, the three setups of a case share "
    "one price path, and tickers move together; the bootstrap resamples whole cases and still "
    "understates the uncertainty.",
    "This checks the calibration of the stated odds of hypothetical setups. It is not a strategy, "
    "not a claim about returns, and not advice.",
]


# ---- one setup on the candles that followed --------------------------------------------

def setup_of(entry):
    """The parts of one entry of trade_entries.build() that the walk and the report read."""
    return {"rank": entry["rank"], "id": entry["id"], "side": entry["side"], "setup": entry["setup"],
            "order": entry["entry"]["order"], "entry": entry["entry"]["price"], "stop": entry["stop"]["price"],
            "target": entry["target"]["price"], "risk": entry["payoff"]["risk"], "ratio": entry["payoff"]["ratio"],
            "fill": entry["odds"]["fill"], "win_if_filled": entry["odds"]["win_if_filled"],
            "if_filled": entry["edge"]["if_filled"], "per_order": entry["edge"]["per_order"],
            "quality": entry["quality"]}


def walk(s, candles, optimistic=False):
    """Play setup `s` (setup_of()) on daily `candles` [[date, open, high, low, close], ...], the
    ones after the as-of day up to valid_until, by the rules in RULES. `optimistic` resolves a
    candle that allows two results in the setup's favour, else against it. -> {"filled",
    "fill_date", "fill_price", "gap_fill", "touch_only", "exit", "exit_date", "exit_price", "r",
    "won"}; exit is "target", "stop", "expiry" or None (never filled)."""
    long = s["side"] == "long"
    entry, stop, target, risk = s["entry"], s["stop"], s["target"], s["risk"]
    out = {"filled": False, "fill_date": None, "fill_price": None, "gap_fill": False, "touch_only": False,
           "exit": None, "exit_date": None, "exit_price": None, "r": 0.0, "won": False}
    if not candles:
        return out

    def reaches_stop(high, low):
        return low <= stop if long else high >= stop

    def reaches_target(high, low):
        return high >= target if long else low <= target

    def close(kind, day, price):
        out.update(exit=kind, exit_date=day, exit_price=price, won=kind == "target",
                   r=round(((price - out["fill_price"]) if long else (out["fill_price"] - price)) / risk, 4))
        return out

    def from_open(day, open_, high, low):
        """A candle wholly after the fill: a gap past a level closes at the open, else the
        levels it reaches. -> "stop", "target" or None (still open)."""
        if (open_ <= stop) if long else (open_ >= stop):
            return close("stop", day, open_)
        if (open_ >= target) if long else (open_ <= target):
            return close("target", day, open_)
        hit_stop, hit_target = reaches_stop(high, low), reaches_target(high, low)
        if hit_stop and hit_target:
            return close("target", day, target) if optimistic else close("stop", day, stop)
        if hit_stop:
            return close("stop", day, stop)
        if hit_target:
            return close("target", day, target)
        return None

    start = None
    for i, (day, open_, high, low, _) in enumerate(candles):
        order = s["order"]
        if order == "market":
            fill, gap = open_, True
        elif (order == "limit") == long:        # long limit or short stop: filled on the way down
            if low > entry:
                continue
            fill, gap = min(open_, entry), open_ <= entry
        else:                                   # long stop or short limit: filled on the way up
            if high < entry:
                continue
            fill, gap = max(open_, entry), open_ >= entry
        out.update(filled=True, fill_date=day, fill_price=fill, gap_fill=gap and order != "market",
                   touch_only=not gap and (low == entry if (order == "limit") == long else high == entry))
        if gap:   # the whole candle comes after the fill
            if from_open(day, open_, high, low):
                return out
        else:     # filled inside the candle: what it reached may have come before the entry
            after = lambda level: min(open_, level) <= entry <= max(open_, level)
            hit_stop, hit_target = reaches_stop(high, low), reaches_target(high, low)
            if hit_stop and hit_target:
                return close("target", day, target) if optimistic else close("stop", day, stop)
            if hit_stop and (after(stop) or not optimistic):
                return close("stop", day, stop)
            if hit_target and (after(target) or optimistic):
                return close("target", day, target)
        start = i + 1
        break
    if start is None:
        return out
    for day, open_, high, low, _ in candles[start:]:
        if from_open(day, open_, high, low):
            return out
    return close("expiry", candles[-1][0], candles[-1][4])


# ---- one case and the whole run ----------------------------------------------------------

def run_case(ticker, horizon, data, day, today, method=None, optimistic=False):
    """One past date -> {"ticker", "horizon", "as_of", "price", "valid_until", "setups": [...]}
    with each setup's stated odds and both walks, or a `skipped` reason."""
    record = {"ticker": ticker, "horizon": horizon, "as_of": day.isoformat()}
    d = backtest.input_as_of(ticker, horizon, data, day)
    main = rz.unpack(d)[1]
    keep = build_prompt.PRESETS[horizon]["main"][2]
    if len(main["rows"]) < keep:
        return {**record, "skipped": f"less history than a live run sees ({keep} {main['candles']} candles)"}
    try:
        proj = mechanical.build(d, method=method)
    except mechanical.MechanicalError as reason:
        return {**record, "skipped": str(reason).split(":")[0]}
    reply = trade_entries.build(d, proj)
    if "error" in reply:
        return {**record, "skipped": reply["error"]}
    valid_until = reply["valid_until"]
    if date.fromisoformat(valid_until) >= today:
        return {**record, "skipped": "horizon not ended yet"}
    places = trade_entries.places(d)
    candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in data["rows"]
               if day.isoformat() < row[0] <= valid_until]
    if not candles:
        return {**record, "skipped": "no daily candle after the as-of day"}
    setups = []
    for entry in reply["entries"]:
        s = setup_of(entry)
        chosen, other = walk(s, candles, optimistic), walk(s, candles, not optimistic)
        setups.append({**s, **chosen, "r_other": other["r"], "exit_other": other["exit"],
                       "ambiguous": (chosen["exit"], chosen["r"]) != (other["exit"], other["r"])})
    record.update(price=d["now"]["price"], valid_until=valid_until, candles=len(candles),
                  confidence=reply["confidence"]["level"], setups=setups)
    return record


def run(tickers, horizons, start=None, end=None, step=None, today=None, progress=True, method=None,
        optimistic=False):
    """The whole backtest -> {"settings", "records", "summary"}. Per horizon, the as-of grid of
    backtest.py (its default range and step unless given)."""
    method = rz.sim_method(method)
    records, failed, ranges = [], {}, {}
    for horizon in horizons:
        first_start, first_end = backtest.default_range(horizon, today or date.today())
        h_start, h_end, h_step = start or first_start, end or first_end, step or backtest.DEFAULT_STEP[horizon]
        if h_start > h_end:
            raise backtest.BacktestError(f"--start {h_start} is after --end {h_end}")
        ranges[horizon] = {"start": h_start.isoformat(), "end": h_end.isoformat(), "step_trading_days": h_step}
        for ticker in tickers:
            ticker = ticker.upper()
            clock = time.monotonic()
            try:
                data = backtest.download(ticker, h_start - timedelta(days=backtest.lookback(horizon)))
            except backtest.BacktestError as reason:
                failed[f"{ticker} {horizon}"] = str(reason)
                backtest.say(f"{ticker} {horizon}: not backtested: {reason}", progress)
                continue
            ticker_today = today or sp.today_in(data["tz"])
            days = backtest.as_of_days(data["rows"], h_start, h_end, h_step)
            mine = [run_case(ticker, horizon, data, day, ticker_today, method, optimistic) for day in days]
            records += mine
            used = sum(1 for r in mine if "skipped" not in r)
            backtest.say(f"{ticker} {horizon}: {len(days)} dates, {used} with setups, {len(days) - used} skipped "
                         f"({time.monotonic() - clock:.0f}s)", progress)
    settings = {"tickers": [t.upper() for t in tickers], "horizons": list(horizons), "ranges": ranges,
                "sim_method": method, "default_sim_method": rz.SIM_METHOD, "setup_rules": trade_entries.RULES,
                "ordering": "optimistic" if optimistic else "pessimistic", "rules": RULES, "failed": failed}
    return {"settings": settings, "records": records, "summary": summarise(records, horizons)}


# ---- summary -------------------------------------------------------------------------------

def mean(values):
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def stats(setups):
    """Stated against observed for a group of setups (rows of flat())."""
    n = len(setups)
    filled = [s for s in setups if s["filled"]]
    out = {"setups": n, "cases": len({(s["ticker"], s["as_of"]) for s in setups}),
           "fill_stated": mean(s["fill"] for s in setups), "fill_observed": mean(s["filled"] for s in setups),
           "fills": len(filled),
           "win_stated": mean(s["win_if_filled"] for s in filled), "win_observed": mean(s["won"] for s in filled),
           "expired": mean(s["exit"] == "expiry" for s in filled),
           "edge_fill_stated": mean(s["if_filled"] for s in filled), "r_per_fill": mean(s["r"] for s in filled),
           "edge_order_stated": mean(s["per_order"] for s in setups), "r_per_order": mean(s["r"] for s in setups),
           "r_per_order_other": mean(s["r_other"] for s in setups),
           "ambiguous": mean(s["ambiguous"] for s in setups)}
    if n:
        pairs = [(s["fill"], s["filled"]) for s in setups]
        out["brier_fill"] = round(sp.brier(pairs), 4)
        out["brier_fill_base_rate"] = round(sp.brier([(out["fill_observed"], y) for _, y in pairs]), 4)
    if filled:
        pairs = [(s["win_if_filled"], s["won"]) for s in filled]
        out["brier_win"] = round(sp.brier(pairs), 4)
        out["brier_win_base_rate"] = round(sp.brier([(out["win_observed"], y) for _, y in pairs]), 4)
    return out


def bootstrap(setups, rounds=BOOTSTRAP, seed=BOOTSTRAP_SEED):
    """90% intervals of the observed rates and mean R, resampling whole cases (a case's three
    setups stay together). Cases overlap in time, so these intervals are too narrow."""
    cases = {}
    for s in setups:
        cases.setdefault((s["ticker"], s["as_of"]), []).append(s)
    groups = list(cases.values())
    if len(groups) < 2:
        return {}
    rng = random.Random(seed)
    draws = {"fill_observed": [], "win_observed": [], "r_per_fill": [], "r_per_order": []}
    for _ in range(rounds):
        sample = [s for _ in groups for s in rng.choice(groups)]
        filled = [s for s in sample if s["filled"]]
        draws["fill_observed"].append(sum(s["filled"] for s in sample) / len(sample))
        draws["r_per_order"].append(sum(s["r"] for s in sample) / len(sample))
        if filled:
            draws["win_observed"].append(sum(s["won"] for s in filled) / len(filled))
            draws["r_per_fill"].append(sum(s["r"] for s in filled) / len(filled))
    out = {}
    for key, values in draws.items():
        if values:
            values.sort()
            out[key] = [round(values[int(0.05 * (len(values) - 1))], 4), round(values[int(0.95 * (len(values) - 1))], 4)]
    return out


def spread(setups):
    """Per-ticker mean R per order and win rate: lowest, median, highest, and how many tickers
    had a positive mean R."""
    per = {}
    for s in setups:
        per.setdefault(s["ticker"], []).append(s)
    r = [sum(x["r"] for x in v) / len(v) for v in per.values()]
    if not r:
        return {}
    return {"tickers": len(r), "r_per_order_min": round(min(r), 4), "r_per_order_median": round(statistics.median(r), 4),
            "r_per_order_max": round(max(r), 4), "tickers_positive": sum(x > 0 for x in r)}


def flat(records):
    """Every setup of every case as one row, with the case's ticker, horizon and dates."""
    return [{"ticker": r["ticker"], "horizon": r["horizon"], "as_of": r["as_of"], "price": r["price"],
             "valid_until": r["valid_until"], **s}
            for r in records if "skipped" not in r for s in r["setups"]]


def scope(setups):
    out = {"overall": stats(setups), "bootstrap_90": bootstrap(setups), "ticker_spread": spread(setups),
           "fill_buckets": sp.buckets([(s["fill"], s["filled"]) for s in setups]),
           "win_buckets": sp.buckets([(s["win_if_filled"], s["won"]) for s in setups if s["filled"]]),
           "exits": {kind: sum(1 for s in setups if s["exit"] == kind) for kind in ("target", "stop", "expiry")}}
    out["exits"]["unfilled"] = sum(1 for s in setups if not s["filled"])
    for key, name in GROUPS:
        values = sorted({s[key] for s in setups},
                        key=lambda v: QUALITY_ORDER.index(v) if v in QUALITY_ORDER else str(v))
        out[f"by_{name}"] = {str(v): stats([s for s in setups if s[key] == v]) for v in values}
    out["by_side_and_kind"] = {f"{side} {kind}": stats(group) for side in ("long", "short")
                               for kind in ("reversal", "continuation")
                               if (group := [s for s in setups if s["side"] == side and s["setup"] == kind])}
    out["by_ticker"] = {t: stats([s for s in setups if s["ticker"] == t]) for t in sorted({s["ticker"] for s in setups})}
    return out


def summarise(records, horizons):
    skipped = {}
    for r in records:
        if "skipped" in r:
            key = f"{r['horizon']}: {r['skipped']}"
            skipped[key] = skipped.get(key, 0) + 1
    rows = flat(records)
    return {"cases": sum(1 for r in records if "skipped" not in r), "setups": len(rows), "skipped": skipped,
            "caveats": CAVEATS,
            "per_horizon": {h: scope([s for s in rows if s["horizon"] == h]) for h in horizons
                            if any(s["horizon"] == h for s in rows)}}


# ---- report ----------------------------------------------------------------------------------

def num(value, form="{:.2f}"):
    return "-" if value is None else form.format(value)


def pct(value):
    return "-" if value is None else f"{value:.0%}"


HEAD = (f"    {'group':<22} {'setups':>6} {'fill st/obs':>12} {'win st/obs':>12} {'R/fill st/obs':>14} "
        f"{'R/order st/obs':>15} {'ambig':>6}")


def row(name, k):
    return (f"    {name:<22} {k['setups']:>6} {pct(k['fill_stated']) + '/' + pct(k['fill_observed']):>12} "
            f"{pct(k['win_stated']) + '/' + pct(k['win_observed']):>12} "
            f"{num(k['edge_fill_stated'], '{:+.2f}') + '/' + num(k['r_per_fill'], '{:+.2f}'):>14} "
            f"{num(k['edge_order_stated'], '{:+.2f}') + '/' + num(k['r_per_order'], '{:+.2f}'):>15} "
            f"{pct(k['ambiguous']):>6}")


def render_scope(horizon, c, ordering):
    o, b, sd = c["overall"], c["bootstrap_90"], c["ticker_spread"]
    other = "optimistic" if ordering == "pessimistic" else "pessimistic"
    lines = [f"HORIZON {horizon}: {o['cases']} case(s), {o['setups']} setups, {o['fills']} filled; exits: "
             + ", ".join(f"{n} {kind}" for kind, n in c["exits"].items()),
             "  st = stated by the setup (mean), obs = observed. win is among filled setups; R/fill is the mean "
             "result of filled setups, R/order counts unfilled ones as 0R.",
             HEAD, row("all", o)]
    for name in ("side", "kind", "side_and_kind", "order", "quality", "rank"):
        lines.append(f"    by {name.replace('_', ' ')}:")
        lines += [row(f"  {key}", k) for key, k in c[f"by_{name}"].items()]
    lines.append("    by ticker:")
    lines += [row(f"  {key}", k) for key, k in c["by_ticker"].items()]
    lines.append(f"  Brier, fill: {num(o.get('brier_fill'), '{:.3f}')} (always the observed rate "
                 f"{num(o.get('brier_fill_base_rate'), '{:.3f}')}); win: {num(o.get('brier_win'), '{:.3f}')} "
                 f"(always the observed rate {num(o.get('brier_win_base_rate'), '{:.3f}')}). 0 is perfect.")
    if b:
        lines.append("  Bootstrap over cases, 90%: fill " + "-".join(pct(x) for x in b["fill_observed"])
                     + (", win " + "-".join(pct(x) for x in b["win_observed"]) if "win_observed" in b else "")
                     + (", R/fill " + " to ".join(num(x, '{:+.2f}') for x in b["r_per_fill"]) if "r_per_fill" in b else "")
                     + ", R/order " + " to ".join(num(x, '{:+.2f}') for x in b["r_per_order"]))
    if sd:
        lines.append(f"  Per ticker, R/order: lowest {sd['r_per_order_min']:+.2f}, median {sd['r_per_order_median']:+.2f}, "
                     f"highest {sd['r_per_order_max']:+.2f}; positive in {sd['tickers_positive']} of {sd['tickers']} tickers.")
    lines.append(f"  Ambiguous candles changed {pct(o['ambiguous'])} of the setups; with the {other} ordering "
                 f"R/order would be {num(o['r_per_order_other'], '{:+.2f}')}.")
    for title, key in (("fill", "fill_buckets"), ("win if filled", "win_buckets")):
        lines.append(f"  Reliability, {title}:  " + "  ".join(
            f"{bucket['from']}-{bucket['to']}%: n {bucket['n']}, {bucket['mean_forecast']:.2f}->{bucket['observed']:.2f}"
            for bucket in c[key]))
    return lines


def render(report):
    s, summary = report["settings"], report["summary"]
    lines = ["BACKTEST of the trade setups (mechanical engine, trade_entries.py), hypothetical, for calibration",
             f"  {', '.join(s['tickers'])}; horizons {', '.join(s['horizons'])}; sim method {s['sim_method']}"
             + ("" if s["sim_method"] == s["default_sim_method"] else
                f" (rz.py sim uses method {s['default_sim_method']} by default)")
             + f"; ordering inside a candle: {s['ordering']}; setup rules {s.get('setup_rules', 1)}"]
    for horizon, r in s["ranges"].items():
        lines.append(f"  {horizon}: as-of dates {r['start']} to {r['end']}, every {r['step_trading_days']} trading day(s)")
    lines.append(f"  {summary['cases']} case(s), {summary['setups']} setups"
                 + ("; skipped: " + ", ".join(f"{n} {why}" for why, n in summary["skipped"].items())
                    if summary["skipped"] else ""))
    for name, reason in s["failed"].items():
        lines.append(f"  {name}: not backtested: {reason}")
    lines.append("  Rules:")
    for text in s["rules"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    lines.append("  Caveats:")
    for text in summary["caveats"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    for horizon, c in summary["per_horizon"].items():
        lines += [""] + render_scope(horizon, c, s["ordering"])
    return "\n".join(lines)


CSV_FIELDS = ["ticker", "horizon", "as_of", "price", "valid_until", "rank", "id", "side", "setup", "order",
              "quality", "entry", "stop", "target", "risk", "ratio", "fill", "win_if_filled", "if_filled",
              "per_order", "filled", "fill_date", "fill_price", "gap_fill", "touch_only", "exit", "exit_date",
              "exit_price", "r", "won", "ambiguous", "exit_other", "r_other"]


def write_csv(path, records):
    """One row per setup of every case."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        out = csv.DictWriter(handle, CSV_FIELDS, extrasaction="ignore")
        out.writeheader()
        for s in flat(records):
            out.writerow(s)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tickers", nargs="+", metavar="TICKER", help="Yahoo symbols, e.g. PETR4.SA NVDA")
    parser.add_argument("--horizon", nargs="+", choices=backtest.HORIZONS, default=["1-3m"],
                        help="one or more horizons (default: 1-3m)")
    parser.add_argument("--start", type=date.fromisoformat,
                        help=f"first as-of date (default: {backtest.DEFAULT_YEARS} years before --end)")
    parser.add_argument("--end", type=date.fromisoformat,
                        help="last as-of date (default: today minus the horizon, so every horizon has ended)")
    parser.add_argument("--step", type=int, metavar="N",
                        help="trading days between two as-of dates (default: "
                             + ", ".join(f"{h} {n}" for h, n in backtest.DEFAULT_STEP.items()) + ")")
    parser.add_argument("--method", type=int, choices=sorted(rz.SIM_METHODS), default=rz.SIM_METHOD,
                        help=f"sim method version of the projection (default: {rz.SIM_METHOD}, as a live run)")
    parser.add_argument("--optimistic", action="store_true",
                        help="resolve a candle that allows two results in the setup's favour (default: against it)")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the tables")
    parser.add_argument("--csv", metavar="FILE", help="also write every setup of every case to FILE")
    parser.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    parser.add_argument("--no-cache", action="store_true",
                        help="download every candle again instead of reusing the local candle cache")
    args = parser.parse_args(argv)
    if args.step is not None and args.step < 1:
        parser.error("--step must be at least 1")
    backtest.CACHE = not args.no_cache
    horizons = list(dict.fromkeys(args.horizon))
    try:
        report = run(args.tickers, horizons, args.start, args.end, args.step, progress=not args.quiet,
                     method=args.method, optimistic=args.optimistic)
    except backtest.BacktestError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if args.csv:
        write_csv(args.csv, report["records"])
        backtest.say(f"Wrote {args.csv}", not args.quiet)
    if args.json:
        print(json.dumps({"settings": report["settings"], **report["summary"]}, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report["summary"]["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
