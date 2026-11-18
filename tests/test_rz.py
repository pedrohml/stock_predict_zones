"""Step 2 helper, scripts/rz.py: levels, schedule, sim and check."""
import copy
from datetime import date

import pytest

SMALL = 4_000   # paths for tests that only look at the structure of the output


# ---- load / levels -----------------------------------------------------------

def test_load_picks_the_series_named_by_close_basis(rz, input_path, input_data):
    d, main, starts, high, low, close = rz.load(str(input_path))
    assert d == input_data
    assert main["candles"] == "1W"
    assert len(starts) == len(high) == len(low) == len(close) == 104
    assert starts[-1] == "2026-11-16"
    assert close[-1] == 44.06
    assert (high >= low).all()


def test_levels_reports_the_facts_of_the_main_series(rz, input_path, input_data, printed):
    rz.levels(str(input_path))
    out = printed()
    assert list(out) == ["price", "candles", "rows", "last_row", "trend", "swing", "retraced",
                         "from_high", "retracements", "volatility", "typical_zone_width",
                         "pivot_lows", "pivot_highs", "recent_lows", "recent_highs",
                         "gap_candidates"]
    assert (out["price"], out["candles"], out["rows"], out["last_row"]) == (44.06, "1W", 104, "forming")
    assert out["trend"] in ("up", "down", "sideways")

    rows = input_data["data"]["series"][-1]["rows"]
    top = max(rows, key=lambda row: row[2])
    bottom = min(rows[:rows.index(top) + 1], key=lambda row: row[3])
    assert out["swing"] == {"low": [bottom[3], bottom[0]], "high": [top[2], top[0]]}
    assert out["from_high"] == round(44.06 / top[2] - 1, 3)
    assert out["retraced"] == round((top[2] - 44.06) / (top[2] - bottom[3]), 2)

    # rally then pullback: retracements step down from the high towards the low
    levels = [out["retracements"][key] for key in ("0.382", "0.5", "0.618", "0.786")]
    assert levels == sorted(levels, reverse=True)
    assert bottom[3] < levels[-1] and levels[0] < top[2]

    starts = [row[0] for row in rows]
    assert [x[0] for x in out["recent_lows"]] == starts[-3:]
    assert [x[0] for x in out["recent_highs"]] == starts[-3:]
    for stamp, _ in out["pivot_lows"] + out["pivot_highs"]:
        assert stamp in starts
    low_width, high_width = out["typical_zone_width"]
    assert 0 < low_width < high_width
    assert set(out["volatility"]) == {"last_8", "last_52", "assumed"}
    assert len(out["volatility"]["assumed"]) == 2


def test_levels_measures_a_rebound_from_the_low(rz, input_data, write_json, printed):
    """Decline then rebound: the low comes after the high, levels step up."""
    data = copy.deepcopy(input_data)
    rows = data["data"]["series"][-1]["rows"]
    prices = [row[1:] for row in reversed(rows)]
    for row, (open_, high, low, close) in zip(rows, prices):
        row[1:] = [close, high, low, open_]
    data["now"]["price"] = rows[-1][4]
    rz.levels(write_json("mirrored.json", data))
    out = printed()
    assert out["swing"]["high"][1] < out["swing"]["low"][1]
    assert list(out["swing"]) == ["high", "low"]
    levels = [out["retracements"][key] for key in ("0.382", "0.5", "0.618", "0.786")]
    assert levels == sorted(levels)
    assert out["retraced"] == round((out["price"] - out["swing"]["low"][0])
                                    / (out["swing"]["high"][0] - out["swing"]["low"][0]), 2)


def test_levels_finds_gaps(rz, input_data, write_json, printed):
    data = copy.deepcopy(input_data)
    rows = data["data"]["series"][-1]["rows"]
    rows[-2][1:] = [60.0, 61.0, 59.0, 60.0]     # gap up from the row before, gap down after
    rz.levels(write_json("gaps.json", data))
    gaps = printed()["gap_candidates"]
    assert [rows[-1][0], rows[-1][2], 59.0] in gaps          # [date, high after, low before]
    assert [rows[-2][0], rows[-3][2], 59.0] in gaps          # [date, high before, low after]


# ---- schedule ----------------------------------------------------------------

def schedule(rz, basis, horizon, last_row, now="2026-11-17T17:36-03:00", session="after_hours", starts=()):
    d = {"now": {"time": now, "session": session}, "horizon": horizon}
    return rz.schedule(d, {"candles": basis, "last_row": last_row}, list(starts))


