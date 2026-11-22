"""Stop and target placement on the long setup backtest (README, "Stop and target placement").

Per case of the long run's grid (backtest_entries.py --universe backtest_universe.txt --start
2005-01-03, the default steps, each horizon's --end), it does what backtest_entries.run_case()
does up to the projection, draws the setups' simulated paths once (trade_entries.paths()), and
then, for every placement variant in VARIANTS, builds every valid setup the way
trade_entries.setups() does with that variant's stop and target, counts its RAW path odds (before
CORRECTION) and walks it on the real daily candles under both orderings
(backtest_entries.walk()). The correction, the ranking and the pick are applied afterwards by
analyse.py, so a refitted correction needs no new paths.

    uv run python examples/placement_2026-11-21/harness.py OUT --part fit --jobs 7 [--horizon H ...]
                       [--variants 0,5] [--tickers T ...]

Writes OUT/H/TICKER_N.npz (one per chunk of as-of dates): `cases` and one array per variant.
Only cases of the requested part (backtest_entries.part_of()) are made.
"""
import argparse
import bisect
import math
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest  # noqa: E402
import backtest_entries as be  # noqa: E402
import build_prompt  # noqa: E402
import mechanical  # noqa: E402
import score_projections as sp  # noqa: E402
import trade_entries as te  # noqa: E402

rz = backtest.rz
START = date(2005, 1, 3)
ENDS = {"1-2w": date(2026, 10, 30), "1-3m": date(2026, 8, 14), "6-12m": date(2025, 11, 13)}
CHUNK = 25


def variants():
    """(k, target, min_risk): the stop k x move beyond its edge; target ("zone",), ("frac", f) or
    ("R", x); min_risk a share of the entry price (0: none). Index 0 is rules 5."""
    out = []
    for k in (0.25, 0.5, 1.0, 1.5):
        for target in (("zone",), ("frac", 0.75), ("R", 1.5), ("R", 2.0), ("R", 3.0)):
            out.append((k, target, 0.0))
    for m in (0.01, 0.02):
        for k in (0.25, 0.5):
            for target in (("zone",), ("R", 2.0)):
                out.append((k, target, m))
    return out


VARIANTS = variants()
assert VARIANTS[0] == (0.25, ("zone",), 0.0)


def name(v):
    k, target, m = v
    t = "zone" if target[0] == "zone" else f"{target[1]:g}zone" if target[0] == "frac" else f"{target[1]:g}R"
    return f"k{k:g}-{t}" + (f"-min{m * 100:g}%" if m else "")


ROW = np.dtype([("case", "i4"), ("id", "U48"), ("side", "U5"), ("kind", "U12"), ("order", "U6"),
                ("entry", "f8"), ("stop", "f8"), ("target", "f8"), ("risk", "f8"), ("reward", "f8"),
                ("ratio", "f8"), ("fill", "f8"), ("win", "f8"), ("loss", "f8"), ("open", "f8"),
                ("if_filled", "f8"), ("filled", "?"), ("won", "?"), ("r", "f8"), ("r_other", "f8"),
                ("won_other", "?"), ("fill_price", "f8")])
CASE = np.dtype([("case", "i4"), ("ticker", "U16"), ("as_of", "U10"), ("valid_until", "U10"),
                 ("price", "f8"), ("part", "U8"), ("digits", "i4"), ("move", "f8")])


class Played:
    """The path odds of setups sharing a side, an order and an entry: the fill day per path and
    the running extremes after it are computed once, then each stop or target level is one
    comparison. Exactly the decisions of trade_entries.odds()."""

    def __init__(self, sim, side, order, entry):
        self.sim, self.long, self.order, self.entry = sim, side == "long", order, entry
        steps = self.steps = sim["steps"]
        high, low = sim["high"], sim["low"]
        days = np.arange(steps)[None, :]
        if order == "market":
            filled_on = np.full(len(high), -1)
        else:
            falling = (order == "limit") == self.long
            filled_on = te.first(low <= math.log(entry) if falling else high >= math.log(entry), steps)
        self.filled = filled_on < steps
        on = filled_on[:, None]
        after = days >= on
        later = (days > on) | ((days == on) & (order == "stop"))
        if self.long:   # stop below: running min of lows; target above: running max of highs
            self.stop_run = np.minimum.accumulate(np.where(after, low, np.inf), axis=1)
            self.target_run = np.maximum.accumulate(np.where(later, high, -np.inf), axis=1)
        else:
            self.stop_run = np.maximum.accumulate(np.where(after, high, -np.inf), axis=1)
            self.target_run = np.minimum.accumulate(np.where(later, low, np.inf), axis=1)
        self.stops, self.targets = {}, {}

    def stopped(self, stop):
        if stop not in self.stops:
            level = math.log(stop)
            run = self.stop_run
            self.stops[stop] = ((run > level) if self.long else (run < level)).sum(axis=1)
        return self.stops[stop]

    def reached(self, target):
        if target not in self.targets:
            level = math.log(target)
            run = self.target_run
            self.targets[target] = ((run < level) if self.long else (run > level)).sum(axis=1)
        return self.targets[target]

    def odds(self, stop, target, ratio):
        steps, filled, entry = self.steps, self.filled, self.entry
        stopped, reached = self.stopped(stop), self.reached(target)
        lost = filled & (stopped < steps) & (stopped <= reached)
        won = filled & ~lost & (reached < steps)
        still = filled & ~lost & ~won
        last = self.sim["last"]
        held = ((last - entry) if self.long else (entry - last)) / abs(entry - stop)
        r = np.where(lost, -1.0, np.where(won, ratio, np.where(still, held, 0.0)))
        count = int(filled.sum())
        share = lambda mask: float(mask.sum()) / count if count else 0.0
        return {"fill": float(filled.mean()), "win": share(won), "loss": share(lost), "open": share(still),
                "if_filled": float(r[filled].mean()) if count else 0.0}


