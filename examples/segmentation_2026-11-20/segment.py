"""Segmentation of the setup backtest, per PROTOCOL.md (README, "Segmenting the setups: no situation held").

The CSVs are not committed (88 MB). Make them first, one per horizon, into DATA (default: this folder):
  uv run python backtest_entries.py --universe backtest_universe.txt --horizon 1-2w --start 2005-01-03 \
      --end 2026-10-30 --by-regime --by-part --jobs 14 --quiet --csv DATA/1-2w.csv
  (1-3m: --end 2026-08-14; 6-12m: --end 2025-11-13)
then:  uv run python examples/segmentation_2026-11-20/segment.py search | judge   [DATA]
"""
import bisect, os, itertools, json, math, sys
from datetime import date
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be  # noqa: E402

SEG = HERE                                          # FROZEN.json and the feature cache
DATA = sys.argv[2] if len(sys.argv) > 2 else HERE   # the backtest CSVs
COST = 0.002
BLOCKS = ((2005, 2012), (2013, 2019), (2020, 2026))
MIN_SETUPS, MIN_DATES, T_SEARCH, T_JUDGE = 1000, 150, 3.0, 2.0
NUMERIC = ["ratio", "fill", "win_if_filled", "if_filled", "risk_pct", "distance_pct", "mom3", "mom12", "vol3"]
CATEG = ["side", "setup", "order", "rank", "quality", "regime", "above200", "kind_side"]


def ticker_features():
    path = f"{DATA}/features.json"
    try:
        return json.load(open(path))
    except FileNotFoundError:
        pass
    uni = be.read_universe(f"{ROOT}/backtest_universe.txt")
    out = {}
    for t in uni["tickers"]:
        rows = backtest.download(t, date(1995, 1, 1))["rows"]
        d = [r[0][:10] for r in rows]
        c = np.array([r[4] for r in rows], float)
        out[t] = {"dates": d, "close": c.tolist()}
    groups = {}
    for name, g in uni["groups"].items():
        for t in g["tickers"]:
            groups.setdefault(t, name)
    json.dump({"series": out, "groups": groups}, open(path, "w"))
    return json.load(open(path))


def enrich(df, feats):
    series, groups = feats["series"], feats["groups"]
    cols = {"mom3": [], "mom12": [], "vol3": [], "above200": []}
    for t, a in zip(df["ticker"], df["as_of"]):
        s = series.get(t)
        i = bisect.bisect_right(s["dates"], a) - 1 if s else -1
        c = s["close"] if s else []
        def ret(n):
            return c[i] / c[i - n] - 1 if i - n >= 0 and c[i - n] > 0 else np.nan
        cols["mom3"].append(ret(63)); cols["mom12"].append(ret(252))
        if i >= 64:
            x = np.array(c[i - 63:i + 1]); x = x[x > 0]
            cols["vol3"].append(float(np.std(np.diff(np.log(x))) * math.sqrt(252)) if len(x) > 20 else np.nan)
        else:
            cols["vol3"].append(np.nan)
        cols["above200"].append(("above" if c[i] > np.mean(c[i - 199:i + 1]) else "below") if i >= 199 else "n/a")
    for k, v in cols.items():
        df[k] = v
    df["group"] = df["ticker"].map(groups)
    df["risk_pct"] = df["risk"] / df["price"]
    df["distance_pct"] = (df["entry"] - df["price"]).abs() / df["price"]
    df["kind_side"] = df["side"] + " " + df["setup"]
    filled = df["filled"].astype(str).str.lower().isin(["true", "1"])
    df["filled_b"] = filled
    df["net"] = df["r"] - np.where(filled, COST * df["entry"] / df["risk"], 0.0)
    df["year"] = df["as_of"].str[:4].astype(int)
    df["month"] = df["as_of"].str[:7]
    return df


def load(h, feats):
    df = pd.read_csv(f"{DATA}/{h}.csv")
    df = df[df["part"].isin(["fit", "held-out"])].copy()
    return enrich(df, feats)


def add_bins(df, cuts):
    for k in NUMERIC:
        lo, hi = cuts[k]
        df[k + "_t"] = np.where(df[k].isna(), "n/a", np.where(df[k] <= lo, "low", np.where(df[k] <= hi, "mid", "high")))
    return df


