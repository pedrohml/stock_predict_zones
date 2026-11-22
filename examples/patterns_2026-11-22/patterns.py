"""Candlestick and chart patterns vs the setups, per the README section "Candlestick and chart
patterns: protocol" (written before any evaluation).

  uv run python examples/patterns_2026-11-22/patterns.py selftest
  uv run python examples/patterns_2026-11-22/patterns.py fit      DATA   # fit part only, writes FROZEN.json
  uv run python examples/patterns_2026-11-22/patterns.py held-out DATA   # once, the frozen patterns only

DATA holds the setup backtest CSVs (rules 6), made with
  uv run python backtest_entries.py --universe backtest_universe.txt --horizon 1-2w --start 2005-01-03 \
      --end 2026-10-30 --step 10 --by-part --jobs 14 --quiet --csv DATA/1-2w.csv
  uv run python backtest_entries.py --universe backtest_universe.txt --horizon 1-3m --start 2005-01-03 \
      --end 2026-08-14 --step 20 --by-part --jobs 14 --quiet --csv DATA/1-3m.csv
and a cache of the daily candles (candles.json) that the first run writes there.

Every detector at day t reads candles 0..t only (the self-test checks that cutting the candles after
t changes nothing).
"""
import bisect, json, math, os, sys
from datetime import date

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be  # noqa: E402

COST = 0.002
BLOCKS = ((2005, 2012), (2013, 2019), (2020, 2026))
START = "2005-01-03"
MIN_EVENTS, MIN_DATES, T_FIT, T_HELD = 300, 100, 3.0, 2.0
CANDLES = ("engulfing", "hammer", "star", "piercing", "soldiers")
CHARTS = ("head_shoulders", "double", "triangle")
FORWARD = {"candle": 10, "chart": 63}            # question A, trading days
SETUP_HORIZON = {"candle": "1-2w", "chart": "1-3m"}  # question B
LOOKBACK = 4                                     # B: the as-of day or the four days before it
WING, MEMORY, WINDOW = 5, 120, 20                # turning points: 11 closes; last 120 days; 20 days to signal


def family(name):
    return "candle" if name in CANDLES else "chart"


# ---- candlestick patterns ----------------------------------------------------------------

def candle_signals(o, h, l, c, t):
    """{name: +1 bullish / -1 bearish} of the candle patterns completed at day t."""
    out = {}
    if t < 6:
        return out
    body = lambda i: abs(c[i] - o[i])
    rng = lambda i: h[i] - l[i]
    white = lambda i: c[i] > o[i]
    black = lambda i: c[i] < o[i]
    down = lambda i: c[i - 5] > c[i - 1]          # the trend before candle i
    up = lambda i: c[i - 5] < c[i - 1]
    mid = lambda i: (o[i] + c[i]) / 2
    if down(t) and black(t - 1) and white(t) and o[t] <= c[t - 1] and c[t] >= o[t - 1] and body(t) > body(t - 1):
        out["engulfing"] = 1
    if up(t) and white(t - 1) and black(t) and o[t] >= c[t - 1] and c[t] <= o[t - 1] and body(t) > body(t - 1):
        out["engulfing"] = -1
    r = rng(t)
    if r > 0 and body(t) <= r / 3:
        lower, upper = min(o[t], c[t]) - l[t], h[t] - max(o[t], c[t])
        if down(t) and lower >= 2 * body(t) and upper <= 0.1 * r:
            out["hammer"] = 1
        elif up(t) and upper >= 2 * body(t) and lower <= 0.1 * r:
            out["hammer"] = -1
    long_ = lambda i: rng(i) > 0 and body(i) >= 0.6 * rng(i)
    if t >= 7 and long_(t - 2) and body(t - 1) <= 0.3 * body(t - 2):
        if down(t - 2) and black(t - 2) and white(t) and c[t] > mid(t - 2):
            out["star"] = 1
        elif up(t - 2) and white(t - 2) and black(t) and c[t] < mid(t - 2):
            out["star"] = -1
    if long_(t - 1):
        if down(t - 1) and black(t - 1) and white(t) and o[t] < l[t - 1] and mid(t - 1) < c[t] < o[t - 1]:
            out["piercing"] = 1
        elif up(t - 1) and white(t - 1) and black(t) and o[t] > h[t - 1] and o[t - 1] < c[t] < mid(t - 1):
            out["piercing"] = -1
    strong = lambda i: rng(i) > 0 and body(i) >= 0.5 * rng(i)
    three = range(t - 2, t + 1)
    if all(strong(i) for i in three):
        if all(white(i) for i in three) and c[t - 2] < c[t - 1] < c[t] \
                and o[t - 2] <= o[t - 1] <= c[t - 2] and o[t - 1] <= o[t] <= c[t - 1]:
            out["soldiers"] = 1
        elif all(black(i) for i in three) and c[t - 2] > c[t - 1] > c[t] \
                and c[t - 2] <= o[t - 1] <= o[t - 2] and c[t - 1] <= o[t] <= o[t - 1]:
            out["soldiers"] = -1
    return out


