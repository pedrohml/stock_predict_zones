"""Four ways to remove losing setups, judged together on universe v2 (README, "Four ways to remove
losing setups, judged together on universe v2: protocol").

  uv run python examples/combo_2026-11-22/combo.py run OUT.csv UNIVERSE_FILE [--part fit] [--jobs 14] [--horizons ...]
  uv run python examples/combo_2026-11-22/combo.py selftest
  uv run --with scikit-learn==1.9.1 python examples/combo_2026-11-22/combo.py fit OUT_FIT.csv CACHE_DIR      # writes FROZEN.json
  uv run --with scikit-learn==1.9.1 python examples/combo_2026-11-22/combo.py judge OUT_FIT.csv OUT_FRESH.csv CACHE_DIR   # once

`run` makes, per case of the grid (1-2w every 10 trading days, 1-3m every 20), exactly the rules 9
setups backtest_entries.run_case() makes, and for every variant the setups it keeps, walked on the
daily candles under both orderings. `base` is rules 9 unchanged (its rows equal backtest_entries.py's).
Variants: gapq95 gapq99 gapmax and rev5-50 rev5-30 rev21-50 rev21-30 (filters before the pick, at
both horizons), conf-above conf-inside (a confirmation entry, 1-2w only). Meta-labelling works on the
base rows at evaluation time, with examples/meta_2026-11-22/meta.py's model and features.
"""
import argparse, bisect, csv, importlib.util, json, math, multiprocessing, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be, build_prompt, mechanical, score_projections as sp  # noqa: E402
import trade_entries as te  # noqa: E402

rz = backtest.rz
START = date(2005, 1, 3)
ENDS = {"1-2w": date(2026, 10, 30), "1-3m": date(2026, 8, 14)}
STEP = {"1-2w": 10, "1-3m": 20}
GAPS = {"gapq95": 0.95, "gapq99": 0.99, "gapmax": 1.0}
REVS = {"rev5-50": (5, 0.5), "rev5-30": (5, 0.3), "rev21-50": (21, 0.5), "rev21-30": (21, 0.3)}
CONFS = {"conf-above": "near", "conf-inside": "far"}
FAMILIES = {"gap": tuple(GAPS), "rev": tuple(REVS), "conf": tuple(CONFS)}
VARIANTS = ("base", *GAPS, *REVS, *CONFS)
GAP_SESSIONS = 250
MIN_TICKERS = 10
COST = 0.002
BLOCKS = ((2005, 2012), (2013, 2019), (2020, 2026))
META_BLOCKS = ((2010, 2012), (2013, 2019), (2020, 2026))
MIN_SHARE = 0.25
CHUNK = 25
FIELDS = ["variant", "ticker", "horizon", "as_of", "valid_until", "part", "price", "rank", "side", "setup", "order",
          "entry", "stop", "target", "risk", "ratio", "fill", "win_if_filled", "if_filled", "per_order",
          "filled", "won", "r", "r_other", "exit"]


# ---- the simulated days, with their closes (for the confirmation entry) ---------------------