def stat(sub):
    """mean net R per order, clustered standard error by as-of month, t."""
    n = len(sub)
    if n == 0:
        return {"n": 0}
    m = sub["net"].mean()
    g = (sub["net"] - m).groupby(sub["month"]).sum()
    se = math.sqrt((g ** 2).sum()) / n if n else float("nan")
    return {"n": n, "dates": sub["as_of"].nunique(), "net": m, "gross": sub["r"].mean(),
            "opt": sub["r_other"].mean(), "fill": sub["filled_b"].mean(), "se": se, "t": m / se if se else 0}


def segments(df):
    feats = CATEG + [k + "_t" for k in NUMERIC]
    vals = {f: [v for v in df[f].dropna().unique() if v != "n/a"] for f in feats}
    for f in feats:
        for v in vals[f]:
            yield ((f, v),)
    for f1, f2 in itertools.combinations(feats, 2):
        for v1 in vals[f1]:
            for v2 in vals[f2]:
                yield ((f1, v1), (f2, v2))


def mask(df, seg):
    m = np.ones(len(df), bool)
    for f, v in seg:
        m &= (df[f] == v).to_numpy()
    return m


def search():
    feats = ticker_features()
    found, cuts_all = [], {}
    for h in ("1-2w", "1-3m", "6-12m"):
        df = load(h, feats)
        fit = df[df["part"] == "fit"].copy()
        cuts = {k: [float(fit[k].quantile(1 / 3)), float(fit[k].quantile(2 / 3))] for k in NUMERIC}
        cuts_all[h] = cuts
        fit = add_bins(fit, cuts)
        base = stat(fit)
        print(f"{h}: fit {base['n']} setups, net {base['net']:+.3f} (gross {base['gross']:+.3f}, opt {base['opt']:+.3f}) t {base['t']:.1f}")
        tried = passed = 0
        for seg in segments(fit):
            m = mask(fit, seg)
            if m.sum() < MIN_SETUPS:
                continue
            sub = fit[m]
            s = stat(sub)
            if s["dates"] < MIN_DATES:
                continue
            tried += 1
            blocks = [sub[(sub["year"] >= a) & (sub["year"] <= b)]["net"].mean() for a, b in BLOCKS]
            ok = s["t"] >= T_SEARCH and all(x > 0 for x in blocks if not math.isnan(x))
            if ok:
                passed += 1
                found.append({"horizon": h, "segment": [list(x) for x in seg], **{k: s[k] for k in ("n", "dates", "net", "gross", "opt", "fill", "t")},
                              "blocks": blocks})
        print(f"  segments tried {tried}, passing {passed}")
    found.sort(key=lambda x: -x["net"])
    frozen, per = [], {}
    for f in found:
        if per.get(f["horizon"], 0) < 2 and len(frozen) < 5:
            frozen.append(f); per[f["horizon"]] = per.get(f["horizon"], 0) + 1
    json.dump({"protocol": "PROTOCOL.md", "cuts": cuts_all, "frozen": frozen, "passing": found},
              open(f"{SEG}/FROZEN.json", "w"), indent=1, default=float)
    print("top passing (fit):")
    for f in found[:15]:
        print(f"  {f['horizon']:6} {f['segment']}  n {f['n']}  net {f['net']:+.3f} gross {f['gross']:+.3f} t {f['t']:.1f} blocks {[round(b, 3) for b in f['blocks']]}")
    print("FROZEN:", [(f["horizon"], f["segment"]) for f in frozen])


def judge():
    feats = ticker_features()
    fz = json.load(open(f"{SEG}/FROZEN.json"))
    for h in ("1-2w", "1-3m", "6-12m"):
        df = add_bins(load(h, feats), fz["cuts"][h])
        held = df[df["part"] == "held-out"]
        b = stat(held)
        print(f"{h}: held-out all {b['n']} net {b['net']:+.3f} gross {b['gross']:+.3f} t {b['t']:.1f}")
        for f in fz["frozen"]:
            if f["horizon"] != h:
                continue
            seg = [tuple(x) for x in f["segment"]]
            m = mask(held, seg)
            s, c = stat(held[m]), stat(held[~m])
            hold = s["n"] and s["net"] > 0 and s["t"] >= T_JUDGE
            print(f"  {seg}: fit net {f['net']:+.3f} | held-out n {s['n']} net {s.get('net', float('nan')):+.3f} gross {s.get('gross', float('nan')):+.3f} "
                  f"t {s.get('t', 0):.1f} fill {s.get('fill', 0):.0%} | complement net {c['net']:+.3f} -> {'HOLDS' if hold else 'does not hold'}")


if __name__ == "__main__":
    {"search": search, "judge": judge}[sys.argv[1]]()
