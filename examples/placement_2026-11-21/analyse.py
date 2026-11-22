"""Correction refit, ranking, pick and scores of the placement variants (README, "Stop and target
placement"), from harness.py's files.

    uv run python examples/placement_2026-11-21/analyse.py fit OUT            # every variant, fit part: the choice
    uv run python examples/placement_2026-11-21/analyse.py check OUT          # rules 5's CORRECTION and bands refitted

The correction, ranking and pick follow trade_entries.corrected(), setups() and pick() exactly
(rounding included); the fit of the correction follows README "An empirical correction of the
odds, per horizon, side and kind".
"""
import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import harness as H  # noqa: E402
import trade_entries as te  # noqa: E402

COST = 0.002
BLOCKS = ((2005, 2012), (2013, 2019), (2020, 2026))
CORRECTED = ("1-2w", "1-3m")
HORIZONS = ("1-2w", "1-3m", "6-12m")
RULES5_CORRECTION = {   # trade_entries.CORRECTION of rules 5, kept here for the comparison
    ("1-2w", "long", "reversal"): {"fill": 0.16, "win": 0.26, "edge": 0.18},
    ("1-2w", "long", "continuation"): {"fill": 0.23, "win": -0.06, "edge": -0.09},
    ("1-2w", "short", "reversal"): {"fill": 0.22, "win": 0.22, "edge": 0.17},
    ("1-2w", "short", "continuation"): {"fill": 0.15, "win": -0.15, "edge": -0.15},
    ("1-3m", "long", "reversal"): {"fill": 0.01, "win": 0.11, "edge": 0.07},
    ("1-3m", "long", "continuation"): {"fill": 0.24, "win": -0.13, "edge": -0.11},
    ("1-3m", "short", "reversal"): {"fill": 0.23, "win": -0.01, "edge": 0.02},
    ("1-3m", "short", "continuation"): {"fill": -0.01, "win": -0.24, "edge": -0.19},
}