def paths_with_closes(source):
    """trade_entries.paths(), the same draws in the same order, plus each day's close ("close", log)."""
    lib = te.rz()
    d, main, t, h, l, c = lib.unpack(source)
    method = lib.sim_method(None)
    walked, dates, frac, v, sub = lib.walk(d, main, t, c, {}, te.PATHS, lib.drift_of(main, method))
    n, start_price = len(dates), math.log(d["now"]["price"])
    sigma = np.repeat(np.linspace(v["assumed"][0], v["assumed"][1], n), sub) / np.sqrt(sub)
    sigma[:sub] *= np.sqrt(frac)
    per = te.DAYS.get(main["candles"], 1) / sub
    counts = [max(1, round(per * (frac if i < sub else 1.0))) for i in range(n * sub)]
    rng = np.random.default_rng(te.BRIDGE_SEED)
    closes = np.log(walked)
    before = np.full(te.PATHS, start_price)
    points, scale = [], []
    for i, m in enumerate(counts):
        day = sigma[i] / math.sqrt(m)
        if m > 1:
            noise = np.cumsum(rng.standard_normal((te.PATHS, m)) * day, axis=1)
            share = np.arange(1, m + 1) / m
            points.append(before[:, None] + noise - share * noise[:, -1:] + share * (closes[:, i] - before)[:, None])
        else:
            points.append(closes[:, i:i + 1])
        scale += [day] * m
        before = closes[:, i]
    end = np.concatenate(points, axis=1)
    start = np.concatenate([np.full((te.PATHS, 1), start_price), end[:, :-1]], axis=1)
    twice = 2 * (te.SPREAD * np.array(scale)) ** 2
    middle, gap = start + end, (end - start) ** 2
    high = (middle + np.sqrt(gap - twice * np.log(rng.random(end.shape)))) / 2
    low = (middle - np.sqrt(gap - twice * np.log(rng.random(end.shape)))) / 2
    return {"high": high, "low": low, "last": np.exp(end[:, -1]), "steps": end.shape[1], "method": method,
            "close": end}


def levels(item):
    """(near, far) edges of a reversal's zone: the limit entry is the near edge, detail.edge the far."""
    return item["entry"], item["detail"]["edge"]


def conf_odds(sim, item, which, minimum, digits):
    """The confirmation entry played on every simulated path -> fill, win, if_filled (R in actual risk)."""
    long, steps = item["side"] == "long", sim["steps"]
    near, far = levels(item)
    level = near if which == "near" else far
    stop, target = item["stop"], item["target"]
    high, low, close = sim["high"], sim["low"], sim["close"]
    days = np.arange(steps)[None, :]
    touch = (low <= math.log(near)) if long else (high >= math.log(near))
    back = (close >= math.log(level)) if long else (close <= math.log(level))
    stop_hit = (low <= math.log(stop)) if long else (high >= math.log(stop))
    target_hit = (high >= math.log(target)) if long else (low <= math.log(target))
    conf = te.first(touch & back, steps)
    broke = te.first(stop_hit, steps)
    entered = (conf < steps) & (broke > conf)
    idx = np.minimum(conf, steps - 1)
    fill = np.exp(close[np.arange(len(close)), idx])
    risk = np.abs(fill - stop)
    reward = (target - fill) if long else (fill - target)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(risk > 0, reward / risk, 0.0)
    entered &= (risk > 0) & (ratio >= te.MIN_RATIO) & (risk >= minimum * fill)
    on = conf[:, None]
    stopped = te.first(stop_hit & (days > on), steps)
    reached = te.first(target_hit & (days > on), steps)
    lost = entered & (stopped < steps) & (stopped <= reached)
    won = entered & ~lost & (reached < steps)
    still = entered & ~lost & ~won
    with np.errstate(divide="ignore", invalid="ignore"):
        held = np.where(risk > 0, ((sim["last"] - fill) if long else (fill - sim["last"])) / risk, 0.0)
    r = np.where(lost, -1.0, np.where(won, ratio, np.where(still, held, 0.0)))
    count = int(entered.sum())
    return {"fill": float(entered.mean()), "win": float(won.sum()) / count if count else 0.0,
            "if_filled": float(r[entered].mean()) if count else 0.0}


def walk_conf(item, which, minimum, digits, candles, optimistic):
    """The confirmation entry on the real daily candles, by walk()'s rules after the entry."""
    long = item["side"] == "long"
    near, far = levels(item)
    level = near if which == "near" else far
    stop, target = item["stop"], item["target"]
    out = {"filled": False, "won": False, "r": 0.0, "exit": None, "entry": item["entry"], "risk": item["risk"]}
    for i, (day, open_, high, low, close) in enumerate(candles):
        if (low <= stop) if long else (high >= stop):
            return out                                   # the zone broke before a confirmation
        touched = (low <= near) if long else (high >= near)
        if touched and ((close >= level) if long else (close <= level)):
            fill = te.half_up(close, digits)
            risk = te.half_up(abs(fill - stop), digits)
            reward = te.half_up((target - fill) if long else (fill - target), digits)
            if risk <= 0 or reward / risk < te.MIN_RATIO or risk < minimum * fill:
                return out                               # fails the rules at the actual entry: no trade
            s = {"side": item["side"], "stop": stop, "target": target, "risk": risk}
            rest = candles[i + 1:]
            if not rest:
                return dict(out, filled=True, entry=fill, risk=risk, exit="expiry", r=0.0)
            r, kind = held_from_close(s, fill, rest, optimistic)
            return dict(out, filled=True, entry=fill, risk=risk, exit=kind, won=kind == "target", r=r)
    return out


