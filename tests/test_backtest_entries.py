"""backtest_entries.py: the trade setups played on later candles. Offline: the walk runs on
hand-made candles with known answers, and the whole run on backtest.py's synthetic random walk
with Yahoo stubbed."""
import csv
import json
from datetime import date

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