# ---- chart patterns ----------------------------------------------------------------------

def turning_points(c):
    """[(j, close, +1 peak / -1 trough)]: a close that is the highest (lowest) of the 11 closes
    centred on it, the first of equal ones. Point j is known on day j + WING; known_points() makes
    them alternate."""
    raw = []
    for j in range(WING, len(c) - WING):
        w = c[j - WING:j + WING + 1]
        if c[j] == max(w) and c[j] > max(c[j - WING:j], default=-math.inf):
            raw.append((j, c[j], 1))
        elif c[j] == min(w) and c[j] < min(c[j - WING:j], default=math.inf):
            raw.append((j, c[j], -1))
    return raw


def known_points(points, t):
    """The turning points known on day t and of the last MEMORY days, made alternating (of two
    neighbours of one kind the more extreme stays) from those points only, so a later point can
    never change an earlier day's answer."""
    js = [p[0] for p in points]
    pts = points[bisect.bisect_left(js, t - MEMORY):bisect.bisect_right(js, t - WING)]
    out = []
    for p in pts:
        if out and out[-1][2] == p[2]:
            if (p[1] - out[-1][1]) * p[2] > 0:
                out[-1] = p
        else:
            out.append(p)
    return out


def first_cross(c, t, since, past):
    """True when close t is past the line (past(i) for day i) and no close from `since` to t-1 was."""
    if since > t or not past(t):
        return False
    return not any(past(i) for i in range(max(since, 0), t))


def near(a, b, tol):
    return abs(a - b) <= tol * (a + b) / 2


def chart_signals(c, raw_points, t):
    """{name: +1 / -1} of the chart patterns whose signal close is day t."""
    out = {}
    pts = known_points(raw_points, t)
    if len(pts) < 3:
        return out
    since = pts[-1][0] + WING
    if t - since > WINDOW:
        return out
    if len(pts) >= 5:
        a, b, m, d, e = pts[-5:]
        if m[2] == 1 and m[1] > a[1] and m[1] > e[1] and near(a[1], e[1], .03) and near(b[1], d[1], .03):
            neck = min(b[1], d[1])
            if first_cross(c, t, since, lambda i: c[i] < neck):
                out["head_shoulders"] = -1
        if m[2] == -1 and m[1] < a[1] and m[1] < e[1] and near(a[1], e[1], .03) and near(b[1], d[1], .03):
            neck = max(b[1], d[1])
            if first_cross(c, t, since, lambda i: c[i] > neck):
                out["head_shoulders"] = 1
    a, m, b = pts[-3:]
    if near(a[1], b[1], .02) and b[0] - a[0] >= 15:
        level = (a[1] + b[1]) / 2
        if a[2] == 1 and m[1] <= level * 0.95 and first_cross(c, t, since, lambda i: c[i] < m[1]):
            out["double"] = -1
        if a[2] == -1 and m[1] >= level * 1.05 and first_cross(c, t, since, lambda i: c[i] > m[1]):
            out["double"] = 1
    if len(pts) >= 4:
        last4 = pts[-4:]
        highs = [p for p in last4 if p[2] == 1]
        lows = [p for p in last4 if p[2] == -1]
        if len(highs) == 2 and len(lows) == 2 and highs[1][1] < highs[0][1] and lows[1][1] > lows[0][1]:
            def line(p, q):
                slope = (q[1] - p[1]) / (q[0] - p[0])
                return lambda i: p[1] + slope * (i - p[0])
            top, bottom = line(*highs), line(*lows)
            if first_cross(c, t, since, lambda i: c[i] > top(i)):
                out["triangle"] = 1
            elif first_cross(c, t, since, lambda i: c[i] < bottom(i)):
                out["triangle"] = -1
    return out


