#!/usr/bin/env python3
"""Helper for the reversal-zones projection. Usage:
  rz.py levels INPUT.json              -> facts for choosing zones
  rz.py sim    INPUT.json ZONES.json   -> baseline probabilities
  rz.py check  OUTPUT.json             -> validate the finished projection
  rz.py save   INPUT.json OUTPUT.json [DIR]  -> keep both under DIR/TICKER/TIME_HORIZON/
  rz.py list   [DIR]                   -> the saved projections, oldest first
  rz.py score  INPUT.json OUTPUT.json CANDLES.json  -> what the price did to a projection
DIR defaults to $RZ_PROJECTIONS_DIR, else ./projections.
"""
import calendar, json, os, re, shutil, sys
from datetime import date, datetime, timedelta
import numpy as np

HORIZON_DAYS = {"today": 1, "1-2w": 14, "1-3m": 91, "6-12m": 365}
SCHEMA_VERSIONS = (1,)   # projection formats check accepts; a projection without the key predates them
CANDLE_DAYS = {"1D": 1.4, "1W": 7, "1M": 30.44}


def main_series(d):
    basis = d["want"]["close_basis"]
    return next((s for s in d["data"]["series"] if s["candles"] == basis), d["data"]["series"][-1])


def load(path):
    return unpack(json.load(open(path, encoding="utf-8")))


def unpack(d):
    """An input already read -> (input, main series, starts, highs, lows, closes)."""
    main = main_series(d)
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
    for key, value in facts(*load(path)).items(): print(key, json.dumps(value))


def facts(d, main, t, h, l, c):
    """What `levels` prints, as a dict (backtest.py chooses its zones from it)."""
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
    return out


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


def walk(d, main, t, c, z, paths):
    """The simulated prices: one row per path, `sub` points per candle, in the order of `dates`."""
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
    return price * np.exp(np.cumsum(shock, axis=1)) - drop, dates, frac, v, sub


def reached(price, ext, zones, is_support):
    """Was each entry of one side reached: one mask per zone, nearest first, then one for the tail.
    `ext` is the lowest price of each path for supports, the highest for resistances. The nearest
    zone is reached at its near edge (always, if the price starts inside it or beyond); every
    later entry is reached when the zone before it broke, strictly beyond its far edge.
    sim() averages these masks over simulated paths; outcome() reads them for the one real path."""
    near = [zn[1] if is_support else zn[0] for zn in zones]
    far = [zn[0] if is_support else zn[1] for zn in zones]
    passed = (lambda x: ext <= x) if is_support else (lambda x: ext >= x)
    broke = (lambda x: ext < x) if is_support else (lambda x: ext > x)
    inside = price <= near[0] if is_support else price >= near[0]
    return [np.full(ext.shape, True) if inside else passed(near[0])] + [broke(x) for x in far]


def middles(S, R):
    """The far edges of the middle zones (second on each side, or the only one): the scenario lines."""
    return S[min(1, len(S) - 1)][0], R[min(1, len(R) - 1)][1]


def sim(path, zones_path, paths=200_000):
    z = json.load(open(zones_path, encoding="utf-8"))
    for key, value in baseline(load(path), z, paths).items(): print(key, json.dumps(value))


