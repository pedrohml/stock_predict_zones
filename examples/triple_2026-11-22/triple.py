"""Does the shape of the last candles predict direction? A triple-barrier test (README, "Does the shape
of the last candles predict direction? A triple-barrier test: protocol"). Needs scikit-learn:

  uv run --with scikit-learn==1.9.1 python examples/triple_2026-11-22/triple.py selftest
  uv run --with scikit-learn==1.9.1 python examples/triple_2026-11-22/triple.py fit CACHE_DIR [--jobs 14]   # writes FROZEN.json
  uv run --with scikit-learn==1.9.1 python examples/triple_2026-11-22/triple.py judge CACHE_DIR             # once

Daily candles come from backtest.download() (data_source: cache, retries, rate limit), with the
universe files' @ranges applied, and are pickled in CACHE_DIR so a rerun reads the same prices.
"""
import argparse, hashlib, json, math, multiprocessing, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

os.environ.setdefault("OMP_NUM_THREADS", "1")   # one thread per worker process
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be  # noqa: E402

UNIVERSES = {"fit": os.path.join(ROOT, "backtest_universe.txt"),
             "v2": os.path.join(ROOT, "examples", "kinds_2026-11-22", "fresh_universe_v2.txt")}
START = "2005-01-03"
EVERY = 5
HISTORY = 252
ATR_DAYS = 14
XS, NS, TARGETS, STOP = (5, 10, 20), (5, 10, 20), (1, 2, 3), 1
SIDES = ("long", "short")
CONFIGS = [(x, n, a, side) for x in XS for n in NS for a in TARGETS for side in SIDES]
FIRST_TEST_YEAR = 2010
EMBARGO_DAYS = 30
TAUS = (0.0, 0.05, 0.10, 0.20)
MIN_KEPT = 0.10
BLOCKS = ((2010, 2012), (2013, 2019), (2020, 2026))
PARAMS = dict(max_depth=3, learning_rate=0.05, max_iter=300, min_samples_leaf=200, l2_regularization=1.0,
              random_state=0)


# ---- data, samples, features, outcomes ---------------------------------------------------------

