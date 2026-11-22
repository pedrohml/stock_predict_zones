#!/usr/bin/env python3
"""Score the saved trade setups (entries.json) against what the price did afterwards.

    python score_entries.py                       # every entries.json in projections/
    python score_entries.py --ticker PETR4.SA     # one ticker
    python score_entries.py projections/PETR4.SA/2026-11-18T1515_1-3m   # one saved folder
    python score_entries.py --runs                # also the run folders under ./runs
    python score_entries.py --optimistic          # the other bound of the ordering inside a candle
    python score_entries.py --json                # the same, for a program

Every pipeline run writes three hypothetical setups (entry, stop, target, stated odds) next to
its projection, and `rz.py save` keeps a copy in the saved history. This plays each one on the
DAILY candles after its as-of day, up to its valid_until, with backtest_entries.walk(): the very
function, and so the very rules, of the backtest of the setups. Unlike that backtest, nothing is
replayed: these are the setups as they were written, before the prices existed, so this is the
forward, out-of-sample check.

Each setup is reported with its stated fill, win and edge next to what happened (open or ended,
filled or not, the exit and the result in R). Setups whose valid_until has passed go into a
summary per rules version of trade_entries.py (their odds mean different things): stated
against observed fill, win and R, Brier scores against the constant forecast, by horizon, side,
kind, order, rank, quality and who wrote the setups. It checks the calibration of the stated
odds of hypothetical setups. It is not a strategy, says nothing about returns, and is not advice.
"""
import argparse
import hashlib
import json
import os
import sys
import textwrap
from datetime import date, timedelta

import backtest_entries as be
import score_projections as sp
import trade_entries

ENTRIES = "entries.json"
LOOKBACK_DAYS = 45      # daily candles before the as-of day: the scale check needs the last closed one
RULES_MEANING = {
    1: "fill and win_if_filled were taken from the projection's reach and turn by formulas; "
       "win_if_filled was a ceiling, since the projection does not know whether the stop comes first",
    2: "fill and win_if_filled were counted on simulated price paths",
    7: "as rules 6, and 1-2w setups whose risk was under 2% of the entry were discarded",
    8: "as rules 7, and only setups whose reward was at least their risk (ratio at least 1) were kept",
}

RULES = [
    "The walk starts with the first daily candle after the as-of day (the day of as_of.time) and "
    "ends with the candle of valid_until, the horizon's last close. A run made during a session "
    "leaves the rest of that session out, since its candle also holds prices from before the "
    "setups. Daily candles are used whatever the horizon's close basis.",
    *be.RULES[1:],
    "A setup is ended once valid_until is in the past at the exchange. Before that it is open: a "
    "stop or target already hit is final, a filled setup still running is marked at the latest "
    "close (exit 'open', provisional R), and an order not filled yet may still fill.",
]

CAVEATS = [
    "These are the setups real runs wrote, scored after the fact: a calibration check of the "
    "stated odds of hypothetical setups, not a strategy, not a claim about returns, and not advice.",
    "No commissions, spread, slippage, borrowing costs or dividends; prices are split-adjusted and "
    "not dividend-adjusted. Touch fills are generous for limit orders.",
    "The ordering inside a daily candle is unknown and counted pessimistically by default "
    "(--optimistic gives the other bound).",
    "The sample is small and overlapping: the three setups of a run share one price path, runs of "
    "one ticker overlap in time, tickers move together, and the tickers were chosen by the user. "
    "A few dozen setups are anecdote; the bootstrap resamples whole runs and still understates "
    "the uncertainty.",
    "Rules versions are reported apart: their odds mean different things and must not be pooled.",
]


class Unscorable(Exception):
    """A saved folder whose setups cannot be played; the message is the reason."""


# ---- one folder ------------------------------------------------------------------------------