def baseline(loaded, z, paths=200_000):
    """What `sim` prints, as a dict. `loaded` is what load() or unpack() returns."""
    d, main, t, h, l, c = loaded
    price, touched = d["now"]["price"], d["want"]["probability"] == "touched_once"
    p, dates, frac, v, _ = walk(d, main, t, c, z, paths)
    n, lo, hi = len(dates), p.min(axis=1), p.max(axis=1)

    def side(zones, is_support):
        reach = [float(mask.mean()) for mask in reached(price, lo if is_support else hi, zones, is_support)]
        reach = [round(x, 2) for x in reach]   # round reach once and derive turn from it, so they always add up
        ids = [("S" if is_support else "R") + str(i + 1) for i in range(len(zones))] + ["below" if is_support else "above"]
        return [{"id": ids[i], "reach": reach[i],
                 "turn": round(reach[i] - (reach[i + 1] if i + 1 < len(reach) else 0), 2)} for i in range(len(reach))]

    S, R = z["supports"], z["resistances"]
    s_mid, r_mid = middles(S, R)
    down, up = lo < s_mid, hi > r_mid
    # Same rule as for reach and turn: round the totals once and take the rest by difference,
    # so breakdown + whipsaw equals the reach past the middle support and the four sum to 1.
    share = lambda mask: round(float(mask.mean()), 2)
    both = share(down & up)
    p_down, p_up = round(share(down) - both, 2), round(share(up) - both, 2)
    p_range = round(1 - p_down - p_up - both, 2)
    out = {
        "horizon": {"label": d["horizon"], "close_basis": main["candles"], "closes": n,
                    "first": dates[0].isoformat(), "last": dates[-1].isoformat()},
        "volatility": {"last_8": round(v["last_8"], 3), "last_52": round(v["last_52"], 3),
                       "assumed": [round(x, 3) for x in v["assumed"]]},
        "first_candle_share_left": round(frac, 2), "measure": "touch" if touched else "close",
        "supports": side(S, True), "resistances": side(R, False),
        "scenarios": [
            {"name": "breakdown", "p": p_down, "low": f"below {s_mid}", "high": f"{r_mid} or lower"},
            {"name": "breakout", "p": p_up, "low": f"{s_mid} or higher", "high": f"above {r_mid}"},
            {"name": "range", "p": p_range, "low": f"{s_mid} or higher", "high": f"{r_mid} or lower"},
            {"name": "whipsaw", "p": both, "low": f"below {s_mid}", "high": f"above {r_mid}"}],
    }
    return out


def problems(o, complete=True):
    """The rules a projection breaks, one line each; [] when it is sound. Never raises: a file too
    malformed to judge gets a line saying so. `complete=False` is for drafts: a missing
    `scenarios` or `confidence` section is skipped instead of reported."""
    bad = []
    try: rules(o, bad, complete)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
        bad.append(f"malformed projection, rules not fully checked ({type(e).__name__}: {e})")
    return bad


def rules(o, bad, complete):
    if "schema_version" in o and o["schema_version"] not in SCHEMA_VERSIONS:
        bad.append(f"schema_version must be {' or '.join(map(str, SCHEMA_VERSIONS))}, got {o['schema_version']!r}")
    price = o["as_of"]["price"]
    for name in ("supports", "resistances"):
        zs = o[name]
        if not zs: bad.append(f"{name}: no zones"); continue
        turn, reach = [x["turn"] for x in zs], [x["reach"] for x in zs]
        for i, x in enumerate(zs):
            if abs(reach[i] - sum(turn[i:])) > 0.011: bad.append(f"{x['id']}: reach must equal the sum of turn from here outward")
            if not x.get("evidence"): bad.append(f"{x['id']}: no evidence")
            if not 0 <= x["turn"] <= x["reach"] <= 1: bad.append(f"{x['id']}: need 0 <= turn <= reach <= 1")
        support = name == "supports"   # walk outward from the price: edges must never turn back
        edges = [e for x in zs for e in (reversed(x["zone"]) if support else x["zone"]) if e is not None]
        if edges != sorted(edges, reverse=support): bad.append(f"{name}: zones must run nearest first without overlapping")
        nearest = zs[0]["zone"]
        if name == "supports" and nearest[0] is not None and nearest[0] > price: bad.append("S1 lies above the price")
        if name == "resistances" and nearest[1] is not None and nearest[1] < price: bad.append("R1 lies below the price")
    sc = {s["name"]: s["p"] for s in o.get("scenarios", [])}
    if "scenarios" not in o and not complete: pass
    elif set(sc) != {"breakdown", "breakout", "range", "whipsaw"}: bad.append("scenarios are breakdown, breakout, range, whipsaw")
    else:
        if abs(sum(sc.values()) - 1) > 0.011: bad.append("scenarios must sum to 1")
        for name, key in (("supports", "breakdown"), ("resistances", "breakout")):
            zs = o[name]
            if not zs: continue
            broken = zs[min(1, len(zs) - 2) + 1]["reach"]   # chance the middle zone breaks
            if abs(sc[key] + sc["whipsaw"] - broken) > 0.021:
                bad.append(f"{key} + whipsaw must equal {broken}, the reach of the entry after the middle {name[:-1]}")
    if "confidence" not in o and not complete: pass
    elif o.get("confidence", {}).get("level") not in ("low", "medium"): bad.append("confidence is low or medium, never high")
    for dr in o.get("drivers", []):
        if not str(dr.get("source", "")).startswith("http"): bad.append("driver without a source URL")


