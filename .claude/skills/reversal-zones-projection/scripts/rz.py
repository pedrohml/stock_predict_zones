#!/usr/bin/env python3
"""Helper for the reversal-zones projection. Usage:
  rz.py levels INPUT.json              -> facts for choosing zones
  rz.py sim    INPUT.json ZONES.json   -> baseline probabilities
  rz.py check  OUTPUT.json             -> validate the finished projection
"""
import calendar, json, sys
from datetime import date, datetime, timedelta
import numpy as np

HORIZON_DAYS = {"today": 1, "1-2w": 14, "1-3m": 91, "6-12m": 365}
CANDLE_DAYS = {"1D": 1.4, "1W": 7, "1M": 30.44}


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    basis = d["want"]["close_basis"]
    main = next((s for s in d["data"]["series"] if s["candles"] == basis), d["data"]["series"][-1])
    cols = d["data"]["columns"]
    rows = main["rows"]
    get = lambda name: np.array([r[cols.index(name)] for r in rows], float)
    return d, main, [r[0] for r in rows], get("high"), get("low"), get("close")


def vol(close):
    r = np.diff(np.log(close))
    sd = lambda n: float(np.std(r[-min(n, len(r)):], ddof=1))
    return {"last_8": sd(8), "last_52": sd(52),
            "assumed": [(sd(13) + sd(26)) / 2, (sd(52) + sd(len(r))) / 2]}


def levels(path):
    d, main, t, h, l, c = load(path)
    price, k, n = d["now"]["price"], 3, len(c)
    piv = lambda a, f: [[t[i], a[i]] for i in range(k, n - k) if a[i] == f(a[i - k:i + k + 1])]
    ih, il = int(h.argmax()), int(l.argmin())
    if ih > il:   # rally then pullback: measure the pullback from the high
        lo, hi = float(l[:ih + 1].min()), float(h[ih]); ilo = int(l[:ih + 1].argmin())
        swing = {"low": [lo, t[ilo]], "high": [hi, t[ih]]}
        retr = {f: round(hi - (hi - lo) * f, 2) for f in (0.382, 0.5, 0.618, 0.786)}
        done = (hi - price) / (hi - lo)
    else:         # decline then rebound: measure the rebound from the low
        hi, lo = float(h[:il + 1].max()), float(l[il]); ihi = int(h[:il + 1].argmax())
        swing = {"high": [hi, t[ihi]], "low": [lo, t[il]]}
        retr = {f: round(lo + (hi - lo) * f, 2) for f in (0.382, 0.5, 0.618, 0.786)}
        done = (price - lo) / (hi - lo)
    v = vol(c)
    w = min(26, n); wh, wl = h[-w:], l[-w:]; move = 2 * v["assumed"][0] * price
    if wh.max() - wl.min() < 2 * move: trend = "sideways"
    elif wh.argmax() > wl.argmin(): trend = "down" if wh.max() - price > move else "up"
    else: trend = "up" if price - wl.min() > move else "down"
    gaps = [[t[i], round(float(h[i]), 2), round(float(l[i - 1]), 2)] for i in range(1, n) if h[i] < l[i - 1]]
    gaps += [[t[i], round(float(h[i - 1]), 2), round(float(l[i]), 2)] for i in range(1, n) if l[i] > h[i - 1]]
    out = {
        "price": price, "candles": main["candles"], "rows": n, "last_row": main["last_row"],
        "trend": trend, "swing": swing, "retraced": round(done, 2),
        "from_high": round(price / float(h.max()) - 1, 3), "retracements": retr,
        "volatility": {key: (round(x, 3) if isinstance(x, float) else [round(y, 3) for y in x]) for key, x in v.items()},
        "typical_zone_width": [round(price * v["assumed"][0] * 0.5, 2), round(price * v["assumed"][0], 2)],
        "pivot_lows": piv(l, np.min)[-25:], "pivot_highs": piv(h, np.max)[-25:],
        "recent_lows": [[t[i], float(l[i])] for i in range(n - k, n)],
        "recent_highs": [[t[i], float(h[i])] for i in range(n - k, n)],
        "gap_candidates": gaps[-8:],
    }
    for key, value in out.items(): print(key, json.dumps(value))