def test_schedule_weekly_forming(rz):
    dates, share = schedule(rz, "1W", "1-3m", "forming")
    assert len(dates) == 13
    assert (dates[0], dates[-1]) == (date(2026, 11, 20), date(2027, 2, 12))
    assert all(day.weekday() == 4 for day in dates)
    assert share == pytest.approx(0.6)      # Tuesday done: three of five sessions left


def test_schedule_weekly_forming_share_depends_on_the_session(rz):
    assert schedule(rz, "1W", "1-3m", "forming", session="open")[1] == pytest.approx(0.7)
    assert schedule(rz, "1W", "1-3m", "forming", session="pre_market")[1] == pytest.approx(0.8)
    late = schedule(rz, "1W", "1-3m", "forming", now="2026-11-20T16:00-03:00", session="open")
    assert late[0][0] == date(2026, 11, 20)
    assert late[1] == pytest.approx(0.1)


def test_schedule_weekly_closed_starts_with_the_next_friday(rz):
    dates, share = schedule(rz, "1W", "1-3m", "closed", now="2026-11-20T18:30-03:00", session="closed")
    assert dates[0] == date(2026, 11, 27)
    assert (len(dates), share) == (13, 1.0)
    weekend = schedule(rz, "1W", "1-3m", "closed", now="2026-11-21T12:00-03:00", session="closed")
    assert weekend[0][0] == date(2026, 11, 27)


def test_schedule_daily_skips_weekends(rz):
    dates, share = schedule(rz, "1D", "1-2w", "closed")
    assert len(dates) == 10
    assert (dates[0], dates[-1]) == (date(2026, 11, 18), date(2026, 12, 1))
    assert all(day.weekday() < 5 for day in dates)
    assert share == 1.0
    dates, share = schedule(rz, "1D", "1-2w", "forming", session="open")
    assert (dates[0], len(dates), share) == (date(2026, 11, 17), 10, 0.5)


def test_schedule_monthly_uses_month_ends(rz):
    dates, share = schedule(rz, "1M", "6-12m", "forming")
    assert len(dates) == 12
    assert dates[:4] == [date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28)]
    assert dates[-1] == date(2027, 10, 31)
    assert share == pytest.approx(1 - 17 / 30)
    dates, share = schedule(rz, "1M", "6-12m", "closed", now="2026-12-01T09:00-03:00")
    assert (dates[0], dates[-1], share) == (date(2027, 1, 31), date(2027, 12, 31), 1.0)


def test_schedule_intraday_covers_the_rest_of_the_session(rz):
    yesterday = [f"2026-11-16T{10 + i // 4:02d}:{15 * (i % 4):02d}" for i in range(28)]
    today = [f"2026-11-17T{10 + i // 4:02d}:{15 * (i % 4):02d}" for i in range(10)]
    dates, share = schedule(rz, "15m", "today", "forming", now="2026-11-17T12:20-03:00",
                            session="open", starts=yesterday + today)
    assert dates == [date(2026, 11, 17)] * 18
    assert share == 0.5
    dates, share = schedule(rz, "15m", "today", "closed", now="2026-11-17T19:00-03:00",
                            session="closed", starts=yesterday + today)
    assert (len(dates), share) == (28, 1.0)


# ---- sim ---------------------------------------------------------------------

def test_sim_output_structure(run_sim, zones):
    out = run_sim(zones, paths=SMALL)
    assert list(out) == ["horizon", "volatility", "first_candle_share_left", "measure",
                         "supports", "resistances", "scenarios"]
    assert out["horizon"] == {"label": "1-3m", "close_basis": "1W", "closes": 13,
                              "first": "2026-11-20", "last": "2027-02-12"}
    assert out["first_candle_share_left"] == 0.6
    assert out["measure"] == "close"
    assert [entry["id"] for entry in out["supports"]] == ["S1", "S2", "S3", "below"]
    assert [entry["id"] for entry in out["resistances"]] == ["R1", "R2", "R3", "above"]
    for side in ("supports", "resistances"):
        reach = [entry["reach"] for entry in out[side]]
        assert reach == sorted(reach, reverse=True)
        for entry in out[side]:
            assert set(entry) == {"id", "reach", "turn"}
            assert 0 <= entry["turn"] <= entry["reach"] <= 1
        assert out[side][-1]["turn"] == out[side][-1]["reach"]      # nothing beyond a tail
    assert out["supports"][0]["reach"] == 1.0      # the price starts inside S1
    assert out["resistances"][0]["reach"] < 1.0    # R1 still has to be reached


