"""backtest_entries.py: the trade setups played on later candles. Offline: the walk runs on
hand-made candles with known answers, and the whole run on backtest.py's synthetic random walk
with Yahoo stubbed."""
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

import backtest as bt
import backtest_entries as be
import mechanical
import trade_entries
from test_backtest import DATA, ROWS, TODAY

LONG_LIMIT = {"side": "long", "order": "limit", "entry": 100.0, "stop": 95.0, "target": 110.0, "risk": 5.0}
SHORT_STOP = {"side": "short", "order": "stop", "entry": 100.0, "stop": 105.0, "target": 90.0, "risk": 5.0}
LONG_STOP = {"side": "long", "order": "stop", "entry": 100.0, "stop": 95.0, "target": 110.0, "risk": 5.0}
SHORT_LIMIT = {"side": "short", "order": "limit", "entry": 100.0, "stop": 105.0, "target": 90.0, "risk": 5.0}
LONG_MARKET = {"side": "long", "order": "market", "entry": 100.0, "stop": 95.0, "target": 110.0, "risk": 5.0}


def c(day, open_, high, low, close):
    return [f"2026-01-{day:02d}", open_, high, low, close]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("test tried to download prices")
    monkeypatch.setattr(bt, "download", refuse)


# ---- the walk: fills ----------------------------------------------------------------------------

def test_never_filled_is_zero_r():
    out = be.walk(LONG_LIMIT, [c(5, 102, 104, 101, 103), c(6, 103, 109, 100.5, 108)])
    assert not out["filled"] and out["exit"] is None and out["r"] == 0.0 and not out["won"]


def test_limit_long_fills_on_touch_then_target():
    out = be.walk(LONG_LIMIT, [c(5, 102, 103, 100, 101), c(6, 101, 111, 100.5, 110.5)])
    assert out["filled"] and out["fill_date"] == "2026-01-05" and out["fill_price"] == 100
    assert out["touch_only"] and not out["gap_fill"]
    assert (out["exit"], out["exit_date"], out["exit_price"], out["r"], out["won"]) == ("target", "2026-01-06", 110, 2.0, True)


def test_limit_long_stop_first():
    out = be.walk(LONG_LIMIT, [c(5, 102, 103, 99, 101), c(6, 101, 102, 94, 96)])
    assert (out["exit"], out["exit_price"], out["r"], out["won"]) == ("stop", 95, -1.0, False)


def test_limit_fill_on_a_gap_fills_at_the_open():
    out = be.walk(LONG_LIMIT, [c(5, 102, 103, 101, 102), c(6, 98, 99, 97, 98), c(7, 98, 111, 98, 110)])
    assert out["fill_price"] == 98 and out["gap_fill"]
    assert out["exit"] == "target" and out["r"] == pytest.approx(12 / 5)


def test_stop_order_short_fills_at_the_open_on_a_gap_down():
    out = be.walk(SHORT_STOP, [c(5, 101, 102, 100.5, 101), c(6, 99, 99.5, 89, 89.5)])
    assert out["fill_price"] == 99 and out["gap_fill"]
    assert out["exit"] == "target" and out["r"] == pytest.approx(9 / 5)   # the gap cost 1 of the 10


def test_stop_order_long_and_short_limit_fill_on_the_way_up():
    up = [c(5, 98, 100.5, 97.5, 100), c(6, 100, 111, 99.5, 110)]
    assert be.walk(LONG_STOP, up)["fill_price"] == 100 and be.walk(LONG_STOP, up)["exit"] == "target"
    short = be.walk(SHORT_LIMIT, up)
    assert short["filled"] and short["fill_date"] == "2026-01-05" and short["exit"] == "stop" and short["r"] == -1.0


def test_market_order_fills_at_the_next_open():
    out = be.walk(LONG_MARKET, [c(5, 101, 102, 100, 101.5), c(6, 101.5, 102, 101, 101)])
    assert out["fill_price"] == 101 and out["fill_date"] == "2026-01-05"
    assert out["exit"] == "expiry" and out["exit_price"] == 101 and out["r"] == 0.0


# ---- the walk: exits ------------------------------------------------------------------------------

def test_gap_through_the_stop_loses_more_than_one_r():
    out = be.walk(LONG_LIMIT, [c(5, 101, 101.5, 99.5, 100), c(6, 92, 93, 90, 91)])
    assert (out["exit"], out["exit_price"], out["r"]) == ("stop", 92, -1.6)