def schedule(d, main, t):
    """Dates of the future closes and the share of the first candle still to run."""
    basis, now = main["candles"], datetime.fromisoformat(d["now"]["time"])
    forming, session, today = main["last_row"] == "forming", d["now"]["session"], now.date()
    if basis in CANDLE_DAYS:
        n = max(1, round(HORIZON_DAYS[d["horizon"]] / CANDLE_DAYS[basis]))
    else:  # intraday: the rest of this session, or one full session if the market is shut
        per_day = max(sum(1 for x in t if x[:10] == day) for day in {x[:10] for x in t})
        done = sum(1 for x in t if x[:10] == today.isoformat())
        n = max(1, per_day - done) if session == "open" else per_day
        return [today] * n, 0.5 if forming else 1.0
    day_done = {"open": 0.5, "pre_market": 0.0}.get(session, 1.0)
    if basis == "1D":
        first = today if forming else today + timedelta(days=1)
        dates, cur = [], first
        while len(dates) < n:
            if cur.weekday() < 5: dates.append(cur)
            cur += timedelta(days=1)
        return dates, 0.5 if forming else 1.0
    if basis == "1W":
        friday = today + timedelta(days=(4 - today.weekday()) % 7)
        first = friday if forming else friday + timedelta(days=7 if friday <= today else 0)
        frac = max(0.1, (5 - min(today.weekday(), 4) - day_done) / 5) if forming else 1.0
        return [first + timedelta(days=7 * i) for i in range(n)], frac
    y, m = today.year, today.month                      # 1M
    if not forming: y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    dates = []
    for _ in range(n):
        dates.append(date(y, m, calendar.monthrange(y, m)[1])); y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    frac = max(0.05, 1 - today.day / calendar.monthrange(today.year, today.month)[1]) if forming else 1.0
    return dates, frac


def sim(path, zones_path, paths=200_000):
    d, main, t, h, l, c = load(path)
    z = json.load(open(zones_path, encoding="utf-8"))
    price, touched = d["now"]["price"], d["want"]["probability"] == "touched_once"
    dates, frac = schedule(d, main, t)
    n, v, sub = len(dates), vol(c), 4 if touched else 1
    step = np.repeat(np.linspace(v["assumed"][0], v["assumed"][1], n), sub) / np.sqrt(sub)
    step[:sub] *= np.sqrt(frac)
    rng = np.random.default_rng(7)
    shock = rng.standard_t(5, (paths, n * sub)) / np.sqrt(5 / 3) * step - 0.5 * step ** 2
    drop = np.zeros(n * sub)
    for s in z.get("shocks", []):   # {"date": "...", "drop": cash} or {"date": "...", "jump": sigma}
        i = next((j for j, day in enumerate(dates) if day >= date.fromisoformat(s["date"])), None)
        if i is None: continue
        if "jump" in s: shock[:, i * sub] += rng.standard_normal(paths) * s["jump"]
        if "drop" in s: drop[i * sub:] += s["drop"]
    p = price * np.exp(np.cumsum(shock, axis=1)) - drop
    lo, hi = p.min(axis=1), p.max(axis=1)

    def side(zones, is_support):
        ext = lo if is_support else hi
        near = [zn[1] if is_support else zn[0] for zn in zones]
        far = [zn[0] if is_support else zn[1] for zn in zones]
        passed = (lambda x: ext <= x) if is_support else (lambda x: ext >= x)
        broke = (lambda x: ext < x) if is_support else (lambda x: ext > x)
        inside = price <= near[0] if is_support else price >= near[0]
        reach = [1.0 if inside else float(passed(near[0]).mean())] + [float(broke(x).mean()) for x in far]
        ids = [("S" if is_support else "R") + str(i + 1) for i in range(len(zones))] + ["below" if is_support else "above"]
        return [{"id": ids[i], "reach": round(reach[i], 2),
                 "turn": round(reach[i] - (reach[i + 1] if i + 1 < len(reach) else 0), 2)} for i in range(len(reach))]

    S, R = z["supports"], z["resistances"]
    s_mid, r_mid = S[min(1, len(S) - 1)][0], R[min(1, len(R) - 1)][1]
    down, up = lo < s_mid, hi > r_mid
    share = lambda mask: round(float(mask.mean()), 2)
    out = {
        "horizon": {"label": d["horizon"], "close_basis": main["candles"], "closes": n,
                    "first": dates[0].isoformat(), "last": dates[-1].isoformat()},
        "volatility": {"last_8": round(v["last_8"], 3), "last_52": round(v["last_52"], 3),
                       "assumed": [round(x, 3) for x in v["assumed"]]},
        "first_candle_share_left": round(frac, 2), "measure": "touch" if touched else "close",
        "supports": side(S, True), "resistances": side(R, False),
        "scenarios": [
            {"name": "breakdown", "p": share(down & ~up), "low": f"below {s_mid}", "high": f"{r_mid} or lower"},
            {"name": "breakout", "p": share(up & ~down), "low": f"{s_mid} or higher", "high": f"above {r_mid}"},
            {"name": "range", "p": share(~down & ~up), "low": f"{s_mid} or higher", "high": f"{r_mid} or lower"},
            {"name": "whipsaw", "p": share(down & up), "low": f"below {s_mid}", "high": f"above {r_mid}"}],
    }
    for key, value in out.items(): print(key, json.dumps(value))