def test_sim_scenarios(run_sim, zones):
    scenarios = run_sim(zones, paths=SMALL)["scenarios"]
    assert [s["name"] for s in scenarios] == ["breakdown", "breakout", "range", "whipsaw"]
    assert sum(s["p"] for s in scenarios) == pytest.approx(1, abs=0.021)
    by_name = {s["name"]: s for s in scenarios}
    # thresholds are the far edges of the middle zones
    assert by_name["breakdown"]["low"] == "below 40.6" == by_name["whipsaw"]["low"]
    assert by_name["breakout"]["high"] == "above 49.6" == by_name["whipsaw"]["high"]
    assert by_name["range"]["low"] == "40.6 or higher" == by_name["breakout"]["low"]
    assert by_name["range"]["high"] == "49.6 or lower" == by_name["breakdown"]["high"]


def test_sim_scenarios_agree_with_the_middle_zone_reach(run_sim, zones):
    out = run_sim(zones, paths=SMALL)
    p = {s["name"]: s["p"] for s in out["scenarios"]}
    assert p["breakdown"] + p["whipsaw"] == pytest.approx(out["supports"][2]["reach"], abs=0.021)
    assert p["breakout"] + p["whipsaw"] == pytest.approx(out["resistances"][2]["reach"], abs=0.021)


def test_sim_is_deterministic_with_default_paths(rz, input_path, write_json, capsys, zones):
    """Fixed seed: the same input and zones must give the same baseline, byte for byte."""
    zones["shocks"] = [{"date": "2026-12-21", "drop": 1.19}, {"date": "2026-11-26", "jump": 0.05}]
    zones_path = write_json("zones.json", zones)
    capsys.readouterr()
    rz.sim(str(input_path), zones_path)
    first = capsys.readouterr().out
    rz.sim(str(input_path), zones_path)
    second = capsys.readouterr().out
    assert first == second
    assert first.count("\n") == 7


def test_sim_default_is_200_000_paths(rz):
    assert rz.sim.__defaults__ == (200_000,)


def test_sim_single_zone_per_side(run_sim):
    out = run_sim({"supports": [[43.2, 44.6]], "resistances": [[45.3, 47.1]]}, paths=SMALL)
    assert [entry["id"] for entry in out["supports"]] == ["S1", "below"]
    assert [entry["id"] for entry in out["resistances"]] == ["R1", "above"]
    assert out["scenarios"][0]["low"] == "below 43.2"
    assert out["scenarios"][1]["high"] == "above 47.1"


def test_sim_cash_drop_pushes_the_price_down(run_sim, zones):
    plain = run_sim(zones, paths=20_000)
    zones["shocks"] = [{"date": "2026-12-21", "drop": 3.0}]
    dropped = run_sim(zones, paths=20_000)
    assert dropped["supports"][-1]["reach"] > plain["supports"][-1]["reach"]
    assert dropped["resistances"][-1]["reach"] < plain["resistances"][-1]["reach"]


def test_sim_jump_widens_both_tails(run_sim, zones):
    plain = run_sim(zones, paths=20_000)
    zones["shocks"] = [{"date": "2026-11-26", "jump": 0.15}]
    jumped = run_sim(zones, paths=20_000)
    assert jumped["supports"][-1]["reach"] > plain["supports"][-1]["reach"]
    assert jumped["resistances"][-1]["reach"] > plain["resistances"][-1]["reach"]


def test_sim_ignores_a_shock_after_the_horizon(run_sim, zones):
    plain = run_sim(zones, paths=SMALL)
    zones["shocks"] = [{"date": "2028-01-01", "drop": 10.0}]
    assert run_sim(zones, paths=SMALL) == plain


def test_sim_forming_candle_shortens_the_first_step(run_sim, zones, input_data):
    data = copy.deepcopy(input_data)
    data["data"]["series"][-1]["last_row"] = "closed"
    closed = run_sim(zones, source=data, paths=SMALL)
    forming = run_sim(zones, source=input_data, paths=SMALL)
    assert forming["first_candle_share_left"] == 0.6
    assert closed["first_candle_share_left"] == 1.0
    assert closed["horizon"]["closes"] == forming["horizon"]["closes"] == 13


def test_sim_touched_once_measures_touches(run_sim, zones, input_data):
    data = copy.deepcopy(input_data)
    data["want"]["probability"] = "touched_once"
    out = run_sim(zones, source=data, paths=SMALL)
    assert out["measure"] == "touch"
    close = run_sim(zones, source=input_data, paths=SMALL)
    # with intra-candle steps the extremes reach further than the closes alone
    assert out["supports"][-1]["reach"] >= close["supports"][-1]["reach"]


