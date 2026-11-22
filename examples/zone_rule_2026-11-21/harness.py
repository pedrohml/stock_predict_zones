"""Zone-rule study (README, "A stronger zone rule"): candidate zone rules for the trade setups.

    uv run python examples/zone_rule_2026-11-21/harness.py collect PART DATA [--jobs 7] [--rules NAMES] [--sample N]
    uv run python examples/zone_rule_2026-11-21/harness.py verify DATA          # the base rule against backtest_entries.run_case()

`collect` walks the as-of grid of the long setup backtest (76 tickers, backtest_universe.txt,
--start 2005-01-03, the rules 5 report's --end per horizon, the default steps) and keeps only the
cases of PART (fit or held-out; part_of() is computed from the schedule before anything else, so
a case of the other part is never built). Per case it builds the input once
(backtest.input_as_of(), the same checks as backtest_entries.run_case()), the facts once, the
setups' simulated days once (trade_entries.paths()), then for every candidate rule its zones,
every valid setup by trade_entries.setups() with the RAW path odds (no CORRECTION; the shifts are
fitted and applied afterwards by analyse.py) and both walks (backtest_entries.walk()). Setups that
several rules share are stored once. The setup price rules are trade_entries.setups()' own: only
the zones differ. Nothing here changes rz.py or build_prompt.py.
"""
import bisect, math, multiprocessing, os, pickle, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be, build_prompt, trade_entries  # noqa: E402
import score_projections as sp  # noqa: E402

rz = backtest.rz
START = date(2005, 1, 3)
END = {"1-2w": date(2026, 10, 30), "1-3m": date(2026, 8, 14), "6-12m": date(2025, 11, 13)}
TODAY = date(2026, 11, 20)     # the rules 5 report's run day; every case here ended long before
HORIZONS = ("1-2w", "1-3m", "6-12m")

# ---- the candidate rules ------------------------------------------------------------------
# width: multiplier of the zone width (and so of the clustering distance and the filler spacing);
# min_members: a cluster with fewer members is not a zone; pivot: a cluster needs a pivot or a
# swing end among its members; recent: only the last N pivot lows and N pivot highs are
# candidates (None: all 25 that rz.facts() reports); pool: keep the `count` clusters with the
# most members among the nearest `pool` (ties: the nearer), then order them by distance.
BASE = {"width": 1.0, "min_members": 1, "pivot": False, "recent": None, "pool": None}
RULES = {
    "base": {},
    "w1.5": {"width": 1.5},
    "w2": {"width": 2.0},
    "m2": {"min_members": 2},
    "pivot": {"pivot": True},
    "recent10": {"recent": 10},
    "pool5": {"pool": 5},
    "w1.5_m2": {"width": 1.5, "min_members": 2},
    "w1.5_pivot": {"width": 1.5, "pivot": True},
    "w2_pivot": {"width": 2.0, "pivot": True},
    "w1.5_pool5": {"width": 1.5, "pool": 5},
    "w2_m2": {"width": 2.0, "min_members": 2},
}
EXTREMES = {"pivot_low", "pivot_high", "swing_low", "swing_high"}


def rule_of(name):
    return {**BASE, **RULES[name]}


def candidates(facts, recent=None):
    if recent is None:
        return backtest.candidates(facts)
    trimmed = dict(facts, pivot_lows=facts["pivot_lows"][-recent:], pivot_highs=facts["pivot_highs"][-recent:])
    return backtest.candidates(trimmed)


def zones(facts, rule, supports=3, resistances=3, places=2):
    """backtest.clustered_zones() with the candidate's parameters; BASE gives exactly its result."""
    price = float(facts["price"])
    width = max(round(round(sum(facts["typical_zone_width"]) / 2, places) * rule["width"], places), 10 ** -places)
    found = candidates(facts, rule["recent"])

    def side(count, is_support):
        sign = -1 if is_support else 1
        levels = sorted((c for c in found if (c[0] < price if is_support else c[0] > price)),
                        key=lambda c: abs(c[0] - price))
        clusters = []
        for level in levels:
            if clusters and abs(level[0] - clusters[-1][0][0]) <= width:
                clusters[-1].append(level)
            else:
                clusters.append([level])
        clusters = [c for c in clusters if len(c) >= rule["min_members"]
                    and (not rule["pivot"] or any(m[1] in EXTREMES for m in c))]
        if rule["pool"]:
            pool = list(enumerate(clusters[:rule["pool"]]))
            keep = sorted(pool, key=lambda ic: (-len(ic[1]), ic[0]))[:count]
            clusters = [c for _, c in sorted(keep)]
        beyond = lambda x, y: sign * (x - y) > 0
        out, edge = [], price
        for members in clusters[:count]:
            centre = sum(m[0] for m in members) / len(members)
            near, far = centre - sign * width / 2, centre + sign * width / 2
            if not out:
                near = near if beyond(near, price) else price
            elif not beyond(near, edge) and near != edge:
                near, far = edge, edge + sign * width
            out.append({"zone": sorted([round(float(near), places), round(float(far), places)]), "members": members})
            edge = out[-1]["zone"][0] if is_support else out[-1]["zone"][1]
        while len(out) < count:
            near = edge + sign * width
            far = near + sign * width
            if far <= 0:
                break
            out.append({"zone": sorted([round(near, places), round(far, places)]), "members": []})
            edge = out[-1]["zone"][0] if is_support else out[-1]["zone"][1]
        return out

    return {"supports": side(supports, True), "resistances": side(resistances, False)}


