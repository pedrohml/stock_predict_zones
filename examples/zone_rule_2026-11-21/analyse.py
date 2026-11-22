"""Zone-rule study: correction fits, picks and the criterion's statistics from harness.py's cases.

    uv run python examples/zone_rule_2026-11-21/analyse.py screen DATA        # fit part: every rule, refit correction
    uv run python examples/zone_rule_2026-11-21/analyse.py judge DATA         # held-out part: base (rules 5) vs FROZEN.json
"""
import json, math, os, pickle, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import trade_entries  # noqa: E402

COST = 0.002
HORIZONS = ("1-2w", "1-3m", "6-12m")
CORRECTED = ("1-2w", "1-3m")
SHRINK = 100
half_up = trade_entries.half_up


def load(path):
    with open(path, "rb") as handle:
        blob = pickle.load(handle)
    return blob


def rows(blob, name):
    """Every case's valid setups under rule `name`: [(case, setup dict)] with the case's
    ticker, as-of date and price."""
    F = blob["fields"]
    for r in blob["records"]:
        if "skipped" in r:
            continue
        mine = r["rules"].get(name)
        if not isinstance(mine, list) or not mine:
            continue
        case = (r["ticker"], r["as_of"])
        yield case, r, [dict(zip(F, r["setups"][i])) for i in mine]


# ---- the correction: trade_entries.corrected() with a given table ---------------------------

def logit(p):
    return trade_entries.logit(p)


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def corrected(s, shift):
    """The stated numbers of raw setup `s` under `shift` ({"fill", "win", "edge"} or None), rounded
    as setups() rounds them."""
    fill, win, if_filled = s["fill"], s["win"], s["if_filled"]
    if shift and s["order"] != "market":
        fill = sigmoid(logit(fill) + shift["fill"])
        win = sigmoid(logit(win) + shift["win"])
        if_filled = if_filled + shift["edge"]
    f, w, e = half_up(fill), half_up(win), half_up(if_filled)
    return {"fill_st": f, "win_st": w, "edge_st": e, "per_order_st": half_up(f * e)}


def picks(setups, horizon, table):
    """trade_entries.setups()' ranking and pick() on the corrected numbers."""
    out = []
    for s in setups:
        out.append({**s, **corrected(s, table.get((horizon, s["side"], s["setup"])))})
    out.sort(key=lambda s: (-s["per_order_st"], -s["fill_st"], s["id"]))
    chosen = []
    for s in out:
        if len(chosen) == 3:
            break
        if all(s["entry"] != k["entry"] for k in chosen):
            chosen.append(s)
    return chosen


def solve_shift(probs, target):
    """x such that mean(sigmoid(logit(p) + x)) == target (bisection)."""
    if not probs:
        return 0.0
    base = [logit(p) for p in probs]
    lo, hi = -5.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        m = sum(sigmoid(b + mid) for b in base) / len(base)
        lo, hi = (mid, hi) if m < target else (lo, mid)
    return (lo + hi) / 2


def fit_correction(blob, name, horizon, include_market=True):
    """README "An empirical correction of the odds": per side and kind, over every valid setup of
    the fit cases, pessimistic ordering: fill and win log-odds shifts to the observed rates, edge
    shift = observed - stated mean R per fill, each shrunk by n / (n + 100) (n cases), rounded to 0.01."""
    groups = defaultdict(lambda: {"fills": [], "filled": [], "wins": [], "won": [], "edge": [], "r": [], "cases": set()})
    for case, _, setups in rows(blob, name):
        for s in setups:
            g = groups[(s["side"], s["setup"])]
            if s["order"] == "market" and not include_market:
                continue
            g["cases"].add(case)
            if s["order"] != "market":
                g["fills"].append(s["fill"]); g["filled"].append(bool(s["filled"]))
            if s["filled"]:
                g["wins"].append(s["win"]); g["won"].append(bool(s["won"]))
                g["edge"].append(s["if_filled"]); g["r"].append(s["r"])
    table = {}
    for (side, kind), g in sorted(groups.items()):
        n = len(g["cases"])
        k = n / (n + SHRINK)
        fill = solve_shift(g["fills"], sum(g["filled"]) / len(g["filled"])) if g["fills"] else 0.0
        win = solve_shift(g["wins"], sum(g["won"]) / len(g["won"])) if g["wins"] else 0.0
        edge = (sum(g["r"]) - sum(g["edge"])) / len(g["r"]) if g["r"] else 0.0
        table[(horizon, side, kind)] = {"fill": round(k * fill, 2), "win": round(k * win, 2), "edge": round(k * edge, 2)}
    return table