def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_entries(folder):
    """-> (reply, sha256) or raises Unscorable. None when the folder has no entries.json."""
    path = os.path.join(folder, ENTRIES)
    if not os.path.isfile(path):
        return None
    try:
        raw = open(path, "rb").read()
        reply = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise Unscorable(f"{ENTRIES} is unreadable ({type(error).__name__}: {str(error)[:120]})")
    if not isinstance(reply, dict):
        raise Unscorable(f"{ENTRIES} is not a JSON object")
    if "error" in reply and "entries" not in reply:
        raise Unscorable(f"{ENTRIES} holds no setups: {str(reply['error'])[:200]}")
    return reply, hashlib.sha256(raw).hexdigest()


def entries_made_by(folder):
    """Who wrote the setups, from the `entries` block of provenance.json: "computed"
    (trade_entries.py build) or "claude" (--entries-by-claude), with the model when recorded.
    "unrecorded" without that block."""
    try:
        record = read_json(os.path.join(folder, "provenance.json"))
        block = record.get("entries") if isinstance(record, dict) else None
    except (OSError, ValueError):
        block = None
    if not isinstance(block, dict):
        return {"by": sp.UNRECORDED}
    if block.get("made_by") == "claude-code":
        claude = block.get("claude") or {}
        used = claude.get("models_used") or []
        model = claude.get("model") or (used[0] if used else None)
        return {"by": "claude", **({"model": model} if model else {})}
    if block.get("made_by") == "trade_entries.py":
        return {"by": "computed"}
    return {"by": str(block.get("made_by") or sp.UNRECORDED)}


def read_input(folder):
    try:
        return read_json(os.path.join(folder, "input.json"))
    except (OSError, ValueError):
        return None


def places_of(d, reply):
    """The precision the setups were rounded to: the input's candle rows (trade_entries.places),
    else the most decimals the setup prices show, at least 2."""
    if d is not None:
        return trade_entries.places(d)
    shown = [repr(float(e[k]["price"])) for e in reply["entries"] for k in ("entry", "stop", "target")]
    return max([2] + [len(x.split(".")[1]) for x in shown if "." in x and "e" not in x])


def plan(folder, reply, d):
    """(ticker, first day to fetch, timezone) of one entries file."""
    ticker = str(reply["asset"]["ticker"]).upper()
    as_of = date.fromisoformat(reply["as_of"]["time"][:10])
    tz = ((d or {}).get("data") or {}).get("timezone") or "America/New_York"
    return ticker, as_of - timedelta(days=LOOKBACK_DAYS), tz


def score_folder(rz, folder, o, reply, prices, optimistic=False):
    """One saved folder with an entries file -> a record with every setup played, or `not_scored`."""
    record = {"folder": folder, "entries_by": entries_made_by(folder)}
    origin = sp.made_by(folder)
    if origin:
        record["made_by"] = origin
    try:
        record.update(ticker=str(reply["asset"]["ticker"]).upper(), horizon=o["horizon"]["label"],
                      as_of=reply["as_of"]["time"], price=reply["as_of"]["price"],
                      valid_until=reply["valid_until"], rules=reply.get("rules", 1))
        setups = [be.setup_of(entry) for entry in reply["entries"]]
        d = read_input(folder)
        ticker, start, tz = plan(folder, reply, d)
        as_of_day = record["as_of"][:10]
        if record["valid_until"] <= as_of_day:
            raise Unscorable("the horizon ends on the as-of day, and the walk uses the daily candles after it")
        rows, splits = prices.get(ticker, "1d", start, tz)
        later = [f"{when} ({ratio:g} for 1)" for when, ratio in splits if when > as_of_day]
        if later:
            raise Unscorable(f"stock split after the setups, on {', '.join(later)}: their prices are on the old scale")
        places = places_of(d, reply)
        if d is not None:
            main = rz.main_series(d)
            if main["candles"] in ("1D", "1W", "1M"):
                daily = [[row[0]] + [round(x, places) for x in row[1:]] for row in rows]
                sp.check_scale(rz, main, main["candles"], sp.aggregate(daily, main["candles"]))
        candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in rows
                   if as_of_day < row[0][:10] <= record["valid_until"]]
        ended = record["valid_until"] < sp.today_in(tz).isoformat()
        if ended and not candles:
            raise Unscorable("no daily candle between the as-of day and valid_until")
        record.update(status="ended" if ended else "open", candles=len(candles),
                      last_candle=candles[-1][0] if candles else None)
        played = []
        for s in setups:
            chosen, other = be.walk(s, candles, optimistic), be.walk(s, candles, not optimistic)
            if not ended:     # an expiry before valid_until is a setup still running
                for result in (chosen, other):
                    if result["exit"] == "expiry":
                        result["exit"] = "open"
            played.append({**s, **chosen, "r_other": other["r"], "exit_other": other["exit"],
                           "ambiguous": (chosen["exit"], chosen["r"]) != (other["exit"], other["r"])})
        record["setups"] = played
        if ended and candles and candles[-1][0] < record["valid_until"]:
            record["note"] = (f"the last daily candle found is {candles[-1][0]}, before valid_until "
                              f"{record['valid_until']}; an expiry is at its close")
    except Unscorable as reason:
        record["not_scored"] = str(reason)
    except sp.Unscorable as reason:
        record["not_scored"] = str(reason)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
        record["not_scored"] = f"malformed saved files ({type(error).__name__}: {error})"
    return record