def bases(proj):
    """The setups of trade_entries.setups() before their stop and target: (side, kind, zone id,
    order, entry, stop edge, target edge)."""
    price, out = proj["as_of"]["price"], []
    for listed, opposite, support in ((proj["supports"], proj["resistances"], True),
                                      (proj["resistances"], proj["supports"], False)):
        zones, first_opposite = te.bounded(listed), te.bounded(opposite)[0]["zone"]
        for index, zone in enumerate(zones):
            low, high = zone["zone"]
            near, far = (high, low) if support else (low, high)
            inside = low <= price <= high
            entry, order = (price, "market") if inside else (near, "limit")
            if inside or (entry <= price if support else entry >= price):
                out.append(("long" if support else "short", "reversal", zone["id"], order, entry, far,
                            first_opposite[0] if support else first_opposite[1]))
            if index + 1 < len(zones) and (far < price if support else far > price):
                following = zones[index + 1]
                out.append(("short" if support else "long", "continuation", zone["id"], "stop", far, near,
                            following["zone"][0] if support else following["zone"][1]))
    return out


def placed(base, variant, move, digits):
    """(stop, target, risk, reward, ratio) of one base setup under a variant, or None when the
    setup is not valid (trade_entries.setups()' add() tests, in its order)."""
    side, kind, zone_id, order, entry, edge, target_edge = base
    k, rule, minimum = variant
    long = side == "long"
    buffer = te.half_up(move * k, digits)
    stop = edge - buffer if long else edge + buffer
    if minimum:
        stop = min(stop, entry * (1 - minimum)) if long else max(stop, entry * (1 + minimum))
    if rule[0] == "zone":
        target = target_edge
    elif rule[0] == "frac":
        target = te.half_up(entry + rule[1] * (target_edge - entry), digits)
    else:
        stop_r = te.half_up(stop, digits)
        target = te.half_up(entry + (rule[1] if long else -rule[1]) * te.half_up(abs(entry - stop_r), digits), digits)
    ordered = stop < entry < target if long else target < entry < stop
    if not ordered:
        return None
    if min(te.half_up(x, digits) for x in (entry, stop, target)) <= 0:
        return None
    risk, reward = te.half_up(abs(entry - stop), digits), te.half_up(abs(target - entry), digits)
    if risk <= 0:
        return None
    ratio = te.half_up(reward / risk)
    if ratio < 0.5:
        return None
    return te.half_up(stop, digits), target, risk, reward, ratio


def make_case(ticker, horizon, data, day, today, part):
    """backtest_entries.run_case() up to the walk -> (meta, d, proj, candles) or None (skipped)."""
    if not data["rows"] or data["rows"][0][0] > be.history_start(horizon, day).isoformat():
        return None
    at = bisect.bisect_right([row[0] for row in data["rows"]], day.isoformat())
    if be.stale(data["rows"][max(0, at - be.STALE_DAYS):at]):
        return None
    try:
        d = backtest.input_as_of(ticker, horizon, data, day)
    except ValueError as error:
        if "nonexistent time" not in str(error):
            raise
        return None
    main = rz.unpack(d)[1]
    if len(main["rows"]) < build_prompt.PRESETS[horizon]["main"][2]:
        return None
    try:
        proj = mechanical.build(d)
    except (mechanical.MechanicalError, ArithmeticError, ValueError):
        return None
    if te.precondition(d, proj):
        return None
    valid_until = proj["horizon"]["last"]
    if be.part_of(day.isoformat(), valid_until) != part:
        return None
    if date.fromisoformat(valid_until) >= today:
        return None
    digits = te.places(d)
    walk_rows = [row for row in data["rows"] if day.isoformat() < row[0] <= valid_until]
    candles = [[row[0]] + [round(x, digits) for x in row[1:]] for row in walk_rows]
    if not candles or be.stale(walk_rows):
        return None
    return d, proj, candles, valid_until, digits