def test_gap_through_the_target_closes_at_the_open():
    out = be.walk(LONG_LIMIT, [c(5, 101, 101.5, 99.5, 100), c(6, 112, 113, 111, 112)])
    assert (out["exit"], out["exit_price"], out["r"], out["won"]) == ("target", 112, 2.4, True)


def test_expiry_uses_the_last_close():
    out = be.walk(SHORT_LIMIT, [c(5, 99, 100.2, 98, 99), c(6, 99, 99.5, 97, 97.5)])
    assert out["exit"] == "expiry" and out["exit_date"] == "2026-01-06" and out["r"] == pytest.approx(0.5)


def test_same_candle_stop_and_target_is_pessimistic_unless_optimistic():
    candles = [c(5, 101, 101.5, 99.5, 100), c(6, 100, 111, 94, 100)]
    assert be.walk(LONG_LIMIT, candles)["exit"] == "stop"
    assert be.walk(LONG_LIMIT, candles, optimistic=True)["exit"] == "target"


# ---- the walk: the fill candle ---------------------------------------------------------------------

def test_fill_candle_stop_beyond_the_entry_is_certain():
    # opens above the entry, so the low (past the stop) comes after the fill whatever the ordering
    candles = [c(5, 102, 103, 94, 96)]
    for optimistic in (False, True):
        out = be.walk(LONG_LIMIT, candles, optimistic)
        assert out["exit"] == "stop" and out["r"] == -1.0


def test_fill_candle_target_may_have_come_first():
    # a long limit: opens above, the high (at the target) may come before the dip to the entry
    candles = [c(5, 102, 111, 99, 101), c(6, 101, 102, 100.5, 101)]
    pessimistic, optimistic = be.walk(LONG_LIMIT, candles), be.walk(LONG_LIMIT, candles, True)
    assert pessimistic["exit"] == "expiry" and pessimistic["r"] == pytest.approx(0.2)
    assert optimistic["exit"] == "target" and optimistic["r"] == 2.0


def test_fill_candle_stop_may_have_come_first():
    # a long stop order: opens below, the low (at the stop) may come before the rise to the entry
    candles = [c(5, 98, 101, 94, 100.5), c(6, 100.5, 102, 100, 101)]
    pessimistic, optimistic = be.walk(LONG_STOP, candles), be.walk(LONG_STOP, candles, True)
    assert pessimistic["exit"] == "stop" and pessimistic["r"] == -1.0
    assert optimistic["exit"] == "expiry" and optimistic["r"] == pytest.approx(0.2)


def test_fill_candle_target_beyond_the_entry_is_certain():
    out = be.walk(LONG_STOP, [c(5, 98, 111, 97, 110)])
    assert out["exit"] == "target" and be.walk(LONG_STOP, [c(5, 98, 111, 97, 110)], True)["exit"] == "target"


def test_fill_candle_with_everything_is_ambiguous():
    candles = [c(5, 98, 111, 94, 100)]
    assert be.walk(LONG_STOP, candles)["exit"] == "stop"
    assert be.walk(LONG_STOP, candles, True)["exit"] == "target"


# ---- one case and the run ---------------------------------------------------------------------------

@pytest.fixture
def yahoo(monkeypatch):
    def fake(ticker, start):
        return {**DATA, "rows": [r for r in ROWS if r[0] >= start.isoformat()]}
    monkeypatch.setattr(bt, "download", fake)


def test_a_case_uses_the_live_runs_code_and_only_later_candles():
    day = date(2025, 3, 12)
    record = be.run_case("AAA", "1-2w", DATA, day, TODAY)
    d = bt.input_as_of("AAA", "1-2w", DATA, day)
    reply = trade_entries.build(d, mechanical.build(d))
    assert [s["id"] for s in record["setups"]] == [e["id"] for e in reply["entries"]]
    assert record["valid_until"] == reply["valid_until"]
    for s, e in zip(record["setups"], reply["entries"]):
        assert (s["entry"], s["stop"], s["target"], s["fill"], s["win_if_filled"], s["per_order"]) == (
            e["entry"]["price"], e["stop"]["price"], e["target"]["price"], e["odds"]["fill"],
            e["odds"]["win_if_filled"], e["edge"]["per_order"])
        if s["filled"]:
            assert day.isoformat() < s["fill_date"] <= record["valid_until"]
            assert s["exit_date"] <= record["valid_until"]
        assert s["ambiguous"] == ((s["exit"], s["r"]) != (s["exit_other"], s["r_other"]))


