"""Zone-rule study, test 4: backtest.py's calibration (Brier of reach and turn per horizon) under a
candidate zone rule, on the twelve tickers of the baseline backtest, default dates and steps.

    uv run python examples/zone_rule_2026-11-21/calib.py OUT.json RULE [RULE ...] [--jobs 7] [--today 2026-11-21]

Each task runs backtest.run() for one ticker and horizon with backtest.clustered_zones replaced by
harness.zones() under the rule ("base" is the current rule exactly), 200,000 paths, sim method 2.
"""
import argparse, json, multiprocessing, os, sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import backtest  # noqa: E402
import harness  # noqa: E402

TICKERS = ("NVDA", "AAPL", "MSFT", "SPY", "JPM", "XOM", "PETR4.SA", "VALE3.SA", "ITUB4.SA", "BBDC4.SA",
           "ABEV3.SA", "WEGE3.SA")


def task(args):
    name, horizon, ticker, today = args
    rule = harness.rule_of(name)
    if name != "base":
        backtest.clustered_zones = lambda facts, s=3, r=3, places=2: harness.zones(facts, rule, s, r, places)
    report = backtest.run([ticker], horizon, today=date.fromisoformat(today), progress=False)
    return name, horizon, report["records"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("rules", nargs="+")
    p.add_argument("--jobs", type=int, default=7)
    p.add_argument("--today", default="2026-11-21")
    a = p.parse_args()
    tasks = [(n, h, t, a.today) for n in a.rules for h in backtest.HORIZONS for t in TICKERS]
    records = {}
    with ProcessPoolExecutor(a.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        for name, horizon, recs in pool.map(task, tasks):
            records.setdefault(f"{name}|{horizon}", []).extend(recs)
    out = {}
    for key, recs in records.items():
        s = backtest.summarise(recs)
        kinds = s["overall"]["kinds"]
        out[key] = {"cases": s["cases"], "skipped": s["skipped"],
                    **{f"{k}_{f}": kinds[k][f] for k in ("reach", "turn", "scenario") for f in ("brier", "brier_base_rate", "n")}}
        print(key, json.dumps(out[key]))
    with open(a.out, "w") as handle:
        json.dump(out, handle, indent=1)


if __name__ == "__main__":
    main()
