"""Meta-labelling the setups (README, "Meta-labelling the setups, with volatility-expansion and
crash-risk features: protocol"). Needs scikit-learn, which is not a project dependency:

  uv run --with scikit-learn==1.9.1 python examples/meta_2026-11-22/meta.py fit   FIT_DIR
  uv run --with scikit-learn==1.9.1 python examples/meta_2026-11-22/meta.py judge FIT_DIR FRESH_DIR   # once

FIT_DIR and FRESH_DIR hold the setup CSVs 1-2w.csv and 1-3m.csv of backtest_entries.py (rules 6):
  uv run python backtest_entries.py --universe UNIVERSE --horizon 1-2w --start 2005-01-03 --end 2026-10-30 \
      --step 10 --by-part --jobs 14 --quiet --csv DIR/1-2w.csv
  (1-3m: --end 2026-08-14 --step 20), UNIVERSE backtest_universe.txt for FIT_DIR and
  examples/trend_2026-11-22/fresh_universe.txt for FRESH_DIR.
"""
import hashlib, json, logging, math, os, sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest_entries as be  # noqa: E402
import data_source  # noqa: E402

COST = 0.002
UNIVERSES = {"fit": os.path.join(ROOT, "backtest_universe.txt"),
             "fresh": os.path.join(ROOT, "examples", "trend_2026-11-22", "fresh_universe.txt")}
EMBARGO = {"1-2w": 30, "1-3m": 120}
FORWARD = {"1-2w": 10, "1-3m": 63}
FIRST_TEST_YEAR = 2010
TAUS = (0.0, 0.02, 0.05, 0.10)
MIN_KEPT = 0.20
BLOCKS = ((2010, 2012), (2013, 2019), (2020, 2026))
PARAMS = dict(max_depth=3, learning_rate=0.05, max_iter=300, min_samples_leaf=200, l2_regularization=1.0,
              random_state=0)
CATEGORICAL = ["side", "setup", "order"]
SETUP_NUM = ["rank", "ratio", "fill", "win_if_filled", "if_filled", "per_order", "risk_pct", "distance_pct"]
VOL = ["rv5", "rv21", "rv63", "rv5_63", "rv21_63", "bb_pct", "range10_63"]
CRASH = ["turnover", "ret21", "ret126", "ret252", "skew126", "dd63", "to_high252", "to_low252"]
TREND = ["sma200", "mom12"]
FEATURES = CATEGORICAL + SETUP_NUM + VOL + CRASH + TREND


# ---- candles and features ------------------------------------------------------------------