def test_unended_horizon_is_skipped():
    record = be.run_case("AAA", "1-3m", DATA, date(2026, 11, 2), TODAY)
    assert record["skipped"] == "horizon not ended yet"


def test_method_reaches_the_projection(monkeypatch):
    seen = []
    real = mechanical.build
    monkeypatch.setattr(mechanical, "build", lambda d, method=None: seen.append(method) or real(d, method=method))
    be.run_case("AAA", "1-2w", DATA, date(2025, 3, 12), TODAY, method=1)
    assert seen == [1]


def test_mechanical_build_default_is_unchanged():
    d = bt.input_as_of("AAA", "1-2w", DATA, date(2025, 3, 12))
    assert mechanical.build(d) == mechanical.build(d, method=None) == mechanical.build(d, method=bt.rz.SIM_METHOD)


@pytest.fixture
def report(yahoo):
    return be.run(["aaa"], ["1-2w", "1-3m"], start=date(2025, 1, 6), end=date(2025, 6, 30), today=TODAY,
                  progress=False)


def test_report_shape(report):
    s, summary = report["settings"], report["summary"]
    assert s["horizons"] == ["1-2w", "1-3m"] and s["tickers"] == ["AAA"] and s["ordering"] == "pessimistic"
    assert summary["cases"] > 0 and summary["setups"] <= 3 * summary["cases"]
    for horizon, scope in summary["per_horizon"].items():
        o = scope["overall"]
        assert o["setups"] == sum(k["setups"] for k in scope["by_rank"].values())
        assert o["setups"] == sum(k["setups"] for k in scope["by_side"].values())
        assert set(scope["by_kind"]) <= {"reversal", "continuation"}
        assert set(scope["by_order"]) <= {"limit", "stop", "market"}
        assert sum(scope["exits"].values()) == o["setups"]
        assert o["fills"] == o["setups"] - scope["exits"]["unfilled"]
        assert 0 <= o["fill_observed"] <= 1 and "brier_fill" in o
        lo, hi = scope["bootstrap_90"]["r_per_order"]
        assert lo <= hi
    rows = be.flat(report["records"])
    assert all(r["horizon"] in ("1-2w", "1-3m") for r in rows)
    one = summary["per_horizon"]["1-2w"]["overall"]
    mine = [r for r in rows if r["horizon"] == "1-2w"]
    assert one["r_per_order"] == pytest.approx(sum(r["r"] for r in mine) / len(mine), abs=1e-4)


def test_run_is_reproducible(yahoo, report):
    again = be.run(["aaa"], ["1-2w", "1-3m"], start=date(2025, 1, 6), end=date(2025, 6, 30), today=TODAY,
                   progress=False)
    assert again == report


def test_text_report_states_rules_and_caveats(report):
    text = be.render(report)
    for needle in ("HORIZON 1-2w", "HORIZON 1-3m", "not advice", "survivors", "pessimistic",
                   "No commissions", "overlap", "by quality", "by rank", "Bootstrap", "Reliability"):
        assert needle in text