def load(out, horizon, chosen):
    """(cases, {variant: rows}) with one case numbering over all files."""
    cases, rows, offset = [], {v: [] for v in chosen}, 0
    for path in sorted(glob.glob(os.path.join(out, horizon, "*.npz"))):
        with np.load(path) as f:
            c = f["cases"]
            if not len(c):
                continue
            c = c.copy()
            c["case"] += offset
            cases.append(c)
            for v in chosen:
                r = f[f"v{v}"].copy()
                r["case"] += offset
                rows[v].append(r)
            offset += len(c)
    return np.concatenate(cases), {v: np.concatenate(r) for v, r in rows.items()}


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def logits(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def shift_to(p, target):
    """x such that the mean of sigmoid(logit(p) + x) equals target (bisection)."""
    z = logits(p)
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if sigmoid(z + mid).mean() < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def fit_correction(rows, horizon):
    """{(horizon, side, kind): {"fill", "win", "edge"}} on every valid setup of `rows` (fit cases),
    pessimistic ordering, shrunk by n / (n + 100) (n: cases), rounded to 0.01."""
    out = {}
    for side in ("long", "short"):
        for kind in ("reversal", "continuation"):
            g = rows[(rows["side"] == side) & (rows["kind"] == kind)]
            if not len(g):
                continue
            n = len(np.unique(g["case"]))
            shrink = n / (n + 100)
            ordered = g[g["order"] != "market"]
            fill = shift_to(ordered["fill"], ordered["filled"].mean()) if len(ordered) else 0.0
            filled = g[g["filled"]]
            win = shift_to(filled["win"], filled["won"].mean()) if len(filled) else 0.0
            edge = float(filled["r"].mean() - filled["if_filled"].mean()) if len(filled) else 0.0
            out[(horizon, side, kind)] = {key: te.half_up(value * shrink) if value >= 0 else -te.half_up(-value * shrink)
                                          for key, value in (("fill", fill), ("win", win), ("edge", edge))}
    return out


def stated(rows, correction, horizon):
    """Stated fill, win, if_filled and per_order of every row, as trade_entries.setups() states
    them under `correction` (None: none at this horizon)."""
    n = len(rows)
    fill, win, if_f, per = np.empty(n), np.empty(n), np.empty(n), np.empty(n)
    h = te.half_up
    for i, r in enumerate(rows):
        f, w, e = float(r["fill"]), float(r["win"]), float(r["if_filled"])
        shift = (correction or {}).get((horizon, str(r["side"]), str(r["kind"])))
        if shift and r["order"] != "market":
            f = 1 / (1 + math.exp(-(te.logit(f) + shift["fill"])))
            w = 1 / (1 + math.exp(-(te.logit(w) + shift["win"])))
            e = e + shift["edge"]
        fill[i], win[i], if_f[i] = h(f), h(w), h(e)
        per[i] = h(fill[i] * if_f[i])
    return fill, win, if_f, per


def picks(rows, fill, per):
    """Indices of the picked setups: per case, sorted by (-per_order, -fill, id), the first three
    with distinct entries (trade_entries.pick())."""
    by_case = defaultdict(list)
    for i, c in enumerate(rows["case"]):
        by_case[int(c)].append(i)
    chosen = []
    for case, idx in by_case.items():
        idx.sort(key=lambda i: (-per[i], -fill[i], str(rows["id"][i])))
        kept = []
        for i in idx:
            if len(kept) == 3:
                break
            if all(rows["entry"][i] != rows["entry"][j] for j in kept):
                kept.append(i)
        chosen += kept
    return np.array(sorted(chosen), dtype=int)


def ceil2(x):
    return math.ceil(round(x * 100, 6)) / 100


def summary(rows, cases, fill, win, if_f, per, idx):
    """Scores of the picked setups `idx`."""
    p = rows[idx]
    f_st, w_st, e_st, po = fill[idx], win[idx], if_f[idx], per[idx]
    filled, won = p["filled"], p["won"]
    cost = np.where(filled, COST * p["entry"] / p["risk"], 0.0)
    net = p["r"] - cost
    price = cases["price"][p["case"]]
    fo = filled.mean()
    wf = won[filled]
    out = {"setups": len(p), "cases": len(np.unique(p["case"])),
           "fill_st": f_st.mean(), "fill_obs": fo, "win_st": w_st[filled].mean(), "win_obs": wf.mean(),
           "per_order_st": po.mean(), "gross": p["r"].mean(), "net": net.mean(), "opt": p["r_other"].mean(),
           "gap": abs(po.mean() - p["r"].mean()),
           "brier_fill": ((f_st - filled) ** 2).mean(), "const_fill": fo * (1 - fo),
           "brier_win": ((w_st[filled] - wf) ** 2).mean(), "const_win": wf.mean() * (1 - wf.mean()),
           "risk_pct": float(np.median(p["risk"] / price)),
           "net_pct": float((p["r"] * p["risk"] / p["entry"] - np.where(filled, COST, 0.0)).mean()),
           "band_pess": abs(e_st[filled].mean() - p["r"][filled].mean()),
           "band_opt": abs(e_st[filled].mean() - p["r_other"][filled].mean()),
           "ratio": float(np.median(p["ratio"]))}
    out["band"] = ceil2(max(out["band_pess"], out["band_opt"]))
    out["bf_rel"] = out["brier_fill"] - out["const_fill"]
    out["bw_rel"] = out["brier_win"] - out["const_win"]
    return out, net, p["case"]


def compare(cases, a, b):
    """Difference of mean net R per order b - a over the cases both have, its standard error
    clustered by as-of month, and the difference in each block of years. a, b: (net, case)."""
    common = np.intersect1d(np.unique(a[1]), np.unique(b[1]))
    month = np.array([s[:7] for s in cases["as_of"]])
    year = np.array([int(s[:4]) for s in cases["as_of"]])
    parts = []
    for net, case in (a, b):
        keep = np.isin(case, common)
        parts.append((net[keep], case[keep]))
    (na, ca), (nb, cb) = parts
    ma, mb = na.mean(), nb.mean()
    g = defaultdict(float)
    for value, c in zip((na - ma) / len(na), ca):
        g[month[c]] -= value
    for value, c in zip((nb - mb) / len(nb), cb):
        g[month[c]] += value
    se = math.sqrt(sum(x * x for x in g.values()))
    blocks = []
    for lo, hi in BLOCKS:
        ka, kb = (year[ca] >= lo) & (year[ca] <= hi), (year[cb] >= lo) & (year[cb] <= hi)
        blocks.append(float(nb[kb].mean() - na[ka].mean()) if ka.any() and kb.any() else float("nan"))
    return {"common": len(common), "a": ma, "b": mb, "diff": mb - ma, "se": se, "t": (mb - ma) / se if se else 0.0,
            "blocks": blocks}


def evaluate(cases, rows, horizon, correction=None, fit_rows=None):
    """Refit (or use) the correction, then stated odds, picks and summary."""
    if horizon in CORRECTED and correction is None:
        correction = fit_correction(fit_rows if fit_rows is not None else rows, horizon)
    if horizon not in CORRECTED:
        correction = None
    fill, win, if_f, per = stated(rows, correction, horizon)
    idx = picks(rows, fill, per)
    s, net, case = summary(rows, cases, fill, win, if_f, per, idx)
    return s, (net, case), correction


def line(name, s, c=None):
    extra = ""
    if c:
        extra = (f" | vs r5 {c['diff']:+.3f} t {c['t']:+.1f} blocks "
                 + "/".join(f"{x:+.3f}" for x in c["blocks"]))
    return (f"{name:<22} n {s['setups']:>6} fill {s['fill_st']:.0%}/{s['fill_obs']:.0%} win {s['win_st']:.0%}/{s['win_obs']:.0%} "
            f"R/o {s['per_order_st']:+.3f}/{s['gross']:+.3f} (opt {s['opt']:+.3f}) net {s['net']:+.3f} gap {s['gap']:.3f} "
            f"Bf {s['brier_fill']:.3f}({s['const_fill']:.3f}) Bw {s['brier_win']:.3f}({s['const_win']:.3f}) "
            f"risk {s['risk_pct']:.2%} net% {s['net_pct']:+.3%} ratio {s['ratio']:.2f} band {s['band']:.2f}" + extra)


def fit(out, horizons=HORIZONS):
    result = {}
    for horizon in horizons:
        cases, rows = load(out, horizon, range(len(H.VARIANTS)))
        base, base_net, _ = evaluate(cases, rows[0], horizon)
        print(f"\n{horizon}: {len(cases)} fit cases")
        print(line("k0.25-zone (rules 5)", base))
        table = {}
        for v in range(len(H.VARIANTS)):
            s, net, corr = evaluate(cases, rows[v], horizon)
            c = compare(cases, base_net, net)
            ok = (v != 0 and c["diff"] >= 0.02 and c["t"] >= 2 and all(x > 0 for x in c["blocks"])
                  and s["gap"] <= base["gap"] + 0.01 and s["bf_rel"] <= base["bf_rel"] + 0.002
                  and s["bw_rel"] <= base["bw_rel"] + 0.002)
            table[v] = {"name": H.name(H.VARIANTS[v]), **s, **{f"cmp_{k}": x for k, x in c.items()}, "eligible": ok,
                        "correction": {f"{k[1]} {k[2]}": x for k, x in (corr or {}).items()}}
            print(("* " if ok else "  ") + line(H.name(H.VARIANTS[v]), s, c), flush=True)
        eligible = [v for v in table if table[v]["eligible"]]
        best = max(eligible, key=lambda v: table[v]["net"]) if eligible else None
        print(f"{horizon}: candidate {H.name(H.VARIANTS[best]) if best is not None else 'none'}")
        result[horizon] = {"candidate": best, "variants": table}
    with open(os.path.join(out, "fit_results.json"), "w") as handle:
        json.dump(result, handle, indent=1, default=float)


def check(out):
    """Rules 5 refitted: its CORRECTION and its bands should come back."""
    for horizon in HORIZONS:
        cases, rows = load(out, horizon, [0])
        s, _, corr = evaluate(cases, rows[0], horizon)
        print(horizon, line("rules 5 refit", s))
        for key, shift in (corr or {}).items():
            print("  ", key, shift, "shipped", RULES5_CORRECTION.get(key))
        if horizon in CORRECTED:   # with the shipped correction
            s, _, _ = evaluate(cases, rows[0], horizon, correction=RULES5_CORRECTION)
            print(horizon, line("rules 5 shipped", s))


if __name__ == "__main__":
    command, out = sys.argv[1], sys.argv[2]
    if command == "fit":
        fit(out, sys.argv[3:] or HORIZONS)
    else:
        check(out)