def signals(rows):
    """Per day of `rows` ([date, open, high, low, close]): {name: direction}."""
    o, h, l, c = (np.array([r[k] for r in rows], float) for k in (1, 2, 3, 4))
    pts = turning_points(c)
    return [{**candle_signals(o, h, l, c, t), **chart_signals(c, pts, t)} for t in range(len(rows))]


# ---- data --------------------------------------------------------------------------------

def universe_rows(data_dir):
    path = os.path.join(data_dir, "candles.json")
    try:
        return json.load(open(path))
    except FileNotFoundError:
        pass
    uni = be.read_universe(os.path.join(ROOT, "backtest_universe.txt"))
    out = {}
    for t in uni["tickers"]:
        d = backtest.download(t, date(2004, 1, 1))
        d = be.cut(d, *uni["spans"].get(t, (None, None)))
        out[t] = d["rows"]
    json.dump(out, open(path, "w"))
    return out


def event_table(rows_of, data_dir):
    """Every pattern event, with question A's outcome. Cached in DATA/events.json."""
    path = os.path.join(data_dir, "events.json")
    try:
        return json.load(open(path))
    except FileNotFoundError:
        pass
    sig, fwd = {}, {n: {} for n in FORWARD.values()}
    for t, rows in rows_of.items():
        sig[t] = signals(rows)
        c = [r[4] for r in rows]
        for n in fwd:
            for i, r in enumerate(rows):
                if i + n < len(rows) and c[i] > 0:
                    fwd[n].setdefault(r[0], []).append(c[i + n] / c[i] - 1)
    market = {n: {d: float(np.mean(v)) for d, v in x.items()} for n, x in fwd.items()}
    events = []
    for t, rows in rows_of.items():
        c = [r[4] for r in rows]
        for i, s in enumerate(sig[t]):
            day = rows[i][0]
            for name, direction in s.items():
                n = FORWARD[family(name)]
                if day < START or i + n >= len(rows) or c[i] <= 0:
                    continue
                excess = (c[i + n] / c[i] - 1 - market[n][day]) * direction - COST
                events.append({"ticker": t, "day": day, "name": name, "dir": direction, "excess": excess,
                               "part": be.part_of(day, rows[i + n][0])})
    by_day = {t: {rows[i][0]: s for i, s in enumerate(sig[t]) if s} for t, rows in rows_of.items()}
    json.dump({"events": events, "signals": by_day}, open(path, "w"))
    return json.load(open(path))


# ---- statistics --------------------------------------------------------------------------

def cluster_t(values, months):
    """mean, clustered standard error by month, t."""
    v = np.asarray(values, float)
    n = len(v)
    if n == 0:
        return float("nan"), float("nan"), 0.0
    m = v.mean()
    sums = {}
    for x, k in zip(v - m, months):
        sums[k] = sums.get(k, 0.0) + x
    se = math.sqrt(sum(s * s for s in sums.values())) / n
    return m, se, (m / se if se else 0.0)


def diff_t(net, agree, months):
    """mean(net | agree) - mean(net), its clustered standard error and t (influence per row)."""
    net, agree = np.asarray(net, float), np.asarray(agree, bool)
    na, n = agree.sum(), len(net)
    if na == 0:
        return float("nan"), float("nan"), 0.0
    ma, m = net[agree].mean(), net.mean()
    infl = np.where(agree, (net - ma) / na, 0.0) - (net - m) / n
    sums = {}
    for x, k in zip(infl, months):
        sums[k] = sums.get(k, 0.0) + x
    se = math.sqrt(sum(s * s for s in sums.values()))
    return ma - m, se, ((ma - m) / se if se else 0.0)


def blocks(values, years):
    out = []
    for a, b in BLOCKS:
        x = [v for v, y in zip(values, years) if a <= y <= b]
        out.append(float(np.mean(x)) if x else float("nan"))
    return out


def setups(data_dir, horizon, part):
    import pandas as pd
    df = pd.read_csv(os.path.join(data_dir, f"{horizon}.csv"))
    df = df[df["part"] == part].copy()
    filled = df["filled"].astype(str).str.lower().isin(["true", "1"])
    df["net"] = df["r"] - np.where(filled, COST * df["entry"] / df["risk"], 0.0)
    return df