# ---- one case ------------------------------------------------------------------------------

def stub(d, facts, chosen, closes, last, basis):
    """The parts of a projection trade_entries.setups() reads (mechanical.build() makes the same)."""
    proj = {"asset": {"ticker": d["asset"]["ticker"], "currency": d["asset"].get("currency", "unsure")},
            "as_of": {"time": d["now"]["time"], "price": d["now"]["price"]},
            "horizon": {"close_basis": basis, "closes": closes, "last": last},
            "structure": {"volatility": {"per": facts["candles"], **facts["volatility"]}}}
    for name, prefix, tail in (("supports", "S", "below"), ("resistances", "R", "above")):
        boxes = [{"id": f"{prefix}{i}", "zone": list(z["zone"])} for i, z in enumerate(chosen[name], 1)]
        edge = chosen[name][-1]["zone"][0 if name == "supports" else 1]
        boxes.append({"id": tail, "zone": [None, edge] if name == "supports" else [edge, None]})
        proj[name] = boxes
    return proj


def run_case(ticker, horizon, data, day, part, names):
    """-> {"ticker", "as_of", "valid_until", "price", "setups": [...], "rules": {name: [index, ...]},
    "zones": {name: (real zones, mean width / price)}} or {"skipped": reason}."""
    record = {"ticker": ticker, "horizon": horizon, "as_of": day.isoformat()}
    if not data["rows"] or data["rows"][0][0] > be.history_start(horizon, day).isoformat():
        return {**record, "skipped": "history"}
    at = bisect.bisect_right([row[0] for row in data["rows"]], day.isoformat())
    if be.stale(data["rows"][max(0, at - be.STALE_DAYS):at]):
        return {**record, "skipped": "stale"}
    try:
        d = backtest.input_as_of(ticker, horizon, data, day)
    except ValueError as error:
        if "nonexistent time" not in str(error):
            raise
        return {**record, "skipped": "dst"}
    loaded = rz.unpack(d)
    main = loaded[1]
    if len(main["rows"]) < build_prompt.PRESETS[horizon]["main"][2]:
        return {**record, "skipped": "rows"}
    dates, _ = rz.schedule(d, main, loaded[2])
    valid_until = dates[-1].isoformat()
    if be.part_of(record["as_of"], valid_until) != part:
        return {**record, "skipped": "other part"}
    if date.fromisoformat(valid_until) >= TODAY:
        return {**record, "skipped": "not ended"}
    digits = trade_entries.places(d)
    raw_candles = [row for row in data["rows"] if day.isoformat() < row[0] <= valid_until]
    candles = [[row[0]] + [round(x, digits) for x in row[1:]] for row in raw_candles]
    if not candles:
        return {**record, "skipped": "no candle"}
    if be.stale(raw_candles):
        return {**record, "skipped": "stale"}
    try:
        facts = rz.facts(*loaded)
    except (ArithmeticError, ValueError) as error:
        return {**record, "skipped": f"facts {type(error).__name__}"}
    sim = trade_entries.paths(d)
    memo, table, index, per_rule, zone_info = {}, [], {}, {}, {}
    real_odds = trade_entries.odds

    def odds(sim_, side, order, entry, stop, target, ratio):
        key = (side, order, entry, stop, target, ratio)
        if key not in memo:
            memo[key] = real_odds(sim_, side, order, entry, stop, target, ratio)
        last_odds[0] = (key, memo[key])
        return memo[key]

    last_odds = [None]
    trade_entries.odds = odds
    try:
        for name in names:
            chosen = zones(facts, rule_of(name), d["want"]["supports"], d["want"]["resistances"], digits)
            if not chosen["supports"] or not chosen["resistances"]:
                per_rule[name] = None
                continue
            proj = stub(d, facts, chosen, len(dates), valid_until, main["candles"])
            real = [z for side in chosen.values() for z in side if z["members"]]
            widths = [(z["zone"][1] - z["zone"][0]) / d["now"]["price"] for side in chosen.values() for z in side]
            zone_info[name] = (len(real), sum(widths) / len(widths))
            try:
                found = trade_entries.setups(d, proj, sim)
            except (ArithmeticError, ValueError) as error:
                per_rule[name] = f"setups {type(error).__name__}"
                continue
            mine = []
            for item in found:
                key = (item["id"], item["side"], item["setup"], item["order"], item["entry"], item["stop"], item["target"])
                if key not in index:
                    raw = memo[(item["side"], item["order"], item["entry"], item["stop"], item["target"], item["ratio"])]
                    s = {"side": item["side"], "order": item["order"], "entry": item["entry"], "stop": item["stop"],
                         "target": item["target"], "risk": item["risk"]}
                    pess, opt = be.walk(s, candles, False), be.walk(s, candles, True)
                    index[key] = len(table)
                    table.append((item["id"], item["side"], item["setup"], item["order"], item["entry"], item["stop"],
                                  item["target"], item["risk"], item["ratio"], raw["fill"], raw["win"], raw["loss"],
                                  raw["open"], raw["if_filled"], pess["filled"], pess["won"], pess["r"], opt["r"],
                                  opt["filled"], opt["won"], pess["exit"]))
                mine.append(index[key])
            per_rule[name] = mine
    finally:
        trade_entries.odds = real_odds
    return {**record, "valid_until": valid_until, "price": d["now"]["price"], "setups": table,
            "rules": per_rule, "zones": zone_info}


