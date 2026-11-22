"""A stop sized by volatility, and dropping the offending kinds of setup (README, "A stop sized by
volatility, and dropping the offending kinds of setup: protocol"). Adapted from
examples/minrisk_2026-11-22/minrisk.py: only the variants, the statistic and the choice differ.

  uv run python examples/volstop_2026-11-22/volstop.py run OUT.csv UNIVERSE_FILE [--part fit] [--jobs 14]
  uv run python examples/volstop_2026-11-22/volstop.py fit OUT_FIT.csv            # writes FROZEN.json
  uv run python examples/volstop_2026-11-22/volstop.py judge OUT_FIT.csv OUT_FRESH.csv   # once

`run` makes, per case of the grid (1-2w every 10 trading days, 1-3m every 20), exactly what
backtest_entries.run_case() makes up to the setups, with trade_entries.MIN_RISK switched off so that
the 2% minimum can be replaced; then for every variant it drops the setups the variant's filter
rejects, passes the rest to trade_entries.build() (which picks the best three) and walks each kept
setup with backtest_entries.walk() under both orderings. `base` is rules 8 unchanged (the 2% minimum
at 1-2w applied here exactly as setups() applies it); `selftest` checks that on a few cases.
"""
import argparse, bisect, csv, json, math, multiprocessing, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be, build_prompt, mechanical, score_projections as sp  # noqa: E402
import trade_entries as te  # noqa: E402

RULES8_MIN_RISK = dict(te.MIN_RISK)
te.MIN_RISK = {}          # every process imports this module, so the workers see it too

rz = backtest.rz
START = date(2005, 1, 3)
ENDS = {"1-2w": date(2026, 10, 30), "1-3m": date(2026, 8, 14)}
STEP = {"1-2w": 10, "1-3m": 20}
KS = (None, 0.5, 1, 1.5, 2)                       # minimum risk in units of one candle's move
KINDS = {"": (), "nomkt": ("market",), "nocont": ("continuation",), "nomkt+nocont": ("market", "continuation")}
VARIANTS = tuple((f"v{k}" if k else "base") + (f"+{kind}" if kind else "") for k in KS for kind in KINDS)
COST = 0.002
BLOCKS = ((2005, 2012), (2013, 2019), (2020, 2026))
CHUNK = 25
FIELDS = ["variant", "ticker", "horizon", "as_of", "valid_until", "part", "trend_sma", "trend_mom", "rank", "side",
          "setup", "order", "entry", "stop", "target", "risk", "ratio", "fill", "win_if_filled", "if_filled",
          "per_order", "filled", "won", "r", "r_other", "exit"]


def trend(rows, day):
    """(sma side, momentum side) at the close of `day`, or None without 252 days of history."""
    closes = [r[4] for r in rows if r[0] <= day]
    if len(closes) < 253:
        return None
    c = closes[-1]
    sma = "long" if c > sum(closes[-200:]) / 200 else "short"
    mom = "long" if closes[-22] / closes[-253] - 1 > 0 else "short"
    return sma, mom


def case(ticker, horizon, data, day, today):
    """Rows of one as-of date for every variant ([] when the case is skipped)."""
    iso = day.isoformat()
    if not data["rows"] or data["rows"][0][0] > be.history_start(horizon, day).isoformat():
        return []
    at = bisect.bisect_right([row[0] for row in data["rows"]], iso)
    if be.stale(data["rows"][max(0, at - be.STALE_DAYS):at]):
        return []
    sides = trend(data["rows"], iso) or ("n/a", "n/a")   # recorded only; no case is skipped for it
    try:
        d = backtest.input_as_of(ticker, horizon, data, day)
    except ValueError as error:
        if "nonexistent time" not in str(error):
            raise
        return []
    main = rz.unpack(d)[1]
    if len(main["rows"]) < build_prompt.PRESETS[horizon]["main"][2]:
        return []
    try:
        proj = mechanical.build(d)
    except (mechanical.MechanicalError, ArithmeticError, ValueError):
        return []
    if te.precondition(d, proj):
        return []
    try:
        found = te.setups(d, proj)
    except (ArithmeticError, ValueError):
        return []
    valid_until = proj["horizon"]["last"]
    if date.fromisoformat(valid_until) >= today:
        return []
    places = te.places(d)
    after = [row for row in data["rows"] if iso < row[0] <= valid_until]
    if not after or be.stale(after):
        return []
    candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in after]
    part = be.part_of(iso, valid_until)
    out = []
    move, minimum = te.units(proj, places)["move"], RULES8_MIN_RISK.get(horizon, 0.0)
    for variant in VARIANTS:
        keep = [item for item in found if kept(variant, item, move, minimum, places)]
        reply = te.build(d, proj, found=keep) if keep else {"error": "none"}
        for entry in reply.get("entries", []):
            s = be.setup_of(entry)
            chosen, other = be.walk(s, candles, False), be.walk(s, candles, True)
            out.append({"variant": variant, "ticker": ticker, "horizon": horizon, "as_of": iso,
                        "valid_until": valid_until, "part": part, "trend_sma": sides[0], "trend_mom": sides[1],
                        **{k: s[k] for k in ("rank", "side", "setup", "order", "entry", "stop", "target", "risk",
                                             "ratio", "fill", "win_if_filled", "if_filled", "per_order")},
                        "filled": chosen["filled"], "won": chosen.get("won"), "r": chosen["r"],
                        "r_other": other["r"], "exit": chosen["exit"]})
        if variant == "base" and not out:
            return []          # no base setup: the case is skipped for every variant, as in backtest_entries
    return out


