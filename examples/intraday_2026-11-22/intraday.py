"""The order of events inside a daily candle, measured on hourly candles (README, "The order of
events inside a daily candle, measured on hourly candles: protocol").

  uv run python examples/intraday_2026-11-22/intraday.py run OUT.csv [--jobs 12]   # downloads, cases, walks
  uv run python examples/intraday_2026-11-22/intraday.py report OUT.csv            # the numbers

`run` makes, per ticker of backtest_universe.txt and as-of date (1-2w every 5 trading days, 1-3m
every 10) whose window lies inside Yahoo's hourly history, exactly the rules 9 setups
backtest_entries.run_case() makes, walks each on the daily candles under both orderings and, when
the two differ, again on the hourly candles of the same days with the same walk(). A setup whose
window has a day where the hourly candles disagree with the daily one (open, high or low more than
TOLERANCE apart, or no hourly candle) is marked unmeasurable. Changes no rule.
"""
import argparse, bisect, csv, json, multiprocessing, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
import backtest, backtest_entries as be, build_prompt, data_source, mechanical, score_projections as sp  # noqa: E402
import trade_entries as te  # noqa: E402

rz = backtest.rz
STEP = {"1-2w": 5, "1-3m": 10}
TOLERANCE = 0.005
HOURLY_PERIOD = "730d"
CHUNK = 20
FIELDS = ["ticker", "horizon", "as_of", "valid_until", "rank", "side", "setup", "order", "entry", "stop", "target",
          "risk", "per_order", "measurable", "bad_days", "filled", "r", "r_other", "ambiguous", "h_filled",
          "h_r", "h_r_other", "h_ambiguous"]


def hourly(ticker):
    """Hourly candles of the last HOURLY_PERIOD -> rows [local time, open, high, low, close], through data_source."""
    import yfinance as yf
    handle = yf.Ticker(ticker)
    frame, _ = data_source.candles(handle, ticker, "1h", period=HOURLY_PERIOD, cache=False, what=f"{ticker} 1h")
    if frame is None or frame.empty:
        return []
    meta = handle.get_history_metadata() or {}
    tz = meta.get("exchangeTimezoneName")
    if tz and frame.index.tz is not None:
        frame = frame.tz_convert(tz)
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    return [[when.strftime("%Y-%m-%dT%H:%M")] + [float(c[k]) for k in ("Open", "High", "Low", "Close")]
            for when, c in frame.iterrows()]


def by_day(rows):
    days = {}
    for row in rows:
        days.setdefault(row[0][:10], []).append(row)
    return days


def agrees(daily_row, hours):
    """Whether one day's hourly candles add up to its daily open, high and low, within TOLERANCE (the
    close is not compared: Yahoo's hourly candles leave out B3's closing auction; README, amendment)."""
    if not hours:
        return False
    o, h, l = hours[0][1], max(x[2] for x in hours), min(x[3] for x in hours)
    return all(abs(a / b - 1) <= TOLERANCE for a, b in zip((o, h, l), daily_row[1:4]) if b > 0)


def case(ticker, horizon, data, hours_by_day, day, today):
    """Rows of one as-of date ([] when the case is skipped, as backtest_entries skips it)."""
    iso = day.isoformat()
    if not data["rows"] or data["rows"][0][0] > be.history_start(horizon, day).isoformat():
        return []
    at = bisect.bisect_right([row[0] for row in data["rows"]], iso)
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
    try:
        reply = te.build(d, proj)
    except (ArithmeticError, ValueError):
        return []
    valid_until = proj["horizon"]["last"]
    if "entries" not in reply or date.fromisoformat(valid_until) >= today:
        return []
    places = te.places(d)
    after = [row for row in data["rows"] if iso < row[0] <= valid_until]
    if not after or be.stale(after):
        return []
    candles = [[row[0]] + [round(x, places) for x in row[1:]] for row in after]
    bad = [row[0] for row in after if not agrees(row, hours_by_day.get(row[0], []))]
    hours = [[h[0]] + [round(x, places) for x in h[1:]] for row in after for h in hours_by_day.get(row[0], [])]
    if hours:
        hours[-1][4] = candles[-1][4]          # the walk reads a close only at expiry: the daily one
    out = []
    for entry in reply["entries"]:
        s = be.setup_of(entry)
        pess, opt = be.walk(s, candles, False), be.walk(s, candles, True)
        row = {"ticker": ticker, "horizon": horizon, "as_of": iso, "valid_until": valid_until,
               **{k: s[k] for k in ("rank", "side", "setup", "order", "entry", "stop", "target", "risk", "per_order")},
               "measurable": not bad, "bad_days": len(bad), "filled": pess["filled"], "r": pess["r"],
               "r_other": opt["r"], "ambiguous": pess["r"] != opt["r"],
               "h_filled": "", "h_r": "", "h_r_other": "", "h_ambiguous": ""}
        if row["ambiguous"] and not bad:
            hp, ho = be.walk(s, hours, False), be.walk(s, hours, True)
            row.update(h_filled=hp["filled"], h_r=hp["r"], h_r_other=ho["r"], h_ambiguous=hp["r"] != ho["r"])
        out.append(row)
    return out


def cases(task):
    ticker, horizon, data, hours_by_day, days, today = task
    rows = []
    for day in days:
        rows += case(ticker, horizon, data, hours_by_day, day, today)
    return rows