def test_cli_json_and_csv(yahoo, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(be.sp, "today_in", lambda tz: TODAY)
    path = tmp_path / "setups.csv"
    code = be.main(["AAA", "--horizon", "1-2w", "--start", "2025-01-06", "--end", "2025-04-30", "--json",
                    "--csv", str(path), "--quiet", "--optimistic"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["ordering"] == "optimistic" and "per_horizon" in out
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == out["setups"] and rows[0].keys() >= {"r", "won", "ambiguous", "fill", "win_if_filled"}


def test_cli_text(yahoo, monkeypatch, capsys):
    monkeypatch.setattr(be.sp, "today_in", lambda tz: TODAY)
    assert be.main(["AAA", "--horizon", "1-2w", "--start", "2025-01-06", "--end", "2025-03-31", "--quiet"]) == 0
    assert "BACKTEST of the trade setups" in capsys.readouterr().out


def test_cli_bad_step(capsys):
    with pytest.raises(SystemExit):
        be.main(["AAA", "--step", "0"])


# ---- the long run: universe file, history, periods, regimes, parallel ------------------------------

UNIVERSE = """# a comment line
version: 7

[first] The first group, for a reason.
aaa BBB  # trailing comment
[second-2] Another reason.
BBB CCC@..2025-06-30 DDD@2020-01-01..
"""


def write_universe(tmp_path, text=UNIVERSE):
    path = tmp_path / "universe.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_universe_file_groups_version_and_ranges(tmp_path):
    u = be.read_universe(write_universe(tmp_path))
    assert u["version"] == "7" and list(u["groups"]) == ["first", "second-2"]
    assert u["groups"]["first"] == {"reason": "The first group, for a reason.", "tickers": ["AAA", "BBB"]}
    assert u["groups"]["second-2"]["tickers"] == ["BBB", "CCC", "DDD"]
    assert u["tickers"] == ["AAA", "BBB", "CCC", "DDD"]
    assert u["spans"] == {"CCC": (None, date(2025, 6, 30)), "DDD": (date(2020, 1, 1), None)}
    plain = be.read_universe(write_universe(tmp_path, "version: 1\n[g] why\nEEE@2024-01-31\n"))
    assert plain["spans"] == {"EEE": (None, date(2024, 1, 31))}   # a bare @date is the last day


@pytest.mark.parametrize("text, needle", [
    ("[g] why\nAAA\n", "no 'version"),
    ("version: 1\nAAA\n[g] why\n", "before the first"),
    ("version: 1\n[g] why\n[g] again\nAAA\n", "appears twice"),
    ("version: 1\n[g]\nAAA\n", "needs a reason"),
    ("version: 1\n[g] why\n[h] empty\n", "has no tickers"),
    ("version: 1\n[g] why\nAAA@2025-13-01\n", "not a date"),
    ("version: 1\n[g] why\nAAA@2025-02-01..2025-01-01\n", "not a FIRST..LAST"),
    ("version: 1\n[g] why\nAAA@..\n", "not a FIRST..LAST"),
    ("version: 1\n[g] why\nAAA@..2025-01-01\n[h] why\nAAA@..2025-02-01\n", "two different"),
    ("version: 1\n[G] why\nAAA\n", "group name"),
    ("version: 1\n[g] why\nAA,A\n", "not a ticker"),
    ("version: 1\nversion: 2\n[g] why\nAAA\n", "second version"),
])
def test_universe_file_errors(tmp_path, text, needle):
    with pytest.raises(be.UniverseError) as error:
        be.read_universe(write_universe(tmp_path, text))
    assert needle in str(error.value)


def test_universe_file_missing(tmp_path):
    with pytest.raises(be.UniverseError, match="cannot read"):
        be.read_universe(str(tmp_path / "nowhere.txt"))


def test_the_repositorys_universe_file():
    u = be.read_universe(Path(be.__file__).resolve().parent / "backtest_universe.txt")
    assert u["groups"]["original"]["tickers"] == ["AAPL", "MSFT", "NVDA", "SPY", "JPM", "XOM", "KO", "PETR4.SA",
                                                  "VALE3.SA", "ITUB4.SA", "BBDC4.SA", "WEGE3.SA"]
    assert 40 <= len(u["tickers"]) <= 80
    assert all(g["reason"] for g in u["groups"].values())
    assert any(t.endswith(".SA") for t in u["tickers"]) and any("." not in t for t in u["tickers"])
    for ticker in set(u["spans"]) & set(u["groups"]["original"]["tickers"]):
        first, last = u["spans"][ticker]   # a cut of the twelve may not reach the earlier reports' rows (2012 on)
        assert last is None and first < date(2012, 1, 1)


def test_history_start_is_the_longest_series_a_live_run_requests():
    assert be.longest_period("1-2w") == "2y" and be.longest_period("1-3m") == "5y"
    assert be.longest_period("6-12m") == "10y"
    assert be.history_start("6-12m", date(2025, 3, 12)) == date(2015, 3, 12)


@pytest.mark.parametrize("horizon, day, enough", [
    ("6-12m", date(2025, 3, 12), True), ("6-12m", date(2024, 6, 3), False),
    ("1-3m", date(2020, 1, 6), True), ("1-3m", date(2019, 12, 2), False),
    ("1-2w", date(2017, 1, 3), True), ("1-2w", date(2016, 12, 1), False),
])
def test_dates_without_enough_history_are_skipped(horizon, day, enough):
    record = be.run_case("AAA", horizon, DATA, day, TODAY)        # ROWS start on 2015-01-01
    skipped = record.get("skipped", "")
    assert skipped.startswith("less history than a live run requests") != enough
    if not enough:
        assert be.longest_period(horizon) in skipped


def test_a_ranges_first_day_counts_as_the_start_of_history():
    late = be.cut(DATA, date(2024, 1, 1))
    assert late["rows"][0][0] >= "2024-01-01" and late["rows"][-1] == DATA["rows"][-1]
    assert be.run_case("AAA", "1-2w", late, date(2025, 3, 12), TODAY)["skipped"].startswith("less history")
    assert "skipped" not in be.run_case("AAA", "1-2w", late, date(2026, 2, 2), TODAY)


def test_a_ranges_last_day_ends_the_walk():
    last = date(2025, 3, 20)
    record = be.run_case("AAA", "1-3m", be.cut(DATA, None, last), date(2025, 3, 12), TODAY)
    assert record["valid_until"] > last.isoformat() and record["candles"] <= 6
    for s in record["setups"]:
        assert s["exit_date"] is None or s["exit_date"] <= last.isoformat()


def test_run_applies_the_ranges(yahoo):
    report = be.run(["aaa"], ["1-2w"], start=date(2025, 1, 6), end=date(2025, 6, 30), today=TODAY, progress=False,
                    spans={"AAA": (None, date(2025, 3, 31))})
    assert max(r["as_of"] for r in report["records"]) <= "2025-03-31"
    assert report["settings"]["spans"] == {"AAA": [None, "2025-03-31"]}


@pytest.mark.parametrize("as_of, valid_until, part", [
    ("2008-03-03", "2008-06-02", "held-out"), ("2008-11-03", "2009-10-30", "held-out"),
    ("2009-11-02", "2010-01-29", "embargo"), ("2013-12-02", "2014-02-28", "embargo"),
    ("2012-01-03", "2012-12-31", "fit"), ("2016-06-01", "2016-06-15", "held-out"),
    ("2016-12-01", "2017-01-31", "embargo"), ("2024-01-02", "2024-03-29", "fit"),
])
def test_fit_and_held_out_parts(as_of, valid_until, part):
    assert be.part_of(as_of, valid_until) == part


def test_index_of_a_ticker():
    assert be.index_of("PETR4.SA") == "^BVSP" and be.index_of("aapl") == "^GSPC" and be.index_of("SPY") == "^GSPC"


@pytest.fixture
def periods_report(yahoo):
    return be.run(["aaa", "bbb.sa"], ["1-2w"], start=date(2024, 12, 9), end=date(2025, 1, 31), today=TODAY,
                  progress=False, regimes=True,
                  view={"periods": be.PERIODS, "groups": {"us": ["AAA"], "br": ["BBB.SA"]}, "subsets": ["br"]})


def test_period_tables_add_up(periods_report):
    c = periods_report["summary"]["per_horizon"]["1-2w"]
    total = c["overall"]["setups"]
    assert set(c["by_year"]) == {"2024", "2025"}
    for table in ("by_year", "by_regime", "by_market_after", "by_part"):
        assert sum(row["all"]["setups"] for row in c[table].values()) == total
        for row in c[table].values():
            assert row["long"]["setups"] + row["short"]["setups"] == row["all"]["setups"]
            assert row["short continuation"]["setups"] <= row["short"]["setups"]
    assert set(c["by_regime"]) <= set(be.REGIMES) and set(c["by_part"]) <= set(be.PARTS)
    assert c["by_group"]["all"]["all"] == c["overall"]
    assert c["by_group"]["us"]["all"]["setups"] + c["by_group"]["br"]["all"]["setups"] == total
    sub = periods_report["summary"]["subsets"]["br"]
    assert sub["tickers"] == ["BBB.SA"] and sub["per_horizon"]["1-2w"]["overall"] == c["by_group"]["br"]["all"]


def test_regime_labels_come_from_the_index_before_and_after(periods_report):
    closes = {row[0]: row[4] for row in ROWS}          # the stub serves the same walk for ^GSPC and ^BVSP
    dates = sorted(closes)

    def on(day):
        return closes[[d for d in dates if d <= day][-1]]

    for r in periods_report["records"]:
        if "skipped" in r:
            continue
        back = (date.fromisoformat(r["as_of"]) - timedelta(days=be.TRAILING_DAYS)).isoformat()
        trailing = on(r["as_of"]) / on(back) - 1
        assert r["index_trailing"] == round(trailing, 4)
        assert r["regime"] == ("bull" if trailing >= 0.1 else "bear" if trailing <= -0.1 else "flat")
        assert r["market_after"] == ("index up" if on(r["valid_until"]) >= on(r["as_of"]) else "index down")
    assert periods_report["settings"]["regimes"]["index"] == {"*.SA": "^BVSP", "other": "^GSPC"}


def test_regime_index_that_fails_to_download_leaves_unknown(monkeypatch):
    def fake(ticker, start):
        if ticker.startswith("^"):
            raise bt.BacktestError("no index today")
        return {**DATA, "rows": [r for r in ROWS if r[0] >= start.isoformat()]}
    monkeypatch.setattr(bt, "download", fake)
    report = be.run(["aaa"], ["1-2w"], start=date(2025, 1, 6), end=date(2025, 2, 28), today=TODAY, progress=False,
                    regimes=True, view={"periods": ["regime"]})
    assert {r["regime"] for r in report["records"] if "skipped" not in r} == {"unknown"}
    assert "^GSPC (regime index)" in report["settings"]["failed"]


def test_period_text_and_part_filter(periods_report, yahoo):
    text = be.render(periods_report)
    for needle in ("by year of the as-of date", "by regime", "index's move over the horizon", "by part of the fixed",
                   "by universe group", "SUBSET [br]", "Fit / held-out split", "Regime: the index's return"):
        assert needle in text
    only = be.run(["aaa"], ["1-2w"], start=date(2024, 12, 9), end=date(2025, 1, 31), today=TODAY, progress=False,
                  view={"part": "fit"})
    assert only["summary"]["cases"] == sum(1 for r in only["records"] if r.get("part") == "fit")
    assert "Only the fit part is reported" in be.render(only)


def test_bootstrap_is_the_original_computation(report):
    """The packed bootstrap must give exactly what resampling the setups themselves gave."""
    def original(setups, rounds=200, seed=be.BOOTSTRAP_SEED):
        cases = {}
        for s in setups:
            cases.setdefault((s["ticker"], s["as_of"]), []).append(s)
        groups = list(cases.values())
        rng = random.Random(seed)
        draws = {"fill_observed": [], "win_observed": [], "r_per_fill": [], "r_per_order": []}
        for _ in range(rounds):
            sample = [s for _ in groups for s in rng.choice(groups)]
            filled = [s for s in sample if s["filled"]]
            draws["fill_observed"].append(sum(s["filled"] for s in sample) / len(sample))
            draws["r_per_order"].append(sum(s["r"] for s in sample) / len(sample))
            if filled:
                draws["win_observed"].append(sum(s["won"] for s in filled) / len(filled))
                draws["r_per_fill"].append(sum(s["r"] for s in filled) / len(filled))
        return {key: [round(sorted(v)[int(0.05 * (len(v) - 1))], 4), round(sorted(v)[int(0.95 * (len(v) - 1))], 4)]
                for key, v in draws.items() if v}

    rows = be.flat(report["records"])
    assert be.bootstrap(rows, rounds=200) == original(rows)


def test_parallel_run_is_identical_to_serial(yahoo):
    kwargs = dict(start=date(2025, 1, 6), end=date(2025, 3, 14), today=TODAY, progress=False,
                  view={"periods": ["year", "part"]})
    serial = be.run(["aaa", "bbb"], ["1-2w", "1-3m"], jobs=1, **kwargs)
    parallel = be.run(["aaa", "bbb"], ["1-2w", "1-3m"], jobs=3, **kwargs)
    assert parallel == serial
    assert be.render(parallel) == be.render(serial)
    assert json.dumps(parallel["summary"]) == json.dumps(serial["summary"])


def test_chunks_keep_the_order(yahoo, monkeypatch):
    monkeypatch.setattr(be, "CHUNK", 3)
    small = be.run(["aaa"], ["1-2w"], start=date(2025, 1, 6), end=date(2025, 3, 31), today=TODAY, progress=False)
    monkeypatch.setattr(be, "CHUNK", 1000)
    big = be.run(["aaa"], ["1-2w"], start=date(2025, 1, 6), end=date(2025, 3, 31), today=TODAY, progress=False)
    assert small == big


def test_cli_universe_groups_subsets_and_periods(yahoo, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(be.sp, "today_in", lambda tz: TODAY)
    path = write_universe(tmp_path)
    code = be.main(["--universe", path, "--group", "first", "--subset", "first", "--horizon", "1-2w",
                    "--start", "2025-01-06", "--end", "2025-02-28", "--by-year", "--by-part", "--quiet", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["tickers"] == ["AAA", "BBB"]
    assert out["settings"]["universe"]["version"] == "7" and "spans" not in out["settings"]
    assert set(out["per_horizon"]["1-2w"]["by_group"]) == {"all", "first"}
    assert "by_year" in out["per_horizon"]["1-2w"] and "by_regime" not in out["per_horizon"]["1-2w"]
    assert out["subsets"]["first"]["tickers"] == ["AAA", "BBB"]
    assert be.main(["--universe", path, "--horizon", "1-2w", "--start", "2025-01-06", "--end", "2025-02-28",
                    "--quiet", "--json"]) == 0
    whole = json.loads(capsys.readouterr().out)["settings"]
    assert whole["tickers"] == ["AAA", "BBB", "CCC", "DDD"]
    assert whole["spans"] == {"CCC": [None, "2025-06-30"], "DDD": ["2020-01-01", None]}


@pytest.mark.parametrize("argv, needle", [
    (["AAA", "--jobs", "0"], "--jobs"),
    (["AAA", "--subset", "x"], "need --universe"),
    ([], "at least one TICKER"),
])
def test_cli_bad_long_run_options(argv, needle, capsys):
    with pytest.raises(SystemExit):
        be.main(argv)
    assert needle in capsys.readouterr().err


def test_cli_unknown_group_and_bad_file(tmp_path, capsys):
    path = write_universe(tmp_path)
    with pytest.raises(SystemExit):
        be.main(["--universe", path, "--group", "nope"])
    assert "no such group" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        be.main(["--universe", path, "--group", "first", "--subset", "second-2"])
    assert "--subset must name a group" in capsys.readouterr().err
    assert be.main(["--universe", str(tmp_path / "missing.txt")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_a_date_whose_series_would_start_at_a_missing_local_midnight_is_skipped():
    # Sao Paulo skipped from 00:00 to 01:00 on 18 Oct 2015; the 2y series of 18 Oct 2017 starts that day
    sao_paulo = {**DATA, "tz": "America/Sao_Paulo"}
    assert "DST" in be.run_case("AAA.SA", "1-2w", sao_paulo, date(2017, 10, 18), TODAY)["skipped"]
    assert "skipped" not in be.run_case("AAA.SA", "1-2w", sao_paulo, date(2017, 10, 19), TODAY)


def test_stale_quotes():
    flat_row = ["2026-01-05", 10.0, 10.0, 10.0, 10.0]
    moving = ["2026-01-06", 10.0, 10.5, 9.8, 10.2]
    assert not be.stale([]) and not be.stale([moving] * 8 + [flat_row] * 2)
    assert be.stale([moving] * 7 + [flat_row] * 3)


def test_cases_on_stale_quotes_are_skipped():
    day = date(2025, 3, 12)
    before = [r if r[0] < "2025-02-01" or r[0] > day.isoformat() else [r[0], r[4], r[4], r[4], r[4]] for r in ROWS]
    assert be.run_case("AAA", "1-2w", {**DATA, "rows": before}, day, TODAY)["skipped"] == be.STALE_REASON
    after = [r if not ("2025-03-13" <= r[0] <= "2025-03-24") else [r[0], r[4], r[4], r[4], r[4]] for r in ROWS]
    assert be.run_case("AAA", "1-2w", {**DATA, "rows": after}, day, TODAY)["skipped"] == be.STALE_REASON
    assert "skipped" not in be.run_case("AAA", "1-2w", DATA, day, TODAY)


def test_cli_help(capsys):
    """Every help string formats (argparse reads % as a format character)."""
    with pytest.raises(SystemExit) as stop:
        be.main(["--help"])
    assert stop.value.code == 0 and "--by-regime" in capsys.readouterr().out