# ---- the whole history -----------------------------------------------------------------------

def found_folders(folder=None, runs=None, ticker=None):
    """[(folder, projection, source)] of the saved history (`folder`, default rz.py's), then of
    the runs folder when given; one ticker if asked."""
    rz = sp.load_rz()
    found = [(here, o, "history") for here, o in rz.saved(folder)]
    if runs and os.path.isdir(runs):
        found += [(here, o, "runs") for here, o in rz.saved(runs)]
    if ticker:
        found = [item for item in found
                 if str(item[1].get("asset", {}).get("ticker", "")).upper() == ticker.upper()]
    return rz, found


def run(folder=None, runs=None, ticker=None, optimistic=False):
    """Score every entries.json -> {"settings", "records", "without_entries", "duplicates", "summary"}.
    A run folder and its saved copy hold the same file: it is scored once (the history's copy)."""
    rz, found = found_folders(folder, runs, ticker)
    prices, todo, without, seen, duplicates, records = sp.Prices(), [], [], set(), 0, []
    for here, o, source in found:
        try:
            got = read_entries(here)
        except Unscorable as reason:
            records.append({"folder": here, "source": source, "not_scored": str(reason)})
            continue
        if got is None:
            without.append(here)
            continue
        reply, digest = got
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        todo.append((here, o, source, reply))
        try:   # so each ticker is downloaded once, from the earliest day needed
            symbol, start, _ = plan(here, reply, read_input(here))
            prices.need(symbol, "1d", start)
        except Exception:
            pass   # score_folder reports it
    for here, o, source, reply in todo:
        records.append({"source": source, **score_folder(rz, here, o, reply, prices, optimistic)})
    ordering = "optimistic" if optimistic else "pessimistic"
    return {"settings": {"ordering": ordering, "rules": RULES, "caveats": CAVEATS,
                         "setup_rules_now": trade_entries.RULES},
            "records": records, "without_entries": without, "duplicates": duplicates,
            "summary": summarise(records)}


def flat(records, status=None):
    """Every played setup as one row, with its run's ticker, horizon, dates, rules and author."""
    return [{"folder": r["folder"], "ticker": r["ticker"], "horizon": r["horizon"], "as_of": r["as_of"], "valid_until": r["valid_until"],
             "rules": r["rules"], "entries_by": r["entries_by"]["by"], "status": r["status"], **s}
            for r in records if "setups" in r and (status is None or r["status"] == status)
            for s in r["setups"]]


def summarise(records):
    """Over the ended setups, one scope per rules version (be.scope(): overall, by side, kind,
    order, quality, rank, ticker, with Brier scores, buckets and a bootstrap), plus by horizon
    and by who wrote the setups."""
    ended, open_ = flat(records, "ended"), flat(records, "open")
    out = {"runs": sum(1 for r in records if "setups" in r), "ended_setups": len(ended),
           "open_setups": len(open_), "not_scored": sum(1 for r in records if "not_scored" in r),
           "caveats": CAVEATS, "by_rules": {}}
    for version in sorted({s["rules"] for s in ended}, key=str):
        mine = [s for s in ended if s["rules"] == version]
        scope = be.scope(mine)
        for key, name in (("horizon", "horizon"), ("entries_by", "author")):
            scope[f"by_{name}"] = {str(v): be.stats([s for s in mine if s[key] == v])
                                   for v in sorted({s[key] for s in mine}, key=str)}
        scope["runs"] = len({s["folder"] for s in mine})
        scope["meaning"] = RULES_MEANING.get(version, f"rules {version}")
        out["by_rules"][str(version)] = scope
    return out