FIELDS = ("id", "side", "setup", "order", "entry", "stop", "target", "risk", "ratio", "fill", "win", "loss",
          "open", "if_filled", "filled", "won", "r", "r_other", "filled_other", "won_other", "exit")


def cases(task):
    ticker, horizon, data, days, part, names = task
    return [run_case(ticker, horizon, data, day, part, names) for day in days]


# ---- the whole grid --------------------------------------------------------------------------

def downloads(horizon):
    uni = be.read_universe(os.path.join(ROOT, "backtest_universe.txt"))
    for ticker in uni["tickers"]:
        try:
            data = backtest.download(ticker, START - timedelta(days=backtest.lookback(horizon)))
        except backtest.BacktestError as reason:
            print(f"{ticker} {horizon}: {reason}", file=sys.stderr)
            continue
        if ticker in uni["spans"]:
            data = be.cut(data, *uni["spans"][ticker])
        yield ticker, data


def collect(part, out, jobs=7, names=None, sample=1, horizons=HORIZONS):
    names = names or list(RULES)
    os.makedirs(out, exist_ok=True)
    pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"))
    try:
        for horizon in horizons:
            clock, futures = time.monotonic(), []
            for ticker, data in downloads(horizon):
                days = backtest.as_of_days(data["rows"], START, END[horizon], backtest.DEFAULT_STEP[horizon])[::sample]
                for i in range(0, len(days), be.CHUNK):
                    futures.append(pool.submit(cases, (ticker, horizon, data, days[i:i + be.CHUNK], part, names)))
            records = []
            for k, f in enumerate(futures):
                records += [r for r in f.result() if "skipped" not in r or r["skipped"] not in ("other part",)]
                if k % 50 == 0:
                    print(f"{horizon}: {k}/{len(futures)} chunks, {time.monotonic() - clock:.0f}s", file=sys.stderr)
            path = os.path.join(out, f"{part}_{horizon}.pkl")
            with open(path, "wb") as handle:
                pickle.dump({"names": names, "fields": FIELDS, "records": records}, handle, protocol=5)
            used = sum(1 for r in records if "skipped" not in r)
            print(f"{horizon}: {used} cases -> {path} ({time.monotonic() - clock:.0f}s)", file=sys.stderr)
    finally:
        pool.shutdown(cancel_futures=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["collect"])
    p.add_argument("part", choices=["fit", "held-out"])
    p.add_argument("out")
    p.add_argument("--jobs", type=int, default=7)
    p.add_argument("--rules", nargs="+")
    p.add_argument("--sample", type=int, default=1)
    p.add_argument("--horizon", nargs="+", default=list(HORIZONS))
    a = p.parse_args()
    collect(a.part, a.out, a.jobs, a.rules, a.sample, a.horizon)