def agreeing(df, name, signals_of, rows_of):
    """Per setup: did `name` fire, in the setup's direction, on the as-of day or the LOOKBACK days before?"""
    days = {t: [r[0] for r in rows] for t, rows in rows_of.items()}
    out = []
    for t, as_of, side in zip(df["ticker"], df["as_of"], df["side"]):
        d = days.get(t, [])
        i = bisect.bisect_right(d, as_of) - 1
        want = 1 if side == "long" else -1
        sig = signals_of.get(t, {})
        out.append(any(sig.get(d[k], {}).get(name) == want for k in range(max(i - LOOKBACK, 0), i + 1)))
    return np.array(out)


def evaluate(data_dir, part, only=None):
    rows_of = universe_rows(data_dir)
    table = event_table(rows_of, data_dir)
    events, sig = table["events"], table["signals"]
    frames = {h: setups(data_dir, h, part) for h in set(SETUP_HORIZON.values())}
    results = []
    for name in CANDLES + CHARTS:
        h = SETUP_HORIZON[family(name)]
        df = frames[h]
        months = df["as_of"].str[:7].tolist()
        years = df["as_of"].str[:4].astype(int).tolist()
        base, _, _ = cluster_t(df["net"], months)
        for direction in (1, -1):
            if only is not None and [name, direction] not in only:
                continue
            ev = [e for e in events if e["name"] == name and e["dir"] == direction and e["part"] == part]
            a = cluster_t([e["excess"] for e in ev], [e["day"][:7] for e in ev])
            a_blocks = blocks([e["excess"] for e in ev], [int(e["day"][:4]) for e in ev])
            side = "long" if direction == 1 else "short"
            sub = df[df["side"] == side]
            agree = agreeing(sub, name, sig, rows_of)
            sm = sub["as_of"].str[:7].tolist()
            b_mean, _, _ = cluster_t(sub["net"][agree], [m for m, x in zip(sm, agree) if x])
            b_diff = diff_t(df["net"], np.isin(df.index, sub.index[agree]), months)
            b_blocks = blocks(sub["net"][agree].tolist(), [y for y, x in zip(sub["as_of"].str[:4].astype(int), agree) if x])
            results.append({
                "name": name, "dir": direction, "horizon_A": FORWARD[family(name)], "horizon_B": h,
                "events": len(ev), "dates": len({e["day"] for e in ev}),
                "A_mean": a[0], "A_t": a[2], "A_blocks": a_blocks,
                "B_setups": int(agree.sum()), "B_dates": int(sub["as_of"][agree].nunique()),
                "B_net": b_mean, "B_all": base, "B_diff": b_diff[0], "B_t": b_diff[2], "B_blocks": b_blocks})
    return results


def passes_fit(r):
    if r["events"] < MIN_EVENTS or r["dates"] < MIN_DATES:
        return False
    a = r["A_mean"] > 0 and r["A_t"] >= T_FIT and all(x > 0 for x in r["A_blocks"])
    b = (r["B_setups"] >= MIN_EVENTS and r["B_dates"] >= MIN_DATES and r["B_net"] > 0 and r["B_diff"] > 0
         and r["B_t"] >= T_FIT and all(x > 0 for x in r["B_blocks"]))
    return a or b


def show(r, mark=""):
    d = "bull" if r["dir"] == 1 else "bear"
    blk = lambda x: "/".join(f"{v:+.3f}" if v == v else "nan" for v in x)
    return (f"{mark:1} {r['name']:15} {d}  A: n {r['events']:6} dates {r['dates']:5} excess {r['A_mean']:+.4f} "
            f"t {r['A_t']:+5.1f} [{blk(r['A_blocks'])}]  |  B {r['horizon_B']}: setups {r['B_setups']:5} "
            f"net {r['B_net']:+.3f} vs all {r['B_all']:+.3f} diff {r['B_diff']:+.3f} t {r['B_t']:+5.1f} [{blk(r['B_blocks'])}]")