def check(path):
    bad = problems(json.load(open(path, encoding="utf-8")))
    print("OK" if not bad else "\n".join(bad))
    sys.exit(1 if bad else 0)


def store(folder=None):
    return folder or os.environ.get("RZ_PROJECTIONS_DIR") or "projections"


def save(input_path, output_path, folder=None):
    """Keep a finished projection next to the input it was made from, so it can be scored
    against what the price did later. Files are copied as they are; nothing is overwritten."""
    d, o = (json.load(open(p, encoding="utf-8")) for p in (input_path, output_path))
    bad = problems(o)
    if d["asset"]["ticker"].upper() != o["asset"]["ticker"].upper(): bad.append("input and projection are for different tickers")
    if d["now"]["time"] != o["as_of"]["time"]: bad.append("as_of.time must equal the input's now.time")
    if bad:
        print("not saved:\n" + "\n".join(bad)); sys.exit(1)
    safe = lambda text: re.sub(r"[^A-Za-z0-9._-]", "_", text)
    stamp = datetime.fromisoformat(o["as_of"]["time"]).strftime("%Y-%m-%dT%H%M")   # the exchange's local time
    base = os.path.join(store(folder), safe(o["asset"]["ticker"].upper()), f"{stamp}_{safe(o['horizon']['label'])}")
    target, n = base, 1
    while os.path.exists(target):   # the same input projected again: keep every attempt
        n += 1; target = f"{base}_{n}"
    os.makedirs(target)
    shutil.copyfile(input_path, os.path.join(target, "input.json"))
    shutil.copyfile(output_path, os.path.join(target, "projection.json"))
    print("saved", target)


def saved(folder=None):
    """[(folder, projection)] for every saved projection, oldest first."""
    root, found = store(folder), []
    for here, _, files in os.walk(root):
        if "projection.json" in files:
            found.append((here, json.load(open(os.path.join(here, "projection.json"), encoding="utf-8"))))
    return sorted(found, key=lambda item: (datetime.fromisoformat(item[1]["as_of"]["time"]).timestamp(), item[0]))


def list_saved(folder=None):
    """One line per saved projection. `ended` means the last close of the horizon is in the past."""
    for here, o in saved(folder):
        last = o["horizon"]["last"]
        state = "ended" if date.fromisoformat(last) < date.today() else "open"
        print(o["as_of"]["time"], o["asset"]["ticker"], o["horizon"]["label"], o["as_of"]["price"], "to", last, state, here)


def closes_on(basis, start):
    """The day the candle starting at `start` closes: the Friday of its week, the last day of its
    month, else its own day. The same days schedule() gives the future closes."""
    day = date.fromisoformat(start[:10])
    if basis == "1W": return day + timedelta(days=(4 - day.weekday()) % 7)
    if basis == "1M": return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
    return day


def realised(d, main, o, rows, today):
    """The candles of `rows` that fall in the projection's horizon -> (rows, closed, ended, partial).
    The horizon starts with the input's last row if that row was still forming (its close is the
    first close, as in schedule()), else with the candle after it, and runs to `horizon.last`.
    Intraday it is one session: the first one with a candle from that point on. `closed` says, per
    row, whether its close is final; `partial` that the first row began before the projection."""
    basis, last, forming = main["candles"], main["rows"][-1][0], main["last_row"] == "forming"
    if basis in CANDLE_DAYS:
        key, end = (lambda start: closes_on(basis, start)), date.fromisoformat(o["horizon"]["last"])
        rows = [r for r in rows if (key(r[0]) > key(last) or (forming and key(r[0]) == key(last))) and key(r[0]) <= end]
        return rows, [key(r[0]) < today for r in rows], end < today, bool(rows) and key(rows[0][0]) == key(last)
    rows = [r for r in rows if r[0] > last or (forming and r[0] == last)]
    rows = [r for r in rows if r[0][:10] == rows[0][0][:10]]
    ended = bool(rows) and date.fromisoformat(rows[0][0][:10]) < today
    return rows, [ended or r is not rows[-1] for r in rows], ended, bool(rows) and rows[0][0] == last