def candles(which, cache_dir):
    """{ticker: (dates, open, high, low, close)} for a universe, its @ranges applied."""
    path = os.path.join(cache_dir, f"triple_candles_{which}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    uni = be.read_universe(UNIVERSES[which])
    out = {}
    for t in uni["tickers"]:
        try:
            data = backtest.download(t, date(2003, 1, 1))
        except backtest.BacktestError as error:
            print(f"{t}: not used: {error}"[:150], flush=True)
            continue
        if t in uni["spans"]:
            data = be.cut(data, *uni["spans"][t])
        rows = data["rows"]
        out[t] = (np.array([r[0] for r in rows]),) + tuple(np.array([r[k] for r in rows], float) for k in range(1, 5))
    pd.to_pickle(out, path)
    return out


def outcome(o, h, l, c, t, n, a, side, atr):
    """R of one trade entered at close t, target a x ATR, stop STOP x ATR, time limit n days, by the
    protocol's rules (gap past a barrier: the open; both in one day: the stop) -> R or None."""
    if t + n >= len(c):
        return None
    entry, unit = c[t], STOP * atr
    up = side == "long"
    target = entry + a * atr if up else entry - a * atr
    stop = entry - unit if up else entry + unit
    sign = 1.0 if up else -1.0
    for k in range(t + 1, t + n + 1):
        if (o[k] <= stop) if up else (o[k] >= stop):
            return sign * (o[k] - entry) / unit
        if (o[k] >= target) if up else (o[k] <= target):
            return sign * (o[k] - entry) / unit
        hit_stop = (l[k] <= stop) if up else (h[k] >= stop)
        hit_target = (h[k] >= target) if up else (l[k] <= target)
        if hit_stop:
            return -1.0
        if hit_target:
            return a / STOP
    return sign * (c[t + n] - entry) / unit


def table(which, cache_dir):
    """One row per (ticker, sample day): features, flat-candle positions, every outcome."""
    path = os.path.join(cache_dir, f"triple_table_{which}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    frames = []
    for ticker, (d, o, h, l, c) in candles(which, cache_dir).items():
        prev = np.concatenate([[np.nan], c[:-1]])
        tr = np.nanmax(np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)]), axis=0)
        atr = pd.Series(tr).rolling(ATR_DAYS).mean().to_numpy()
        flat = (o == h) & (h == l) & (l == c)
        first = int(np.searchsorted(d, START))
        idx = [t for t in range(max(first, HISTORY), len(c)) if (t - first) % EVERY == 0]
        rows = []
        for t in idx:
            if not atr[t] > 0:
                continue
            row = {"ticker": ticker, "date": d[t], "t": t, "atr_pct": atr[t] / c[t],
                   "ret21": c[t] / c[t - 21] - 1, "ret63": c[t] / c[t - 63] - 1}
            for lag in range(max(XS)):
                k = t - lag
                for name, arr in (("o", o), ("h", h), ("l", l), ("c", c)):
                    row[f"{name}{lag}"] = (arr[k] - c[t]) / atr[t]
            row["flat_before"] = max([lag for lag in range(max(XS)) if flat[t - lag]], default=-1)
            row["flat_after"] = min([k - t for k in range(t + 1, min(len(c), t + max(NS) + 1)) if flat[k]], default=99)
            for n in NS:
                row[f"end{n}"] = d[t + n] if t + n < len(c) else None
                for a in TARGETS:
                    for side in SIDES:
                        row[f"r_{n}_{a}_{side}"] = outcome(o, h, l, c, t, n, a, side, atr[t])
            rows.append(row)
        frames.append(pd.DataFrame(rows))
        print(f"{which} {ticker}: {len(rows)} samples", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df["year"] = df["date"].str[:4].astype(int)
    df["month"] = df["date"].str[:7]
    pd.to_pickle(df, path)
    return df


def config_rows(df, x, n, a, side, part):
    """The samples of one configuration: outcome known, no flat candle in its X + N days, in `part`."""
    r = df[f"r_{n}_{a}_{side}"]
    ok = r.notna() & (df["flat_before"] < x) & (df["flat_after"] > n) & df[f"end{n}"].notna()
    sub = df[ok].copy()
    sub["r"] = sub[f"r_{n}_{a}_{side}"].astype(float)
    sub["end"] = sub[f"end{n}"]
    if part is not None:
        parts = [be.part_of(s, e) for s, e in zip(sub["date"], sub["end"])]
        sub = sub[np.array(parts) == part]
    return sub


def features(x):
    return ["atr_pct", "ret21", "ret63"] + [f"{k}{lag}" for lag in range(x) for k in "ohlc"]


def model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(**PARAMS)


# ---- statistics -----------------------------------------------------------------------------

def clustered(values, months):
    s = pd.Series(values).groupby(np.asarray(months)).sum()
    return math.sqrt(float((s ** 2).sum()))


def kept_stats(sub, keep):
    keep = np.asarray(keep, bool)
    r, months = sub["r"].to_numpy(), sub["month"].to_numpy()
    n, nk = len(r), int(keep.sum())
    if nk == 0:
        return {"kept": 0, "share": 0.0}
    mk, m = r[keep].mean(), r.mean()
    se_k = clustered((r[keep] - mk) / nk, months[keep])
    se_d = clustered(np.where(keep, (r - mk) / nk, 0.0) - (r - m) / n, months)
    years = sub["year"].to_numpy()
    blocks = [float(r[(years >= lo) & (years <= hi) & keep].mean() - r[(years >= lo) & (years <= hi)].mean())
              if ((years >= lo) & (years <= hi) & keep).any() else float("nan") for lo, hi in BLOCKS]
    return {"samples": n, "kept": nk, "share": nk / n, "r_kept": mk, "t_kept": mk / se_k if se_k else 0.0,
            "r_all": m, "diff": mk - m, "t": (mk - m) / se_d if se_d else 0.0, "blocks": blocks}


def passes(s):
    return (s.get("kept", 0) and s["share"] >= MIN_KEPT and s["diff"] > 0 and s["t"] >= 3
            and all(b > 0 for b in s["blocks"]))


def show(name, s):
    if not s.get("kept"):
        return f"{name:22} nothing kept"
    blk = "/".join(f"{b:+.3f}" if b == b else "nan" for b in s["blocks"])
    return (f"{name:22} kept {s['kept']:6}/{s['samples']:6} ({s['share']:4.0%}) R {s['r_kept']:+.3f} (t {s['t_kept']:+.1f}) "
            f"vs all {s['r_all']:+.3f} diff {s['diff']:+.3f} t {s['t']:+.1f} [{blk}]")


# ---- fit and judge --------------------------------------------------------------------------

def walk_forward(cfg_and_df):
    (x, n, a, side), sub = cfg_and_df
    cols = features(x)
    pred = pd.Series(np.nan, index=sub.index)
    for year in range(FIRST_TEST_YEAR, int(sub["year"].max()) + 1):
        test = sub["year"] == year
        if not test.any():
            continue
        cut = (date(year, 1, 1) - timedelta(days=EMBARGO_DAYS)).isoformat()
        train = sub["end"] < cut
        pred[test] = model().fit(sub.loc[train, cols], sub.loc[train, "r"]).predict(sub.loc[test, cols])
    return (x, n, a, side), pred


def fit(cache_dir, jobs):
    df = table("fit", cache_dir)
    subs = {cfg: config_rows(df, *cfg, "fit") for cfg in CONFIGS}
    results = {}
    with ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        for cfg, pred in pool.map(walk_forward, subs.items()):
            results[cfg] = pred
    report, best = {}, {}
    for cfg in CONFIGS:
        x, n, a, side = cfg
        sub, pred = subs[cfg], results[cfg]
        oos = sub[pred.notna()]
        p = pred[pred.notna()].to_numpy()
        base_rate = float(oos["r"].mean())
        hit = float((oos["r"] >= a / STOP - 1e-9).mean())
        print(f"== X {x:2} N {n:2} target {a} ATR {side:5}: {len(oos)} out-of-sample samples, base R {base_rate:+.3f}, "
              f"target hit {hit:.0%}")
        for tau in TAUS:
            s = kept_stats(oos, p > tau)
            ok = passes(s)
            print(("   * " if ok else "     ") + show(f"tau {tau:.2f}", s))
            report[f"{x} {n} {a} {side} {tau}"] = s
            if ok and (side not in best or s["r_kept"] > best[side][2]["r_kept"]):
                best[side] = (cfg, tau, s)
    frozen = {}
    for side, (cfg, tau, s) in best.items():
        x, n, a, _ = cfg
        sub = subs[cfg]
        final = model().fit(sub[features(x)], sub["r"])
        check = hashlib.sha256(np.round(final.predict(sub[features(x)]), 10).tobytes()).hexdigest()[:16]
        frozen[side] = {"x": x, "n": n, "a": a, "tau": tau, "fit_samples": len(sub), "prediction_hash": check, "fit": s}
    json.dump({"protocol": "README, Does the shape of the last candles predict direction?", "params": PARAMS,
               "frozen": frozen, "fit": report}, open(os.path.join(HERE, "FROZEN.json"), "w"), indent=1, default=float)
    print("frozen:", {k: (v["x"], v["n"], v["a"], v["tau"]) for k, v in frozen.items()} or "none (universe v2 stays unread)")


def judge(cache_dir):
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))["frozen"]
    if not frozen:
        sys.exit("nothing was frozen on the fit part")
    df_fit, df_v2 = table("fit", cache_dir), table("v2", cache_dir)
    out = {}
    for side, spec in frozen.items():
        x, n, a, tau = spec["x"], spec["n"], spec["a"], spec["tau"]
        sub = config_rows(df_fit, x, n, a, side, "fit")
        final = model().fit(sub[features(x)], sub["r"])
        check = hashlib.sha256(np.round(final.predict(sub[features(x)]), 10).tobytes()).hexdigest()[:16]
        assert check == spec["prediction_hash"], "the refitted model differs from the frozen one"
        fresh = config_rows(df_v2, x, n, a, side, None)
        s = kept_stats(fresh, final.predict(fresh[features(x)]) > tau)
        s["adopted"] = bool(s.get("kept") and s["diff"] > 0 and s["t"] >= 2)
        print(f"{side}: " + show(f"X {x} N {n} {a} ATR tau {tau}", s) + (" -> PASSES" if s["adopted"] else " -> does not pass"))
        out[side] = s
    json.dump(out, open(os.path.join(HERE, "judge.json"), "w"), indent=1, default=float)