def run(out_path, jobs):
    uni = be.read_universe(os.path.join(ROOT, "backtest_universe.txt"))
    store = os.path.join(os.path.dirname(os.path.abspath(out_path)), "hourly")
    os.makedirs(store, exist_ok=True)
    pool = ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("spawn"))
    with open(out_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        pending = []
        for ticker in uni["tickers"]:
            path = os.path.join(store, ticker + ".json")
            try:
                if os.path.exists(path):
                    hours = json.load(open(path))
                else:
                    hours = hourly(ticker)
                    json.dump(hours, open(path, "w"))
            except data_source.DataError as error:
                print(f"{ticker}: no hourly candles: {error}"[:200], flush=True)
                continue
            if not hours:
                print(f"{ticker}: no hourly candles", flush=True)
                continue
            hours_by_day = by_day(hours)
            first = min(hours_by_day)
            for horizon in STEP:
                start = date.fromisoformat(first) - timedelta(days=1)
                try:
                    data = backtest.download(ticker, start - timedelta(days=backtest.lookback(horizon)))
                except backtest.BacktestError as reason:
                    print(f"{ticker} {horizon}: not run: {reason}", flush=True)
                    continue
                if ticker in uni["spans"]:
                    data = be.cut(data, *uni["spans"][ticker])
                today = sp.today_in(data["tz"])
                days = [d for d in backtest.as_of_days(data["rows"], start, today, STEP[horizon])
                        if (d + timedelta(days=1)).isoformat() >= first]
                futures = [pool.submit(cases, (ticker, horizon, data, hours_by_day, days[i:i + CHUNK], today))
                           for i in range(0, len(days), CHUNK)]
                pending.append((ticker, horizon, futures))
        for ticker, horizon, futures in pending:
            n = 0
            for f in futures:
                rows = f.result()
                writer.writerows(rows)
                n += len({r["as_of"] for r in rows})
            print(f"{ticker} {horizon}: {n} cases", flush=True)
    pool.shutdown()


def report(path):
    import pandas as pd
    df = pd.read_csv(path)
    truth = lambda x: x.astype(str).str.lower().isin(["true", "1"])
    df["ambiguous_b"], df["measurable_b"] = truth(df["ambiguous"]), truth(df["measurable"])
    out = {}
    for horizon in STEP:
        h = df[df["horizon"] == horizon]
        m = h[h["measurable_b"]]
        amb = m[m["ambiguous_b"]].copy()
        amb["h_amb"] = truth(amb["h_ambiguous"])
        resolved = amb[~amb["h_amb"]]
        low = m["r"].copy(); high = m["r"].copy()
        low[amb.index], high[amb.index] = amb["h_r"].astype(float), amb["h_r_other"].astype(float)
        pess, opt = m["r"].mean(), m["r_other"].mean()
        lo, hi = low.mean(), high.mean()
        where = lambda x: (x - pess) / (opt - pess) if opt != pess else float("nan")
        res = {"setups": len(h), "measurable": len(m), "cases": int(m["as_of"].str.cat(m["ticker"]).nunique()),
               "first_as_of": m["as_of"].min(), "last_as_of": m["as_of"].max(),
               "ambiguous": len(amb), "ambiguous_share": len(amb) / len(m),
               "resolved": len(resolved), "resolved_share": len(resolved) / max(1, len(amb)),
               "resolved_favour_share": float((resolved["h_r"].astype(float) > resolved["r"]).mean()) if len(resolved) else float("nan"),
               "resolved_against_share": float((resolved["h_r"].astype(float) == resolved["r"]).mean()) if len(resolved) else float("nan"),
               "r_daily_pessimistic": pess, "r_daily_optimistic": opt, "r_hourly_low": lo, "r_hourly_high": hi,
               "position_low": where(lo), "position_high": where(hi), "stated": m["per_order"].mean()}
        out[horizon] = res
        print(f"{horizon}: {res['measurable']} of {res['setups']} setups measurable ({res['cases']} cases, "
              f"as-of {res['first_as_of']} to {res['last_as_of']})")
        print(f"  ambiguous on daily candles: {res['ambiguous']} ({res['ambiguous_share']:.1%}); resolved by the hours: "
              f"{res['resolved']} ({res['resolved_share']:.0%}), of which in the setup's favour {res['resolved_favour_share']:.0%}, "
              f"as the pessimistic ordering said {res['resolved_against_share']:.0%}")
        print(f"  R per order: stated {res['stated']:+.4f}; daily pessimistic {pess:+.4f}, daily optimistic {opt:+.4f}, "
              f"with the hours {lo:+.4f} to {hi:+.4f} (at {res['position_low']:.0%} to {res['position_high']:.0%} of the way "
              f"from pessimistic to optimistic)")
        for kind, part in m.groupby("setup"):
            print(f"    {kind}: {len(part)} setups, ambiguous {part['ambiguous_b'].mean():.1%}")
    json.dump(out, open(os.path.join(HERE, "report.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("run", "report")); p.add_argument("out"); p.add_argument("--jobs", type=int, default=12)
    a = p.parse_args()
    if a.cmd == "run":
        t0 = time.time(); run(a.out, a.jobs); print(f"{time.time() - t0:.0f}s")
    else:
        report(a.out)