def held_from_close(s, fill, rest, optimistic):
    """(R, exit) of a position opened at `fill` (a close) and walked on the next days by walk()'s rules."""
    long, stop, target, risk = s["side"] == "long", s["stop"], s["target"], s["risk"]
    r = lambda price: round(((price - fill) if long else (fill - price)) / risk, 4)
    for day, open_, high, low, close in rest:
        if (open_ <= stop) if long else (open_ >= stop):
            return r(open_), "stop"
        if (open_ >= target) if long else (open_ <= target):
            return r(open_), "target"
        hs = (low <= stop) if long else (high >= stop)
        ht = (high >= target) if long else (low <= target)
        if hs and ht:
            return (r(target), "target") if optimistic else (r(stop), "stop")
        if hs:
            return r(stop), "stop"
        if ht:
            return r(target), "target"
    return r(rest[-1][4]), "expiry"


# ---- one case ---------------------------------------------------------------------------------

def gap_quantiles(rows, at):
    """Quantiles of |open / previous close - 1| over the GAP_SESSIONS sessions up to row `at` (exclusive)."""
    gaps = np.abs(np.array([rows[j][1] / rows[j - 1][4] - 1 for j in range(max(1, at - GAP_SESSIONS), at)]))
    if len(gaps) < 50:
        return None
    return {k: float(np.quantile(gaps, q)) for k, q in GAPS.items()}