def selftest():
    o = np.array([10, 10, 10.5, 9.0, 10.0]); h = np.array([10, 10.2, 11.2, 9.5, 10.2])
    l = np.array([10, 9.8, 10.4, 8.8, 9.9]); c = np.array([10, 10.1, 11.0, 9.2, 10.0])
    assert outcome(o, h, l, c, 1, 3, 1, "long", 1.0) == 1.0          # day 2 reaches 11.1? high 11.2 >= 11.1: target
    assert outcome(o, h, l, c, 2, 2, 1, "long", 1.0) == (9.0 - 11.0) / 1.0   # day 3 opens at 9.0, past the stop 10.0
    assert outcome(o, h, l, c, 1, 1, 3, "short", 1.0) == -1.0        # day 2: high 11.2 >= stop 11.1
    assert outcome(o, h, l, c, 3, 1, 3, "long", 1.0) == (10.0 - 9.2) / 1.0   # time limit: last close
    assert outcome(o, h, l, c, 3, 5, 1, "long", 1.0) is None
    print("selftest: outcomes ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("selftest", "fit", "judge")); p.add_argument("cache", nargs="?")
    p.add_argument("--jobs", type=int, default=14)
    a = p.parse_args()
    t0 = time.time()
    {"selftest": selftest, "fit": lambda: fit(a.cache, a.jobs), "judge": lambda: judge(a.cache)}[a.cmd]()
    print(f"{time.time() - t0:.0f}s")