def check(path):
    o, bad = json.load(open(path, encoding="utf-8")), []
    price = o["as_of"]["price"]
    for name in ("supports", "resistances"):
        zs = o[name]
        turn, reach = [x["turn"] for x in zs], [x["reach"] for x in zs]
        for i, x in enumerate(zs):
            if abs(reach[i] - sum(turn[i:])) > 0.011: bad.append(f"{x['id']}: reach must equal the sum of turn from here outward")
            if not x.get("evidence"): bad.append(f"{x['id']}: no evidence")
            if not 0 <= x["turn"] <= x["reach"] <= 1: bad.append(f"{x['id']}: need 0 <= turn <= reach <= 1")
        support = name == "supports"   # walk outward from the price: edges must never turn back
        edges = [e for x in zs for e in (reversed(x["zone"]) if support else x["zone"]) if e is not None]
        if edges != sorted(edges, reverse=support): bad.append(f"{name}: zones must run nearest first without overlapping")
        nearest = zs[0]["zone"]
        if name == "supports" and nearest[0] > price: bad.append("S1 lies above the price")
        if name == "resistances" and nearest[1] < price: bad.append("R1 lies below the price")
    sc = {s["name"]: s["p"] for s in o["scenarios"]}
    if set(sc) != {"breakdown", "breakout", "range", "whipsaw"}: bad.append("scenarios are breakdown, breakout, range, whipsaw")
    else:
        if abs(sum(sc.values()) - 1) > 0.011: bad.append("scenarios must sum to 1")
        for name, key in (("supports", "breakdown"), ("resistances", "breakout")):
            zs = o[name]; broken = zs[min(1, len(zs) - 2) + 1]["reach"]   # chance the middle zone breaks
            if abs(sc[key] + sc["whipsaw"] - broken) > 0.021:
                bad.append(f"{key} + whipsaw must equal {broken}, the reach of the entry after the middle {name[:-1]}")
    if o["confidence"]["level"] not in ("low", "medium"): bad.append("confidence is low or medium, never high")
    for dr in o.get("drivers", []):
        if not str(dr.get("source", "")).startswith("http"): bad.append("driver without a source URL")
    print("OK" if not bad else "\n".join(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    {"levels": levels, "sim": sim, "check": check}[sys.argv[1]](*sys.argv[2:])