# ---- check -------------------------------------------------------------------

def run_check(rz, capsys, path):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        rz.check(str(path))
    return stop.value.code, capsys.readouterr().out.strip()


def test_check_accepts_the_example_projection(rz, capsys, projection_path):
    assert run_check(rz, capsys, projection_path) == (0, "OK")


def scenario(proj, name):
    return next(s for s in proj["scenarios"] if s["name"] == name)


def scenarios_do_not_sum_to_one(proj):
    scenario(proj, "range")["p"] = 0.29


def overlapping_zones(proj):
    proj["supports"][1]["zone"] = [42.0, 43.5]      # S2 reaches into S1 [43.2, 44.6]


def zones_out_of_order(proj):
    proj["resistances"][0]["zone"], proj["resistances"][1]["zone"] = (
        proj["resistances"][1]["zone"], proj["resistances"][0]["zone"])


def confidence_high(proj):
    proj["confidence"]["level"] = "high"


def driver_without_source(proj):
    del proj["drivers"][0]["source"]


def driver_source_not_a_url(proj):
    proj["drivers"][0]["source"] = "Reuters, 12 Nov"


def reach_not_sum_of_turns(proj):
    proj["supports"][1]["reach"] = 0.90             # the turns from S2 outward sum to 0.80


def turn_above_reach(proj):
    proj["resistances"][3]["turn"] = 0.20           # the tail's reach is 0.14


def no_evidence(proj):
    proj["resistances"][1]["evidence"] = []


def s1_above_price(proj):
    proj["as_of"]["price"] = 43.0


def r1_below_price(proj):
    proj["as_of"]["price"] = 47.5


def wrong_scenario_names(proj):
    scenario(proj, "whipsaw")["name"] = "chop"


def breakdown_disagrees_with_reach(proj):
    scenario(proj, "breakdown")["p"] = 0.40         # 0.40 + 0.06 != S3 reach 0.56
    scenario(proj, "range")["p"] = 0.29             # still sums to 1


@pytest.mark.parametrize("breakage, message", [
    (scenarios_do_not_sum_to_one, "scenarios must sum to 1"),
    (overlapping_zones, "supports: zones must run nearest first without overlapping"),
    (zones_out_of_order, "resistances: zones must run nearest first without overlapping"),
    (confidence_high, "confidence is low or medium, never high"),
    (driver_without_source, "driver without a source URL"),
    (driver_source_not_a_url, "driver without a source URL"),
    (reach_not_sum_of_turns, "S2: reach must equal the sum of turn from here outward"),
    (turn_above_reach, "above: need 0 <= turn <= reach <= 1"),
    (no_evidence, "R2: no evidence"),
    (s1_above_price, "S1 lies above the price"),
    (r1_below_price, "R1 lies below the price"),
    (wrong_scenario_names, "scenarios are breakdown, breakout, range, whipsaw"),
    (breakdown_disagrees_with_reach, "breakdown + whipsaw must equal 0.56"),
], ids=lambda value: value.__name__ if callable(value) else None)
def test_check_rejects_a_broken_projection(rz, capsys, projection_data, write_json, breakage, message):
    breakage(projection_data)
    code, out = run_check(rz, capsys, write_json("broken.json", projection_data))
    assert code == 1
    assert any(message in line for line in out.splitlines()), out
    assert "OK" not in out.splitlines()


def test_check_reports_every_violation(rz, capsys, projection_data, write_json):
    confidence_high(projection_data)
    driver_without_source(projection_data)
    scenarios_do_not_sum_to_one(projection_data)
    code, out = run_check(rz, capsys, write_json("broken.json", projection_data))
    assert code == 1
    assert len(out.splitlines()) == 3


def test_check_allows_touching_zones_and_a_projection_without_drivers(rz, capsys, projection_data, write_json):
    assert projection_data["resistances"][0]["zone"][1] == projection_data["resistances"][1]["zone"][0]
    del projection_data["drivers"]
    assert run_check(rz, capsys, write_json("ok.json", projection_data)) == (0, "OK")


def test_check_accepts_an_unadjusted_sim_baseline(rz, capsys, run_sim, zones, projection_from_sim, write_json):
    zones["shocks"] = [{"date": "2026-12-21", "drop": 1.19}]
    out = run_sim(zones)
    proj = projection_from_sim(out, zones)
    assert run_check(rz, capsys, write_json("baseline.json", proj)) == (0, "OK")