# ---- report ----------------------------------------------------------------------------------

def price(x):
    return f"{x:g}"


def author_line(r):
    by = r["entries_by"]
    who = {"computed": "computed by trade_entries.py", "claude": "written by Claude Code (--entries-by-claude)"
           + (f", {by['model']}" if by.get("model") else ""),
           sp.UNRECORDED: "author unrecorded (no entries block in provenance.json)"}.get(by["by"], f"made by {by['by']}")
    model = (r.get("made_by") or {}).get("model")
    return f"  setups {who}; rules {r.get('rules', '?')}" + (f"; projection by {model}" if model else "")


SETUP_HEAD = (f"  {'#':>1} {'side':<5} {'kind':<12} {'order':<6} {'entry':>9} {'stop':>9} {'target':>9}  "
              f"{'fill':>4} {'win':>4} {'R/ord':>5}  {'filled':<10} {'exit':<7} {'R':>6}  other")


def setup_line(s):
    filled = s["fill_date"] if s["filled"] else "no"
    exit_ = s["exit"] or "-"
    other = "same" if not s["ambiguous"] else f"{s['exit_other'] or '-'} {s['r_other']:+.2f}"
    return (f"  {s['rank']:>1} {s['side']:<5} {s['setup']:<12} {s['order']:<6} {price(s['entry']):>9} "
            f"{price(s['stop']):>9} {price(s['target']):>9}  {s['fill']:>4.2f} {s['win_if_filled']:>4.2f} "
            f"{s['per_order']:>+5.2f}  {filled:<10} {exit_:<7} {s['r']:>+6.2f}  {other}")


def render_record(r):
    head = (f"{r.get('ticker', '?')} {r.get('horizon', '?')}  as of {r.get('as_of', '?')} at {r.get('price', '?')}")
    if "not_scored" in r:
        return [head, f"  NOT SCORED: {r['not_scored']}", f"  {r['folder']}"]
    state = (f"  ENDED {r['valid_until']}" if r["status"] == "ended"
             else f"  OPEN until {r['valid_until']}: PROVISIONAL, so far")
    lines = [head + state, author_line(r),
             f"  {r['candles']} daily candle(s) after the as-of day"
             + (f", the last {r['last_candle']}" if r["last_candle"] else ""),
             "  stated: fill, win if filled, edge per order (R); observed: fill day, exit, R; "
             "other: the result with the other ordering when a candle allowed two",
             SETUP_HEAD]
    lines += [setup_line(s) for s in r["setups"]]
    if r.get("note"):
        lines.append(f"  note: {r['note']}")
    lines.append(f"  {r['folder']}")
    return lines