def case(ticker, horizon, data, ranks, day, today):
    """Rows of one as-of date for every variant ([] when backtest_entries would skip the case)."""
    iso = day.isoformat()
    if not data["rows"] or data["rows"][0][0] > be.history_start(horizon, day).isoformat():
        return []
    dates = [row[0] for row in data["rows"]]
    at = bisect.bisect_right(dates, iso)
    if be.stale(data["rows"][max(0, at - be.STALE_DAYS):at]):
        return []
    try:
        d = backtest.input_as_of(ticker, horizon, data, day)
    except ValueError as error:
        if "nonexistent time" not in str(error):
            raise
        return []
    if len(rz.unpack(d)[1]["rows"]) < build_prompt.PRESETS[horizon]["main"][2]:
        return []
    try:
        proj = mechanical.build(d)
    except (mechanical.MechanicalError, ArithmeticError, ValueError):
        return []
    if te.precondition(d, proj):
        return []
    valid_until = proj["horizon"]["last"]
    if date.fromisoformat(valid_until) >= today:
        return []
    try:
        sim = paths_with_closes(d) if horizon in ("1-2w",) else None
        found = te.setups(d, proj, sim=sim)
    except (ArithmeticError, ValueError):
        return []
    places = te.places(d)
    after = [row for row in data["rows"] if iso < row[0] <= valid_until]
    if not after or be.stale(after):
        return []
    candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in after]
    part = be.part_of(iso, valid_until)
    price = proj["as_of"]["price"]
    gq = gap_quantiles(data["rows"], at)
    rank = ranks.get(iso, {})
    out = []

    def emit(variant, entries):
        for entry in entries:
            s = be.setup_of(entry)
            chosen, other = be.walk(s, candles, False), be.walk(s, candles, True)
            out.append({"variant": variant, "ticker": ticker, "horizon": horizon, "as_of": iso,
                        "valid_until": valid_until, "part": part, "price": price,
                        **{k: s[k] for k in ("rank", "side", "setup", "order", "entry", "stop", "target", "risk",
                                             "ratio", "fill", "win_if_filled", "if_filled", "per_order")},
                        "filled": chosen["filled"], "won": chosen.get("won"), "r": chosen["r"],
                        "r_other": other["r"], "exit": chosen["exit"]})

    def build(keep):
        return te.build(d, proj, found=keep).get("entries", []) if keep else []

    emit("base", build(found))
    if not out:
        return []                      # no base setup: the case is skipped for every variant
    for variant, key in ((v, v) for v in GAPS):
        keep = [i for i in found if gq is not None and i["risk"] >= gq[key] * i["entry"]]
        emit(variant, build(keep))
    for variant, (look, cut) in REVS.items():
        pct = rank.get(look)
        keep = [] if pct is None else [i for i in found if (pct <= cut if i["side"] == "long" else pct >= 1 - cut)]
        emit(variant, build(keep))
    if horizon == "1-2w":
        minimum = te.MIN_RISK.get(horizon, 0.0)
        for variant, which in CONFS.items():
            scored = []
            for item in found:
                if item["setup"] != "reversal" or item["order"] != "limit":
                    continue
                o = conf_odds(sim, item, which, minimum, places)
                if te.half_up(o["fill"]) < te.MIN_FILL:
                    continue
                fill, win, iff = te.half_up(o["fill"]), te.half_up(o["win"]), te.half_up(o["if_filled"])
                scored.append(dict(item, fill=fill, win_if_filled=win, if_filled=iff, per_order=te.half_up(fill * iff)))
            scored.sort(key=lambda i: (-i["per_order"], -i["fill"], i["id"]))
            for n, item in enumerate(te.pick(scored), 1):
                a, b = (walk_conf(item, which, minimum, places, candles, opt) for opt in (False, True))
                out.append({"variant": variant, "ticker": ticker, "horizon": horizon, "as_of": iso,
                            "valid_until": valid_until, "part": part, "price": price, "rank": n,
                            "side": item["side"], "setup": "reversal", "order": "confirm",
                            "entry": a["entry"], "stop": item["stop"], "target": item["target"], "risk": a["risk"],
                            "ratio": item["ratio"], "fill": item["fill"], "win_if_filled": item["win_if_filled"],
                            "if_filled": item["if_filled"], "per_order": item["per_order"],
                            "filled": a["filled"], "won": a["won"], "r": a["r"], "r_other": b["r"], "exit": a["exit"]})
    return out


def cases(task):
    ticker, horizon, data, ranks, days, today, part = task
    rows = []
    for day in days:
        r = case(ticker, horizon, data, ranks, day, today)
        if r and (part is None or r[0]["part"] == part):
            rows += r
    return rows


def cross_ranks(datas):
    """{ticker: {date: {5: pct, 21: pct}}}: each ticker's trailing return ranked across the universe
    (0 = the biggest loser), from closes up to that date; no rank with fewer than MIN_TICKERS."""
    import pandas as pd
    out = {t: {} for t in datas}
    for look in (5, 21):
        cols = {}
        for t, data in datas.items():
            closes = pd.Series([r[4] for r in data["rows"]], index=[r[0] for r in data["rows"]])
            cols[t] = closes / closes.shift(look) - 1
        frame = pd.DataFrame(cols).sort_index().ffill(limit=5)
        counts = frame.notna().sum(axis=1)
        pct = frame.rank(axis=1, method="average")
        pct = (pct - 1).div((counts - 1).where(counts >= MIN_TICKERS), axis=0)
        for t, data in datas.items():
            col = pct[t]
            for r in data["rows"]:
                v = col.get(r[0])
                if v is not None and v == v:
                    out[t].setdefault(r[0], {})[look] = float(v)
    return out