# ---- statistics of the picks ---------------------------------------------------------------------

def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def summary(picked):
    """picked: [(case, as_of month, setup)] -> the criterion's numbers."""
    n = len(picked)
    S = [s for _, _, s in picked]
    filled = [s for s in S if s["filled"]]
    net = [s["r"] - (COST * s["entry"] / s["risk"] if s["filled"] else 0.0) for s in S]
    fo = sum(bool(s["filled"]) for s in S) / n
    wo = sum(bool(s["won"]) for s in filled) / len(filled) if filled else float("nan")
    out = {
        "setups": n, "cases": len({c for c, _, _ in picked}),
        "fill_st": sum(s["fill_st"] for s in S) / n, "fill_obs": fo,
        "win_st": sum(s["win_st"] for s in filled) / len(filled), "win_obs": wo,
        "edge_fill_st": sum(s["edge_st"] for s in filled) / len(filled),
        "r_fill": sum(s["r"] for s in filled) / len(filled),
        "r_fill_other": sum(s["r_other"] for s in S if s["filled_other"]) / max(1, sum(bool(s["filled_other"]) for s in S)),
        "per_order_st": sum(s["per_order_st"] for s in S) / n,
        "r_order": sum(s["r"] for s in S) / n, "r_order_other": sum(s["r_other"] for s in S) / n,
        "net_order": sum(net) / n,
        "brier_fill": brier([(s["fill_st"], bool(s["filled"])) for s in S]),
        "brier_fill_const": brier([(fo, bool(s["filled"])) for s in S]),
        "brier_win": brier([(s["win_st"], bool(s["won"])) for s in filled]),
        "brier_win_const": brier([(wo, bool(s["won"])) for s in filled]),
    }
    out["gap"] = abs(out["per_order_st"] - out["r_order"])
    return out


def diff_t(a, b):
    """Mean net R per order of picks `a` minus that of `b`, and its standard error clustered by
    as-of month: per month, the sum of each setup's deviation from its rule's mean over n."""
    def parts(picked):
        net = [(m, s["r"] - (COST * s["entry"] / s["risk"] if s["filled"] else 0.0)) for _, m, s in picked]
        mean = sum(x for _, x in net) / len(net)
        g = defaultdict(float)
        for m, x in net:
            g[m] += (x - mean) / len(net)
        return mean, g
    ma, ga = parts(a)
    mb, gb = parts(b)
    se = math.sqrt(sum((ga.get(m, 0.0) - gb.get(m, 0.0)) ** 2 for m in set(ga) | set(gb)))
    return ma - mb, se, (ma - mb) / se if se else float("nan")


def picked_of(blob, name, horizon, table):
    out, zones = [], []
    for case, r, setups in rows(blob, name):
        for s in picks(setups, horizon, table):
            out.append((case, r["as_of"][:7], s))
        if name in r["zones"]:
            zones.append(r["zones"][name])
    return out, zones


def band(picked):
    """EDGE_BAND's method: the larger of |mean stated edge.if_filled - mean observed R per fill| of
    the picks under the two orderings, rounded up to 0.01."""
    f = [s for _, _, s in picked if s["filled"]]
    fo = [s for _, _, s in picked if s["filled_other"]]
    st = sum(s["edge_st"] for s in f) / len(f)
    st_o = sum(s["edge_st"] for s in fo) / len(fo)
    pess = abs(st - sum(s["r"] for s in f) / len(f))
    opt = abs(st_o - sum(s["r_other"] for s in fo) / len(fo))
    return math.ceil(max(pess, opt) * 100 - 1e-9) / 100, pess, opt


def current_table():
    return dict(trade_entries.CORRECTION)


def line(name, k, z=None):
    zs = ""
    if z:
        zs = f" zones {sum(a for a, _ in z) / len(z):.2f} width {100 * sum(b for _, b in z) / len(z):.2f}%"
    return (f"  {name:<12} n {k['setups']:>6}  fill {k['fill_st']:.3f}/{k['fill_obs']:.3f}  win {k['win_st']:.3f}/{k['win_obs']:.3f}"
            f"  R/ord st {k['per_order_st']:+.3f} obs {k['r_order']:+.3f} opt {k['r_order_other']:+.3f} net {k['net_order']:+.3f}"
            f"  gap {k['gap']:.3f}  Bf {k['brier_fill']:.4f}({k['brier_fill_const']:.4f}) Bw {k['brier_win']:.4f}({k['brier_win_const']:.4f}){zs}")