def render_rules(version, c, ordering):
    o = c["overall"]
    other = "optimistic" if ordering == "pessimistic" else "pessimistic"
    lines = [f"RULES {version}: {c['runs']} run(s), {o['setups']} ended setups, {o['fills']} filled; exits: "
             + ", ".join(f"{n} {kind}" for kind, n in c["exits"].items())]
    lines += textwrap.wrap(f"Meaning of the odds: {c['meaning']}.", 100, initial_indent="  ", subsequent_indent="  ")
    lines += ["  st = stated by the setup (mean), obs = observed. win is among filled setups; R/fill is the mean "
              "result of filled setups, R/order counts unfilled ones as 0R.", be.HEAD, be.row("all", o)]
    for name in ("horizon", "side", "kind", "side_and_kind", "order", "quality", "rank", "author", "ticker"):
        lines.append(f"    by {name.replace('_', ' ')}:")
        lines += [be.row(f"  {key}", k) for key, k in c[f"by_{name}"].items()]
    lines.append(f"  Brier, fill: {be.num(o.get('brier_fill'), '{:.3f}')} (constant forecast at the observed rate "
                 f"{be.num(o.get('brier_fill_base_rate'), '{:.3f}')}); win: {be.num(o.get('brier_win'), '{:.3f}')} "
                 f"(constant {be.num(o.get('brier_win_base_rate'), '{:.3f}')}). 0 is perfect.")
    b = c["bootstrap_90"]
    if b:
        lines.append("  Bootstrap over runs, 90%: fill " + "-".join(be.pct(x) for x in b["fill_observed"])
                     + (", win " + "-".join(be.pct(x) for x in b["win_observed"]) if "win_observed" in b else "")
                     + ", R/order " + " to ".join(be.num(x, '{:+.2f}') for x in b["r_per_order"]))
    lines.append(f"  Ambiguous candles changed {be.pct(o['ambiguous'])} of the setups; with the {other} ordering "
                 f"R/order would be {be.num(o['r_per_order_other'], '{:+.2f}')}.")
    for title, key in (("fill", "fill_buckets"), ("win if filled", "win_buckets")):
        lines.append(f"  Reliability, {title}:  " + ("  ".join(
            f"{x['from']}-{x['to']}%: n {x['n']}, {x['mean_forecast']:.2f}->{x['observed']:.2f}"
            for x in c[key]) or "-"))
    return lines


def render(report):
    s, summary = report["settings"], report["summary"]
    lines = ["SCORE of the saved trade setups, hypothetical, for calibration",
             f"  {summary['runs']} run(s) scored: {summary['ended_setups']} ended setups, {summary['open_setups']} open"
             + (f"; {summary['not_scored']} not scored" if summary["not_scored"] else "")
             + (f"; {len(report['without_entries'])} saved projection(s) without {ENTRIES}" if report["without_entries"] else "")
             + (f"; {report['duplicates']} copy(ies) of a file already counted" if report["duplicates"] else ""),
             f"  ordering inside a candle: {s['ordering']}; the setups' rules are now version {s['setup_rules_now']}",
             "  Rules:"]
    for text in s["rules"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    lines.append("  Caveats:")
    for text in s["caveats"]:
        lines += textwrap.wrap(text, 100, initial_indent="  - ", subsequent_indent="    ")
    if not summary["by_rules"]:
        lines += ["", "  No setup has reached its valid_until yet, so there is nothing to summarise."]
    for version, c in summary["by_rules"].items():
        lines += [""] + render_rules(version, c, s["ordering"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", nargs="?",
                        help="the folder of saved projections, or one saved projection inside it "
                             "(default: $RZ_PROJECTIONS_DIR, else ./projections)")
    parser.add_argument("--ticker", help="only setups of this ticker, e.g. PETR4.SA")
    parser.add_argument("--runs", nargs="?", const="runs", metavar="DIR",
                        help="also the run folders under DIR (default: ./runs); a file already in "
                             "the history is counted once")
    parser.add_argument("--optimistic", action="store_true",
                        help="resolve a candle that allows two results in the setup's favour (default: against it)")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the tables")
    parser.add_argument("--no-cache", action="store_true",
                        help="download every candle again instead of reusing finished ones from the "
                             "local candle cache (README: Data sources)")
    args = parser.parse_args(argv)
    sp.CACHE = not args.no_cache

    report = run(args.folder, args.runs, args.ticker, args.optimistic)
    records = report["records"]
    if not records:
        where = args.folder or os.environ.get("RZ_PROJECTIONS_DIR") or "projections"
        what = (f"{len(report['without_entries'])} saved projection(s), none with {ENTRIES}"
                if report["without_entries"] else "no saved projections")
        print(f"No trade setups to score under {where}" + (f" or {args.runs}" if args.runs else "")
              + (f" for {args.ticker}" if args.ticker else "") + f": {what}.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for record in records:
            print("\n".join(render_record(record)) + "\n")
        print(render(report))
    scored = [r for r in records if "setups" in r]
    return 1 if not scored else 0


if __name__ == "__main__":
    sys.exit(main())
