"""Check the harness against the live code on sampled fit cases: ids, prices and raw odds of
rules 5 (variant 0) equal trade_entries.setups() / odds(), and the walks equal run_case()'s.

    uv run python examples/placement_2026-11-21/verify.py
"""
import os
import random
import sys
import time
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import harness as H  # noqa: E402
import backtest  # noqa: E402
import backtest_entries as be  # noqa: E402
import score_projections as sp  # noqa: E402
import trade_entries as te  # noqa: E402

random.seed(1)
bad = n = 0
for horizon in ("1-2w", "1-3m", "6-12m"):
    for ticker in ("PETR4.SA", "NVDA", "GE"):
        data = backtest.download(ticker, H.START - timedelta(days=backtest.lookback(horizon)))
        today = sp.today_in(data["tz"])
        days = backtest.as_of_days(data["rows"], H.START, H.ENDS[horizon], backtest.DEFAULT_STEP[horizon])
        days = [d for d in days if d.year not in be.HELD_OUT_YEARS]
        for day in random.sample(days, 4):
            made = H.make_case(ticker, horizon, data, day, today, "fit")
            if made is None:
                continue
            d, proj, candles, valid_until, digits = made
            clock = time.time()
            rows = H.case_rows(0, d, proj, candles, digits, list(range(len(H.VARIANTS))))
            spent = time.time() - clock
            found = te.setups(d, proj)
            sim = te.paths(d)
            mine = {r[1]: r for r in rows[0]}
            if set(mine) != {f["id"] for f in found}:
                bad += 1
                print("ids differ", ticker, day, set(mine) ^ {f["id"] for f in found})
            for f in found:
                r = mine.get(f["id"])
                if r is None:
                    continue
                raw = te.odds(sim, f["side"], f["order"], f["entry"], f["stop"], f["target"], f["ratio"])
                if (r[5], r[6], r[7], r[8], r[10]) != (f["entry"], f["stop"], f["target"], f["risk"], f["ratio"]) or \
                        (r[11], r[12], r[13], r[14], r[15]) != (raw["fill"], raw["win"], raw["loss"], raw["open"],
                                                                raw["if_filled"]):
                    bad += 1
                    print("diff", ticker, day, f["id"], r[5:16], raw)
            for variant, got in rows.items():   # every variant's odds against odds() itself
                for r in got[:3]:
                    raw = te.odds(sim, r[2], r[4], r[5], r[6], r[7], r[10])
                    if (r[11], r[12], r[15]) != (raw["fill"], raw["win"], raw["if_filled"]):
                        bad += 1
                        print("variant odds differ", variant, r[1])
            record = be.run_case(ticker, horizon, data, day, today)
            for s in record.get("setups", []):
                r = mine[s["id"]]
                if (r[16], r[17], r[18], r[19]) != (s["filled"], s["won"], s["r"], s["r_other"]):
                    bad += 1
                    print("walk diff", s["id"])
            n += 1
            print(horizon, ticker, day, "rows", sum(len(v) for v in rows.values()), f"{spent:.2f}s", flush=True)
print("cases", n, "problems", bad)