def screen(data, names=None):
    report = {}
    for horizon in HORIZONS:
        path = os.path.join(data, f"fit_{horizon}.pkl")
        if not os.path.exists(path):
            continue
        blob = load(path)
        base_table = current_table()
        base, bz = picked_of(blob, "base", horizon, base_table)
        kb = summary(base)
        print(f"{horizon}: fit part")
        print(line("rules5", kb, bz))
        for name in names or blob["names"]:
            table = {}
            if horizon in CORRECTED:
                table = fit_correction(blob, name, horizon)
            mine, z = picked_of(blob, name, horizon, table)
            if not mine:
                continue
            k = summary(mine)
            d, se, t = diff_t(mine, base)
            b = band(mine)
            print(line(name, k, z) + f"  dNet {d:+.3f} t {t:+.1f}  band {b[0]:.2f}")
            report[(horizon, name)] = {"summary": k, "diff": [d, se, t], "band": b, "table": {"|".join(key): v for key, v in table.items()}}
    return report


def judge(data):
    """The criterion's tests 1-3 on the held-out part: the current rule (rules 5 as shipped) against
    FROZEN.json, on the cases for which both make setups."""
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))
    name = frozen["rule_name"]
    table = {tuple(k.split("|")): v for k, v in frozen["correction"].items()}
    out = {}
    for horizon in HORIZONS:
        blob = load(os.path.join(data, f"held-out_{horizon}.pkl"))
        base, bz = picked_of(blob, "base", horizon, current_table())
        mine, z = picked_of(blob, name, horizon, table)
        both = {c for c, _, _ in base} & {c for c, _, _ in mine}
        base = [p for p in base if p[0] in both]
        mine = [p for p in mine if p[0] in both]
        kb, kc = summary(base), summary(mine)
        d, se, t = diff_t(mine, base)
        tests = {"gap": kc["gap"] - kb["gap"] <= 0.01,
                 "brier_fill": (kc["brier_fill"] - kc["brier_fill_const"]) - (kb["brier_fill"] - kb["brier_fill_const"]) <= 0.002,
                 "brier_win": (kc["brier_win"] - kc["brier_win_const"]) - (kb["brier_win"] - kb["brier_win_const"]) <= 0.002}
        if horizon in CORRECTED:
            tests["net"] = d >= 0.02 and t >= 2
        print(f"{horizon}: held-out part, {len(both)} cases with setups under both rules")
        print(line("current", kb, bz))
        print(line(name, kc, z))
        print(f"  net difference {d:+.4f} (se {se:.4f}, t {t:+.1f}); gap {kb['gap']:.4f} -> {kc['gap']:.4f}; "
              + ", ".join(f"{k} {'pass' if v else 'FAIL'}" for k, v in tests.items()))
        for side in ("long", "short"):
            for kind in ("reversal", "continuation"):
                pick_b = [p for p in base if p[2]["side"] == side and p[2]["setup"] == kind]
                pick_c = [p for p in mine if p[2]["side"] == side and p[2]["setup"] == kind]
                if pick_b or pick_c:
                    fmt = lambda P: (f"{len(P)} st {sum(s['per_order_st'] for _, _, s in P) / len(P):+.3f} obs "
                                     f"{sum(s['r'] for _, _, s in P) / len(P):+.3f}" if P else "none")
                    print(f"    {side} {kind}: current {fmt(pick_b)} | candidate {fmt(pick_c)}")
        out[horizon] = {"cases": len(both), "current": kb, "candidate": kc, "diff": [d, se, t], "tests": tests,
                        "zones_current": [sum(a for a, _ in bz) / len(bz), sum(b for _, b in bz) / len(bz)],
                        "zones_candidate": [sum(a for a, _ in z) / len(z), sum(b for _, b in z) / len(z)]}
    return out


if __name__ == "__main__":
    what, data = sys.argv[1], sys.argv[2]
    if what == "judge":
        rep = judge(data)
        with open(os.path.join(data, "judge.json"), "w") as handle:
            json.dump(rep, handle, indent=1)
    if what == "screen":
        rep = screen(data, sys.argv[3:] or None)
        with open(os.path.join(data, "screen.json"), "w") as handle:
            json.dump({"|".join(k): v for k, v in rep.items()}, handle, indent=1)