def case_rows(index, d, proj, candles, digits, chosen):
    """{variant index: [row tuples]} for one case."""
    move = te.units(proj, digits)["move"]
    sim = None
    played = {}
    out = {v: [] for v in chosen}
    for base in bases(proj):
        side, kind, zone_id, order, entry = base[:5]
        for v in chosen:
            got = placed(base, VARIANTS[v], move, digits)
            if got is None:
                continue
            stop, target, risk, reward, ratio = got
            if sim is None:
                sim = te.paths(d)
            key = (side, order, entry)
            if key not in played:
                played[key] = Played(sim, side, order, entry)
            result = played[key].odds(stop, target, ratio)
            if te.half_up(result["fill"]) < te.MIN_FILL:
                continue
            s = {"side": side, "order": order, "entry": entry, "stop": stop, "target": target, "risk": risk}
            pess, opt = be.walk(s, candles, False), be.walk(s, candles, True)
            out[v].append((index, f"{side}-{zone_id}-{kind}", side, kind, order, entry, stop, target, risk, reward,
                           ratio, result["fill"], result["win"], result["loss"], result["open"],
                           result["if_filled"], pess["filled"], pess["won"], pess["r"], opt["r"], opt["won"],
                           pess["fill_price"] if pess["filled"] else np.nan))
    return out


def chunk(task):
    ticker, horizon, data, days, today, part, chosen, path = task
    cases, rows = [], {v: [] for v in chosen}
    for day in days:
        made = make_case(ticker, horizon, data, day, today, part)
        if made is None:
            continue
        d, proj, candles, valid_until, digits = made
        index = len(cases)
        cases.append((index, ticker, day.isoformat(), valid_until, d["now"]["price"], part, digits,
                      te.units(proj, digits)["move"]))
        for v, found in case_rows(index, d, proj, candles, digits, chosen).items():
            rows[v] += found
    arrays = {"cases": np.array(cases, dtype=CASE)}
    for v in chosen:
        arrays[f"v{v}"] = np.array(rows[v], dtype=ROW)
    np.savez_compressed(path, **arrays)
    return len(cases)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--part", choices=("fit", "held-out"), required=True)
    parser.add_argument("--horizon", nargs="+", default=list(ENDS))
    parser.add_argument("--variants", default=None, help="comma-separated indices (default: all)")
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--jobs", type=int, default=7)
    args = parser.parse_args()
    chosen = [int(x) for x in args.variants.split(",")] if args.variants else list(range(len(VARIANTS)))
    universe = be.read_universe(os.path.join(ROOT, "backtest_universe.txt"))
    tickers = [t.upper() for t in args.tickers] if args.tickers else universe["tickers"]
    held = set(be.HELD_OUT_YEARS)
    pool = ProcessPoolExecutor(args.jobs, mp_context=multiprocessing.get_context("spawn"))
    futures = []
    for horizon in args.horizon:
        os.makedirs(os.path.join(args.out, horizon), exist_ok=True)
        step = backtest.DEFAULT_STEP[horizon]
        for ticker in tickers:
            data = backtest.download(ticker, START - timedelta(days=backtest.lookback(horizon)))
            if ticker in universe["spans"]:
                data = be.cut(data, *universe["spans"][ticker])
            today = sp.today_in(data["tz"])
            days = backtest.as_of_days(data["rows"], START, ENDS[horizon], step)
            # the cheap part of part_of(): an as-of date in a held-out year is never a fit case,
            # and one outside them never a held-out case
            days = [d for d in days if (d.year in held) == (args.part == "held-out")]
            for i in range(0, len(days), CHUNK):
                path = os.path.join(args.out, horizon, f"{ticker}_{i // CHUNK:04d}.npz")
                if os.path.exists(path):
                    continue
                futures.append((horizon, ticker, pool.submit(chunk, (ticker, horizon, data, days[i:i + CHUNK], today,
                                                                     args.part, chosen, path))))
    clock, done = time.monotonic(), 0
    for horizon, ticker, future in futures:
        n = future.result()
        done += 1
        if done % 50 == 0 or done == len(futures):
            print(f"{done}/{len(futures)} chunks ({horizon} {ticker}: {n} cases) {time.monotonic() - clock:.0f}s",
                  file=sys.stderr, flush=True)
    pool.shutdown()


if __name__ == "__main__":
    main()