def run(out_path, universe_path, part, jobs, horizons):
    uni = be.read_universe(universe_path)
    pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"))
    with open(out_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        for horizon in horizons:
            datas = {}
            for ticker in uni["tickers"]:
                try:
                    data = backtest.download(ticker, START - timedelta(days=backtest.lookback(horizon)))
                except backtest.BacktestError as reason:
                    print(f"{ticker} {horizon}: not run: {reason}", flush=True)
                    continue
                if ticker in uni["spans"]:
                    data = be.cut(data, *uni["spans"][ticker])
                datas[ticker] = data
            ranks = cross_ranks(datas)
            pending = []
            for ticker, data in datas.items():
                today = sp.today_in(data["tz"])
                days = backtest.as_of_days(data["rows"], START, ENDS[horizon], STEP[horizon])
                futures = [pool.submit(cases, (ticker, horizon, data, ranks[ticker], days[i:i + CHUNK], today, part))
                           for i in range(0, len(days), CHUNK)]
                pending.append((ticker, futures))
            for ticker, futures in pending:
                n = 0
                for f in futures:
                    rows = f.result()
                    writer.writerows(rows)
                    n += len({r["as_of"] for r in rows})
                print(f"{ticker} {horizon}: {n} cases", flush=True)
    pool.shutdown()


def selftest():
    """paths_with_closes() equals trade_entries.paths(); base equals trade_entries.build(); the
    confirmation walk on hand-made candles."""
    uni = be.read_universe(os.path.join(ROOT, "backtest_universe.txt"))
    checked = 0
    for ticker in uni["tickers"][:3]:
        data = backtest.download(ticker, START - timedelta(days=backtest.lookback("1-2w")))
        days = backtest.as_of_days(data["rows"], START, ENDS["1-2w"], STEP["1-2w"])
        for day in days[::len(days) // 15]:
            try:
                d = backtest.input_as_of(ticker, "1-2w", data, day)
            except ValueError:
                continue
            a, b = te.paths(d), paths_with_closes(d)
            assert all(np.array_equal(a[k], b[k]) for k in ("high", "low", "last")) and a["steps"] == b["steps"]
            assert np.allclose(np.exp(b["close"][:, -1]), b["last"])
            rows = [r for r in case(ticker, "1-2w", data, {}, day, date(2026, 11, 22)) if r["variant"] == "base"]
            if rows:
                proj = mechanical.build(d)
                want = [(e["entry"]["price"], e["stop"]["price"], e["target"]["price"]) for e in te.build(d, proj)["entries"]]
                assert want == [(r["entry"], r["stop"], r["target"]) for r in rows]
                checked += 1
    item = {"side": "long", "entry": 10.0, "detail": {"edge": 9.5}, "stop": 9.3, "target": 11.5, "risk": 0.7}
    c = [["d1", 10.5, 10.6, 10.2, 10.4], ["d2", 10.3, 10.4, 9.8, 10.1], ["d3", 10.1, 11.6, 10.0, 11.4]]
    w = walk_conf(item, "near", 0.0, 2, c, False)       # touches 10 on d2, closes 10.1 >= 10: entry 10.1
    assert w["filled"] and w["entry"] == 10.1 and w["risk"] == 0.8 and w["r"] == round(1.4 / 0.8, 4)
    c2 = [["d1", 10.5, 10.6, 9.2, 10.1]]                 # reaches the stop on the touch day: cancelled
    assert not walk_conf(item, "far", 0.0, 2, c2, False)["filled"]
    print(f"selftest: paths and base equal rules 9 on {checked} cases; confirmation walk ok")


# ---- evaluation ---------------------------------------------------------------------------

def load(path):
    import pandas as pd
    df = pd.read_csv(path)
    filled = df["filled"].astype(str).str.lower().isin(["true", "1"])
    df["filled_b"] = filled
    df["net"] = df["r"] - np.where(filled, COST * df["entry"] / df["risk"], 0.0)
    df["month"] = df["as_of"].str[:7]
    df["year"] = df["as_of"].str[:4].astype(int)
    return df


def clustered(values, months):
    s = {}
    for v, m in zip(values, months):
        s[m] = s.get(m, 0.0) + v
    return math.sqrt(sum(x * x for x in s.values()))


def compare(v, b, blocks=BLOCKS):
    """Gross R per order of rows `v` against base rows `b` (same cases), clustered t, blocks, gaps."""
    if len(v) == 0:
        return None
    mv, mb = v["r"].mean(), b["r"].mean()
    infl_v, infl_b = (v["r"] - mv) / len(v), (b["r"] - mb) / len(b)
    se = clustered(list(infl_v) + list(-infl_b), list(v["month"]) + list(b["month"]))
    out_blocks = []
    for lo, hi in blocks:
        vv, bb = v[(v["year"] >= lo) & (v["year"] <= hi)], b[(b["year"] >= lo) & (b["year"] <= hi)]
        out_blocks.append(vv["r"].mean() - bb["r"].mean() if len(vv) and len(bb) else float("nan"))
    return {"orders": len(v), "base_orders": len(b), "share": len(v) / len(b), "long": float((v["side"] == "long").mean()),
            "fill": float(v["filled_b"].mean()), "gross": mv, "base_gross": mb, "diff": mv - mb,
            "t": (mv - mb) / se if se else 0.0, "blocks": out_blocks, "stated": v["per_order"].mean(),
            "opt": v["r_other"].mean(), "net": v["net"].mean(), "base_net": b["net"].mean(),
            "gap": abs(v["per_order"].mean() - mv), "base_gap": abs(b["per_order"].mean() - mb)}


def line(name, c):
    if c is None:
        return f"{name:12} no orders"
    blk = "/".join(f"{x:+.3f}" if x == x else "nan" for x in c["blocks"])
    return (f"{name:12} orders {c['orders']:6} ({c['share']:4.0%}) long {c['long']:4.0%} fill {c['fill']:4.0%} "
            f"R/o st {c['stated']:+.3f} obs {c['gross']:+.3f} (opt {c['opt']:+.3f}) vs base {c['base_gross']:+.3f} "
            f"diff {c['diff']:+.3f} t {c['t']:+.1f} [{blk}] gap {c['gap']:.3f} (base {c['base_gap']:.3f}) "
            f"net {c['net']:+.3f} (base {c['base_net']:+.3f})")


def passes(c):
    return c is not None and c["diff"] > 0 and c["t"] >= 3 and all(x > 0 for x in c["blocks"]) and c["share"] >= MIN_SHARE


def meta_module(fresh_universe):
    spec = importlib.util.spec_from_file_location("meta", os.path.join(ROOT, "examples", "meta_2026-11-22", "meta.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.UNIVERSES["fresh"] = fresh_universe
    m.BLOCKS = META_BLOCKS
    return m


def meta_table(m, df, which, cache_dir):
    """The base rows of one horizon with meta.py's features; the target is gross R."""
    import pandas as pd
    data = m.candles(which, cache_dir)
    df = df.copy()
    df["risk_pct"] = df["risk"] / df["price"]
    df["distance_pct"] = (df["entry"] - df["price"]).abs() / df["price"]
    frames = []
    for t, g in df.groupby("ticker", sort=False):
        if t in data:
            frames.append(g.join(m.features(data[t], g["horizon"].iloc[0]), on="as_of"))
    out = pd.concat(frames).sort_values(["as_of", "ticker", "rank"]).reset_index(drop=True)
    for k in m.CATEGORICAL:
        out[k] = out[k].astype("category")
    out["target"] = out["r"]
    return out


def meta_walk_forward(m, df, horizon):
    import pandas as pd
    pred = pd.Series(np.nan, index=df.index)
    for year in range(m.FIRST_TEST_YEAR, df["year"].max() + 1):
        test = df["year"] == year
        if not test.any():
            continue
        cut = (date(year, 1, 1) - timedelta(days=m.EMBARGO[horizon])).isoformat()
        train = df["valid_until"] < cut
        pred[test] = m.model().fit(df.loc[train, m.FEATURES], df.loc[train, "target"]).predict(df.loc[test, m.FEATURES])
    return pred


def fit(path, cache_dir):
    df = load(path)
    df = df[df["part"] == "fit"]
    frozen, report = {}, {}
    for horizon in ("1-2w", "1-3m"):
        h = df[df["horizon"] == horizon]
        b = h[h["variant"] == "base"]
        print(f"== {horizon}: {len(b)} base orders")
        for family, names in FAMILIES.items():
            best = None
            for name in names:
                c = compare(h[h["variant"] == name], b)
                if c is None and family == "conf" and horizon != "1-2w":
                    continue
                ok = passes(c)
                print(("* " if ok else "  ") + line(name, c))
                report[f"{horizon} {name}"] = c
                if ok and (best is None or c["gross"] > best[1]["gross"]):
                    best = (name, c)
            if best:
                frozen.setdefault(horizon, {})[family] = best[0]
        m = meta_module(None)
        t = meta_table(m, b, "fit", cache_dir)
        pred = meta_walk_forward(m, t, horizon)
        oos, p = t[pred.notna()], pred[pred.notna()]
        best = None
        for tau in m.TAUS:
            keep = (p > tau).to_numpy()
            c = compare(oos[keep], oos, META_BLOCKS)
            ok = passes(c)
            print(("* " if ok else "  ") + line(f"meta {tau:.2f}", c))
            report[f"{horizon} meta {tau}"] = c
            if c is not None and c["share"] >= MIN_SHARE and (best is None or c["gross"] > best[1]["gross"]):
                best = (tau, c)
        if best and passes(best[1]):
            frozen.setdefault(horizon, {})["meta"] = best[0]
    json.dump({"protocol": "README, Four ways to remove losing setups", "frozen": frozen, "fit": report},
              open(os.path.join(HERE, "FROZEN.json"), "w"), indent=1, default=float)
    print("frozen:", frozen or "none (universe v2 stays unread)")


def judge(fit_path, fresh_path, cache_dir):
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))["frozen"]
    if not frozen:
        sys.exit("nothing was frozen on the fit part")
    fit_df = load(fit_path)
    fit_df = fit_df[fit_df["part"] == "fit"]
    df = load(fresh_path)
    out = {}
    for horizon, items in frozen.items():
        h = df[df["horizon"] == horizon]
        b = h[h["variant"] == "base"]
        for family, choice in items.items():
            if family == "meta":
                m = meta_module(os.path.join(ROOT, "examples", "kinds_2026-11-22", "fresh_universe_v2.txt"))
                train = meta_table(m, fit_df[(fit_df["horizon"] == horizon) & (fit_df["variant"] == "base")], "fit", cache_dir)
                model = m.model().fit(train[m.FEATURES], train["target"])
                t = meta_table(m, b, "fresh", cache_dir)
                c = compare(t[model.predict(t[m.FEATURES]) > choice], t)
                name = f"meta {choice:.2f}"
            else:
                c, name = compare(h[h["variant"] == choice], b), choice
            c["adopted"] = bool(c["diff"] > 0 and c["t"] >= 2 and c["gap"] <= c["base_gap"] + 0.02)
            print(f"{horizon} " + line(name, c) + (" -> ADOPTED" if c["adopted"] else " -> not adopted"))
            out[f"{horizon} {family}"] = dict(c, choice=choice)
    json.dump(out, open(os.path.join(HERE, "judge.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        p = argparse.ArgumentParser()
        p.add_argument("cmd"); p.add_argument("out"); p.add_argument("universe")
        p.add_argument("--part"); p.add_argument("--jobs", type=int, default=14)
        p.add_argument("--horizons", nargs="+", default=["1-2w", "1-3m"])
        a = p.parse_args()
        t0 = time.time(); run(a.out, a.universe, a.part, a.jobs, a.horizons); print(f"{time.time() - t0:.0f}s")
    elif cmd == "selftest":
        selftest()
    elif cmd == "fit":
        fit(sys.argv[2], sys.argv[3])
    else:
        judge(sys.argv[2], sys.argv[3], sys.argv[4])