def kept(variant, item, move, minimum, places):
    """Whether `variant` keeps one setup of trade_entries.setups() (computed without MIN_RISK)."""
    head, _, kind = variant.partition("+")
    if item["order"] in KINDS[kind] or item["setup"] in KINDS[kind]:
        return False
    if head == "base":                                   # rules 8: the 2% minimum, as setups() applies it
        return not item["risk"] < minimum * te.half_up(item["entry"], places)
    return item["risk"] >= float(head[1:]) * move


def selftest(universe_path, n=40):
    """`base` rows equal trade_entries.build() with the real MIN_RISK on some cases of each horizon."""
    uni = be.read_universe(universe_path)
    checked = 0
    for horizon in ("1-2w", "1-3m"):
        for ticker in uni["tickers"][:3]:
            data = backtest.download(ticker, START - timedelta(days=backtest.lookback(horizon)))
            days = backtest.as_of_days(data["rows"], START, ENDS[horizon], STEP[horizon])
            for day in days[::max(1, len(days) // n)]:
                rows = [r for r in case(ticker, horizon, data, day, date(2026, 11, 22)) if r["variant"] == "base"]
                if not rows:
                    continue
                d = backtest.input_as_of(ticker, horizon, data, day)
                proj = mechanical.build(d)
                te.MIN_RISK = RULES8_MIN_RISK
                try:
                    reply = te.build(d, proj)
                finally:
                    te.MIN_RISK = {}
                want = [(e["id"], e["entry"]["price"], e["stop"]["price"], e["target"]["price"]) for e in reply["entries"]]
                got = [(r["setup"], r["entry"], r["stop"], r["target"]) for r in rows]
                assert [w[1:] for w in want] == [g[1:] for g in got], (ticker, horizon, day, want, got)
                checked += 1
    print(f"selftest: base equals rules 8 on {checked} cases")


def cases(task):
    ticker, horizon, data, days, today, part = task
    rows = []
    for day in days:
        r = case(ticker, horizon, data, day, today)
        if r and (part is None or r[0]["part"] == part):
            rows += r
    return rows


def run(out_path, universe_path, part, jobs):
    uni = be.read_universe(universe_path)
    pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"))
    with open(out_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        for horizon in ("1-2w", "1-3m"):
            pending = []
            for ticker in uni["tickers"]:
                try:
                    data = backtest.download(ticker, START - timedelta(days=backtest.lookback(horizon)))
                except backtest.BacktestError as reason:
                    print(f"{ticker} {horizon}: not run: {reason}", flush=True)
                    continue
                if ticker in uni["spans"]:
                    data = be.cut(data, *uni["spans"][ticker])
                today = sp.today_in(data["tz"])
                days = backtest.as_of_days(data["rows"], START, ENDS[horizon], STEP[horizon])
                futures = [pool.submit(cases, (ticker, horizon, data, days[i:i + CHUNK], today, part))
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


# ---- evaluation ----------------------------------------------------------------------------

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


def compare(df, horizon, variant):
    """Net R per order of `variant` and of base on the same cases, the difference, its clustered t."""
    h = df[df["horizon"] == horizon]
    v, b = h[h["variant"] == variant], h[h["variant"] == "base"]
    if len(v) == 0:
        return None
    mv, mb = v["r"].mean(), b["r"].mean()
    infl_v = (v["r"] - mv) / len(v)
    infl_b = (b["r"] - mb) / len(b)
    se = clustered(list(infl_v) + list(-infl_b), list(v["month"]) + list(b["month"]))
    se_v = clustered(list(infl_v), list(v["month"]))
    blocks = []
    for lo, hi in BLOCKS:
        vv, bb = v[(v["year"] >= lo) & (v["year"] <= hi)], b[(b["year"] >= lo) & (b["year"] <= hi)]
        blocks.append(vv["r"].mean() - bb["r"].mean() if len(vv) and len(bb) else float("nan"))
    stated, observed = v["per_order"].mean(), v["r"].mean()
    return {"variant": variant, "horizon": horizon, "orders": len(v), "base_orders": len(b),
            "cases": h["as_of"].str.cat(h["ticker"]).nunique(),
            "long_share": float((v["side"] == "long").mean()), "fill": float(v["filled_b"].mean()),
            "gross": mv, "t_gross": mv / se_v if se_v else 0.0, "base_gross": mb, "diff": mv - mb,
            "net": v["net"].mean(), "base_net": b["net"].mean(), "setups_per_case": len(v) / max(1, v["as_of"].str.cat(v["ticker"]).nunique()),
            "t": (mv - mb) / se if se else 0.0, "blocks": blocks, "stated": stated, "observed": observed,
            "opt": v["r_other"].mean(), "gap": abs(stated - observed),
            "base_gap": abs(b["per_order"].mean() - b["r"].mean())}


def line(c):
    blk = "/".join(f"{x:+.3f}" if x == x else "nan" for x in c["blocks"])
    return (f"{c['horizon']:5} {c['variant']:15} orders {c['orders']:6} ({c['orders'] / c['base_orders']:4.0%}) long {c['long_share']:4.0%} "
            f"fill {c['fill']:4.0%} R/o st {c['stated']:+.3f} obs {c['observed']:+.3f} (opt {c['opt']:+.3f}) "
            f"vs base {c['base_gross']:+.3f} diff {c['diff']:+.3f} t {c['t']:+.1f} "
            f"[{blk}] gap {c['gap']:.3f} (base {c['base_gap']:.3f}) net {c['net']:+.3f} (base {c['base_net']:+.3f})")


def passes(c):
    return c["diff"] > 0 and c["t"] >= 3 and all(x > 0 for x in c["blocks"]) and c["orders"] >= c["base_orders"] / 2


def fit(path):
    df = load(path)
    df = df[df["part"] == "fit"]
    frozen, out = {}, []
    for horizon in ("1-2w", "1-3m"):
        best = None
        for variant in VARIANTS:
            c = compare(df, horizon, variant)
            ok = variant != "base" and passes(c)
            print(("* " if ok else "  ") + line(c))
            out.append(c)
            if ok and (best is None or c["gross"] > best["gross"]):
                best = c
        if best:
            frozen[horizon] = best["variant"]
    json.dump({"protocol": "README, A stop sized by volatility, and dropping the offending kinds of setup", "frozen": frozen, "fit": out},
              open(os.path.join(HERE, "FROZEN.json"), "w"), indent=1, default=float)
    print("frozen:", frozen or "none (the fresh universe is not judged)")


def judge(fit_path, fresh_path):
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))["frozen"]
    if not frozen:
        sys.exit("nothing was frozen on the fit part")
    df = load(fresh_path)
    out = []
    for horizon, variant in frozen.items():
        c = compare(df, horizon, variant)
        c["adopted"] = bool(c["diff"] > 0 and c["t"] >= 2 and c["gap"] <= c["base_gap"] + 0.02)
        print(line(c), "-> ADOPTED" if c["adopted"] else "-> not adopted")
        for part in ("fit", "held-out"):
            p = compare(df[df["part"] == part], horizon, variant)
            if p:
                print(f"   fresh tickers, {part} years: " + line(p))
        out.append(c)
    print("for reference, every variant on the fresh tickers:")
    for horizon in ("1-2w", "1-3m"):
        for variant in VARIANTS:
            print("   " + line(compare(df, horizon, variant)))
    json.dump(out, open(os.path.join(HERE, "judge.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        p = argparse.ArgumentParser()
        p.add_argument("cmd"); p.add_argument("out"); p.add_argument("universe")
        p.add_argument("--part"); p.add_argument("--jobs", type=int, default=14)
        a = p.parse_args()
        t0 = time.time(); run(a.out, a.universe, a.part, a.jobs); print(f"{time.time() - t0:.0f}s")
    elif cmd == "selftest":
        selftest(sys.argv[2])
    elif cmd == "fit":
        fit(sys.argv[2])
    else:
        judge(sys.argv[2], sys.argv[3])