def candles(which, cache_dir):
    """{ticker: DataFrame(Open, High, Low, Close, Volume)} for a universe, its @ranges applied."""
    path = os.path.join(cache_dir, f"candles_{which}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    uni = be.read_universe(UNIVERSES[which])
    out = {}
    for t in uni["tickers"]:
        try:
            frame, _ = data_source.candles(yf.Ticker(t), t, "1d", start="2003-01-01", what=f"{t} 1d")
        except data_source.DataError as error:
            print(f"{t}: {error}"[:120]); continue
        if frame is None or frame.empty:
            continue
        f = frame[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"]).copy()
        f.index = [d.strftime("%Y-%m-%d") for d in f.index]
        first, last = uni["spans"].get(t, (None, None))
        f = f[(f.index >= (first.isoformat() if first else "")) & (f.index <= (last.isoformat() if last else "9999"))]
        out[t] = f
    pd.to_pickle(out, path)
    return out


def features(f, horizon):
    """Per day: the features of the protocol from the candles up to that day, and (diagnostics
    only) the realised volatility and skewness of the days after it."""
    c, h, l, v = f["Close"], f["High"], f["Low"], f["Volume"].fillna(0)
    r = np.log(c / c.shift(1))
    out = pd.DataFrame(index=f.index)
    for n in (5, 21, 63):
        out[f"rv{n}"] = r.rolling(n).std() * math.sqrt(252)
    out["rv5_63"] = out["rv5"] / out["rv63"]
    out["rv21_63"] = out["rv21"] / out["rv63"]
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    width = 4 * sd / mid
    out["bb_pct"] = width.rolling(252).rank(pct=True)
    rng = (h - l) / c
    out["range10_63"] = rng.rolling(10).mean() / rng.rolling(63).mean()
    lv = np.log1p(v)
    out["turnover"] = lv.rolling(21).mean() - lv.rolling(252).mean()
    for n in (21, 126, 252):
        out[f"ret{n}"] = c / c.shift(n) - 1
    out["skew126"] = r.rolling(126).skew()
    out["dd63"] = c / c.rolling(63).max() - 1
    out["to_high252"] = c / c.rolling(252).max() - 1
    out["to_low252"] = c / c.rolling(252).min() - 1
    out["sma200"] = c / c.rolling(200).mean() - 1
    out["mom12"] = c.shift(21) / c.shift(252) - 1
    n = FORWARD[horizon]
    fwd = r.shift(-1)
    out["fwd_rv"] = fwd[::-1].rolling(n).std()[::-1] * math.sqrt(252)
    out["fwd_skew"] = r.shift(-1)[::-1].rolling(63).skew()[::-1]
    return out.replace([np.inf, -np.inf], np.nan)


def table(csv_dir, which, horizon, cache_dir, parts):
    df = pd.read_csv(os.path.join(csv_dir, f"{horizon}.csv"))
    df = df[df["part"].isin(parts)].copy()
    filled = df["filled"].astype(str).str.lower().isin(["true", "1"])
    df["net"] = df["r"] - np.where(filled, COST * df["entry"] / df["risk"], 0.0)
    df["risk_pct"] = df["risk"] / df["price"]
    df["distance_pct"] = (df["entry"] - df["price"]).abs() / df["price"]
    df["month"] = df["as_of"].str[:7]
    df["year"] = df["as_of"].str[:4].astype(int)
    frames = []
    data = candles(which, cache_dir)
    for t, g in df.groupby("ticker", sort=False):
        if t not in data:
            continue
        feat = features(data[t], horizon)
        frames.append(g.join(feat, on="as_of"))
    out = pd.concat(frames).sort_values(["as_of", "ticker", "rank"]).reset_index(drop=True)
    for k in CATEGORICAL:
        out[k] = out[k].astype("category")
    return out


# ---- model and statistics ------------------------------------------------------------------

def model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(categorical_features="from_dtype", **PARAMS)


def walk_forward(df, horizon):
    """Out-of-sample predictions for the fit cases of FIRST_TEST_YEAR and later."""
    pred = pd.Series(np.nan, index=df.index)
    for year in range(FIRST_TEST_YEAR, df["year"].max() + 1):
        test = df["year"] == year
        if not test.any():
            continue
        cut = (date(year, 1, 1) - timedelta(days=EMBARGO[horizon])).isoformat()
        train = df["valid_until"] < cut
        m = model().fit(df.loc[train, FEATURES], df.loc[train, "net"])
        pred[test] = m.predict(df.loc[test, FEATURES])
    return pred


def clustered(values, months):
    s = pd.Series(values).groupby(np.asarray(months)).sum()
    return math.sqrt(float((s ** 2).sum()))


def kept_stats(df, keep):
    """Net R per order of the kept setups and of all, the difference, clustered t's, blocks."""
    keep = np.asarray(keep, bool)
    net, months = df["net"].to_numpy(), df["month"].to_numpy()
    n, nk = len(net), keep.sum()
    if nk == 0:
        return {"kept": 0}
    mk, m = net[keep].mean(), net.mean()
    se_k = clustered((net[keep] - mk) / nk, months[keep])
    infl = np.where(keep, (net - mk) / nk, 0.0) - (net - m) / n
    se_d = clustered(infl, months)
    blocks = []
    for a, b in BLOCKS:
        y = df["year"].between(a, b).to_numpy()
        blocks.append(float(net[y & keep].mean() - net[y].mean()) if (y & keep).any() else float("nan"))
    side = df["side"].astype(str).to_numpy()
    return {"setups": n, "kept": int(nk), "share": nk / n, "net_kept": mk, "t_kept": mk / se_k if se_k else 0.0,
            "net_all": m, "diff": mk - m, "t_diff": (mk - m) / se_d if se_d else 0.0, "blocks": blocks,
            "long_kept": float((side[keep] == "long").mean()), "gross_kept": float(df["r"].to_numpy()[keep].mean()),
            "opt_kept": float(df["r_other"].to_numpy()[keep].mean())}


def show(s):
    if not s.get("kept"):
        return "nothing kept"
    blk = "/".join(f"{x:+.3f}" if x == x else "nan" for x in s["blocks"])
    return (f"kept {s['kept']:6}/{s['setups']:6} ({s['share']:.0%}, long {s['long_kept']:.0%}) net {s['net_kept']:+.3f} "
            f"(t {s['t_kept']:+.1f}; gross {s['gross_kept']:+.3f}, opt {s['opt_kept']:+.3f}) vs all {s['net_all']:+.3f} "
            f"diff {s['diff']:+.3f} (t {s['t_diff']:+.1f}) [{blk}]")


def diagnostics(df):
    """Items 2 and 3 on their own: Spearman of each feature with the next days' volatility / skewness."""
    one = df.drop_duplicates(["ticker", "as_of"])
    out = {}
    for k in VOL:
        out[f"{k} ~ next vol"] = float(one[k].corr(one["fwd_rv"], method="spearman"))
    for k in CRASH:
        out[f"{k} ~ next skew"] = float(one[k].corr(one["fwd_skew"], method="spearman"))
    return out


def fit(fit_dir):
    frozen, report = {}, {}
    for horizon in ("1-2w", "1-3m"):
        df = table(fit_dir, "fit", horizon, fit_dir, ["fit"])
        print(f"== {horizon}: {len(df)} fit setups, {df['as_of'].nunique()} dates")
        diag = diagnostics(df)
        print("  items 2 and 3 on their own (Spearman, every case):")
        for k, v in diag.items():
            print(f"    {k:22} {v:+.3f}")
        pred = walk_forward(df, horizon)
        oos = df[pred.notna()].copy()
        p = pred[pred.notna()]
        rows = {}
        for tau in TAUS:
            s = kept_stats(oos, (p > tau).to_numpy())
            rows[tau] = s
            print(f"  tau {tau:.2f}: " + show(s))
        eligible = [t for t in TAUS if rows[t].get("kept") and rows[t]["share"] >= MIN_KEPT]
        tau = max(eligible, key=lambda t: rows[t]["net_kept"]) if eligible else None
        ok = False
        if tau is not None:
            s = rows[tau]
            ok = (s["net_kept"] > 0 and s["t_kept"] >= 3 and s["diff"] >= 0.02 and s["t_diff"] >= 3
                  and all(x > 0 for x in s["blocks"]))
        print(f"  chosen tau {tau} -> {'PASSES' if ok else 'does not pass'}")
        if ok:
            final = model().fit(df[FEATURES], df["net"])
            check = hashlib.sha256(np.round(final.predict(df[FEATURES]), 10).tobytes()).hexdigest()[:16]
            frozen[horizon] = {"tau": tau, "fit_setups": len(df), "prediction_hash": check}
        report[horizon] = {"diagnostics": diag, "taus": {str(k): v for k, v in rows.items()}, "tau": tau, "passes": ok}
    json.dump({"protocol": "README, Meta-labelling the setups", "features": FEATURES, "params": PARAMS,
               "frozen": frozen, "fit": report}, open(os.path.join(HERE, "FROZEN.json"), "w"), indent=1, default=float)
    print("frozen:", frozen or "none (the fresh universe stays unread)")


def judge(fit_dir, fresh_dir):
    fz = json.load(open(os.path.join(HERE, "FROZEN.json")))["frozen"]
    if not fz:
        sys.exit("nothing was frozen on the fit part")
    out = {}
    for horizon, spec in fz.items():
        train = table(fit_dir, "fit", horizon, fit_dir, ["fit"])
        final = model().fit(train[FEATURES], train["net"])
        check = hashlib.sha256(np.round(final.predict(train[FEATURES]), 10).tobytes()).hexdigest()[:16]
        assert check == spec["prediction_hash"], "the refitted model differs from the frozen one"
        fresh = table(fresh_dir, "fresh", horizon, fresh_dir, ["fit", "held-out", "embargo"])
        p = final.predict(fresh[FEATURES])
        s = kept_stats(fresh, p > spec["tau"])
        s["adopted"] = bool(s.get("kept") and s["net_kept"] > 0 and s["diff"] > 0 and s["t_diff"] >= 2)
        print(f"{horizon} fresh universe: " + show(s) + (" -> ADOPTED" if s["adopted"] else " -> not adopted"))
        out[horizon] = s
    json.dump(out, open(os.path.join(HERE, "judge.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    {"fit": lambda: fit(sys.argv[2]), "judge": lambda: judge(sys.argv[2], sys.argv[3])}[sys.argv[1]]()