def outcome(d, o, rows, today=None):
    """What the price did to projection `o`, made from input `d`, judged the way sim() counts one
    simulated path. `rows` are realised candles of the close basis, in the input's column order
    and on the input's price scale (split-adjusted, not dividend-adjusted); rows outside the
    horizon are ignored. While the horizon is open (`final` false) an event that happened stays,
    but `turned` and the scenario can still change. Raises ValueError if nothing can be judged."""
    main, cols = main_series(d), d["data"]["columns"]
    H, L, C = (cols.index(name) for name in ("high", "low", "close"))
    price, touched = d["now"]["price"], d["want"]["probability"] == "touched_once"
    rows, closed, ended, partial = realised(d, main, o, rows, today or date.today())
    if touched:
        lows, highs = [[r[0], r[L]] for r in rows], [[r[0], r[H]] for r in rows]
        if partial:   # the first candle began before the projection: only a new extreme, or its close, is known to come after
            was = main["rows"][-1]
            if lows[0][1] >= was[L]: lows[0][1] = rows[0][C]
            if highs[0][1] <= was[H]: highs[0][1] = rows[0][C]
    else:
        lows = highs = [[r[0], r[C]] for r, done in zip(rows, closed) if done]
    if not lows: raise ValueError("no candle of the horizon has closed yet" if rows else "no candles after the projection")
    low, high = min(lows, key=lambda x: x[1]), max(highs, key=lambda x: x[1])
    out = {"state": "ended" if ended else "open", "final": ended, "measure": "touch" if touched else "close",
           "closes": {"scheduled": o["horizon"]["closes"], "counted": len(lows)},
           "window": [lows[0][0], lows[-1][0]], "low": [low[1], low[0]], "high": [high[1], high[0]]}
    zones = {}
    for name, ext, is_support in (("supports", low[1], True), ("resistances", high[1], False)):
        zones[name] = [x["zone"] for x in o[name] if None not in x["zone"]]
        hit = [bool(mask[0]) for mask in reached(price, np.array([ext]), zones[name], is_support)]
        out[name] = [{"id": x["id"], "zone": x["zone"], "reach": x["reach"], "turn": x["turn"],
                      "forced": i == 0 and bool(price <= x["zone"][1] if is_support else price >= x["zone"][0]),
                      "reached": hit[i], "broken": hit[i + 1] if i + 1 < len(hit) else None,
                      "turned": hit[i] and not (i + 1 < len(hit) and hit[i + 1])}
                     for i, x in enumerate(o[name][:len(hit)])]
    s_mid, r_mid = middles(zones["supports"], zones["resistances"])
    down, up = bool(low[1] < s_mid), bool(high[1] > r_mid)
    out["scenario"] = "whipsaw" if down and up else "breakdown" if down else "breakout" if up else "range"
    out["scenarios"] = [{"name": s["name"], "p": s["p"], "happened": s["name"] == out["scenario"]} for s in o.get("scenarios", [])]
    return out


def score(input_path, output_path, candles_path):
    d, o, rows = (json.load(open(p, encoding="utf-8")) for p in (input_path, output_path, candles_path))
    try: out = outcome(d, o, rows["rows"] if isinstance(rows, dict) else rows)
    except ValueError as e:
        print("not scored:", e); sys.exit(1)
    for key, value in out.items(): print(key, json.dumps(value))


if __name__ == "__main__":
    {"levels": levels, "sim": sim, "check": check, "save": save, "list": list_saved, "score": score}[sys.argv[1]](*sys.argv[2:])