def fit(data_dir):
    res = evaluate(data_dir, "fit")
    print("Fit part. A: excess return in the pattern's direction over the universe mean, net of 0.20%, "
          f"over {FORWARD['candle']} (candle) / {FORWARD['chart']} (chart) trading days. B: net R per order "
          "of the setups agreeing with the pattern, against all setups of the horizon.")
    frozen = []
    for r in res:
        ok = passes_fit(r)
        print(show(r, "*" if ok else ""))
        if ok:
            frozen.append([r["name"], r["dir"]])
    json.dump({"protocol": "README, Candlestick and chart patterns: protocol", "frozen": frozen, "fit": res},
              open(os.path.join(HERE, "FROZEN.json"), "w"), indent=1, default=float)
    print("passing:", frozen or "none (the held-out part is not read)")


def held_out(data_dir):
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))["frozen"]
    if not frozen:
        sys.exit("nothing passed the fit part: the held-out part is not read")
    res = evaluate(data_dir, "held-out", only=frozen)
    out = []
    for r in res:
        a = r["A_mean"] > 0 and r["A_t"] >= T_HELD
        b = r["B_net"] > 0 and r["B_diff"] > 0 and r["B_t"] >= T_HELD
        r["holds"] = bool(a or b)
        print(show(r, "+" if r["holds"] else "-"), "HOLDS" if r["holds"] else "does not hold")
        out.append(r)
    json.dump(out, open(os.path.join(HERE, "held_out.json"), "w"), indent=1, default=float)


# ---- self-test ---------------------------------------------------------------------------

def selftest():
    def rows_from(ohlc):
        return [[f"2020-01-{i + 1:02d}" if i < 31 else f"x{i}", *x] for i, x in enumerate(ohlc)]
    down = [(10 - i * .3 + .1, 10 - i * .3 + .2, 10 - i * .3 - .2, 10 - i * .3 - .1) for i in range(7)]
    up = [(10 + i * .3 - .1, 10 + i * .3 + .2, 10 + i * .3 - .2, 10 + i * .3 + .1) for i in range(7)]
    last = down[-1][3]
    cases = {
        "engulfing +1": down + [(last + .1, last + .15, last - .45, last - .4), (last - .5, last + .4, last - .55, last + .3)],
        "hammer +1": down + [(last - .3, last - .28, last - 1.3, last - .25)],
        "hammer -1": up + [(up[-1][3] + .3, up[-1][3] + 1.3, up[-1][3] + .28, up[-1][3] + .25)],
        "piercing +1": down + [(last, last + .05, last - 1.05, last - 1.0), (last - 1.2, last - .2, last - 1.25, last - .3)],
        "soldiers +1": down + [(9, 9.55, 8.98, 9.5), (9.3, 10.05, 9.28, 10.0), (9.8, 10.55, 9.78, 10.5)],
    }
    for label, ohlc in cases.items():
        name, want = label.split()
        got = signals(rows_from(ohlc))[-1].get(name)
        assert got == int(want), (label, signals(rows_from(ohlc))[-1])
    # a double top: rise to 12, fall to 11, back to 12, then a close below 11
    path = list(np.linspace(10, 12, 20)) + list(np.linspace(12, 11, 10)) + list(np.linspace(11, 12.05, 10)) \
        + list(np.linspace(12.05, 11.2, 8)) + [11.1, 10.9, 10.8, 10.7, 10.6, 10.5, 10.4]
    rows = [[f"d{i:03d}", x, x * 1.001, x * .999, x] for i, x in enumerate(path)]
    s = signals(rows)
    hits = [i for i, x in enumerate(s) if x.get("double") == -1]
    assert hits and path[hits[0]] < 11 and path[hits[0] - 1] >= 11, hits
    # no look-ahead: each day's signals from the candles up to that day only
    rng = np.random.default_rng(7)
    c = 50 * np.exp(np.cumsum(rng.normal(0, .02, 600)))
    o = c * np.exp(rng.normal(0, .01, 600))
    rows = [[f"d{i:04d}", o[i], max(o[i], c[i]) * 1.01, min(o[i], c[i]) * .99, c[i]] for i in range(600)]
    full = signals(rows)
    for t in range(0, 600, 7):
        assert signals(rows[:t + 1])[t] == full[t], t
    assert sum(bool(x) for x in full) > 20
    print("selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "selftest":
        selftest()
    else:
        {"fit": fit, "held-out": held_out}[cmd](sys.argv[2])
