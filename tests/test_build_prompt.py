"""Step 1, build_prompt.py: the pure functions only. Nothing here calls Yahoo."""
import copy
import json
from datetime import date, datetime

import pandas as pd
import pytest

import build_prompt

TZ = "America/Sao_Paulo"


def at(text):
    return pd.Timestamp(text, tz=TZ)


# ---- to_ts -------------------------------------------------------------------

def test_to_ts_epoch_seconds_are_utc():
    ts = build_prompt.to_ts(1_700_000_000, TZ)   # 2023-11-14 22:13:20 UTC
    assert str(ts.tz) == TZ
    assert ts == pd.Timestamp("2023-11-14 22:13:20", tz="UTC")
    assert ts.hour == 19


def test_to_ts_accepts_float_epoch():
    assert build_prompt.to_ts(1_700_000_000.0, TZ) == build_prompt.to_ts(1_700_000_000, TZ)


def test_to_ts_naive_value_is_localized():
    ts = build_prompt.to_ts("2026-11-17 10:00", TZ)
    assert ts == at("2026-11-17 10:00")
    assert (ts.hour, str(ts.tz)) == (10, TZ)


def test_to_ts_aware_value_is_converted():
    ts = build_prompt.to_ts(pd.Timestamp("2026-11-17 13:00", tz="UTC"), TZ)
    assert ts == at("2026-11-17 10:00")
    assert (ts.hour, str(ts.tz)) == (10, TZ)


# ---- session_state -----------------------------------------------------------

def trading_meta():
    epoch = lambda text: int(at(text).timestamp())
    return {"currentTradingPeriod": {
        "pre": {"start": epoch("2026-11-17 09:45"), "end": epoch("2026-11-17 10:00")},
        "regular": {"start": epoch("2026-11-17 10:00"), "end": epoch("2026-11-17 17:00")},
        "post": {"start": epoch("2026-11-17 17:00"), "end": epoch("2026-11-17 18:00")},
    }}


@pytest.mark.parametrize("now, expected", [
    ("2026-11-17 08:00", "closed"),
    ("2026-11-17 09:45", "pre_market"),
    ("2026-11-17 09:59", "pre_market"),
    ("2026-11-17 10:00", "open"),
    ("2026-11-17 16:59", "open"),
    ("2026-11-17 17:00", "after_hours"),
    ("2026-11-17 17:59", "after_hours"),
    ("2026-11-17 18:00", "closed"),
    ("2026-11-18 12:00", "closed"),
])
def test_session_state(now, expected):
    assert build_prompt.session_state(trading_meta(), at(now), TZ) == expected


def test_session_state_accepts_timestamps():
    meta = trading_meta()
    for span in meta["currentTradingPeriod"].values():
        for key in span:
            span[key] = pd.Timestamp(span[key], unit="s", tz="UTC")
    assert build_prompt.session_state(meta, at("2026-11-17 12:00"), TZ) == "open"


@pytest.mark.parametrize("meta", [
    {},
    {"currentTradingPeriod": None},
    {"currentTradingPeriod": {"regular": {"start": 1, "end": 2}}},   # no pre/post
    {"currentTradingPeriod": {"pre": None, "regular": None, "post": None}},
])
def test_session_state_unsure_without_trading_periods(meta):
    assert build_prompt.session_state(meta, at("2026-11-17 12:00"), TZ) == "unsure"


# ---- last_row_state ----------------------------------------------------------

@pytest.mark.parametrize("interval, start, now, session, expected", [
    # intraday: forming until the candle's own span has run out
    ("15m", "2026-11-17 10:00", "2026-11-17 10:14", "open", "forming"),
    ("15m", "2026-11-17 10:00", "2026-11-17 10:15", "open", "closed"),
    ("1h", "2026-11-17 10:00", "2026-11-17 10:59", "open", "forming"),
    ("1h", "2026-11-17 10:00", "2026-11-17 11:00", "open", "closed"),
    # daily: forming on its own day until the regular session is over
    ("1d", "2026-11-17", "2026-11-17 09:50", "pre_market", "forming"),
    ("1d", "2026-11-17", "2026-11-17 12:00", "open", "forming"),
    ("1d", "2026-11-17", "2026-11-17 17:30", "after_hours", "closed"),
    ("1d", "2026-11-17", "2026-11-17 20:00", "closed", "closed"),
    ("1d", "2026-11-16", "2026-11-17 12:00", "open", "closed"),
    # weekly (2026-11-16 is a Monday): forming until Friday's regular close
    ("1wk", "2026-11-16", "2026-11-17 12:00", "open", "forming"),
    ("1wk", "2026-11-16", "2026-11-17 20:00", "closed", "forming"),
    ("1wk", "2026-11-16", "2026-11-20 12:00", "open", "forming"),
    ("1wk", "2026-11-16", "2026-11-20 17:30", "after_hours", "closed"),
    ("1wk", "2026-11-16", "2026-11-21 12:00", "closed", "closed"),
    ("1wk", "2026-11-09", "2026-11-17 12:00", "open", "closed"),
    # monthly: forming for the whole calendar month
    ("1mo", "2026-11-01", "2026-11-30 20:00", "closed", "forming"),
    ("1mo", "2026-10-01", "2026-11-01 00:30", "closed", "closed"),
    ("1mo", "2025-11-01", "2026-11-17 12:00", "open", "closed"),
    # anything else is not guessed
    ("5m", "2026-11-17 10:00", "2026-11-17 10:01", "open", "unsure"),
])
def test_last_row_state(interval, start, now, session, expected):
    assert build_prompt.last_row_state(interval, at(start), at(now), session) == expected


# ---- render ------------------------------------------------------------------

def test_render_round_trips(input_data):
    text = build_prompt.render(copy.deepcopy(input_data))
    assert json.loads(text) == input_data


def test_render_keeps_each_candle_row_on_one_line(input_data):
    lines = [line.strip().rstrip(",") for line in
             build_prompt.render(copy.deepcopy(input_data)).splitlines()]
    all_rows = [row for series in input_data["data"]["series"] for row in series["rows"]]
    assert len(all_rows) == 104 + 36
    for row in all_rows:
        assert json.dumps(row) in lines
    # and nothing else spills a row over several lines
    assert sum(1 for line in lines if line.startswith('["20')) == len(all_rows)


def test_render_keeps_columns_and_events_on_one_line(input_data):
    lines = [line.strip().rstrip(",") for line in
             build_prompt.render(copy.deepcopy(input_data)).splitlines()]
    assert '"columns": ["start", "open", "high", "low", "close"]' in lines
    assert '{"date": "2026-12-21", "what": "ex-dividend"}' in lines


def test_render_is_otherwise_indented_and_not_ascii_escaped(input_data):
    text = build_prompt.render(copy.deepcopy(input_data))
    assert text.startswith('{\n  "task": "reversal_zones",\n')
    assert "São Paulo" in text
    assert "@@" not in text


def test_render_matches_the_committed_fixture(input_data, input_path):
    """The fixture was written by render(); regenerate it with
    tests/fixtures/make_input.py if the format changes on purpose."""
    assert build_prompt.render(copy.deepcopy(input_data)) + "\n" == input_path.read_text(encoding="utf-8")


def test_render_with_volume_several_events_and_no_context(input_data):
    data = copy.deepcopy(input_data)
    data["data"]["columns"].append("volume")
    data["data"]["series"] = data["data"]["series"][1:]
    data["data"]["series"][0]["rows"] = [row + [1_000_000 + i] for i, row in
                                         enumerate(data["data"]["series"][0]["rows"][-12:])]
    data["events"] = [{"date": "2026-11-26", "what": "resultado do 3º trimestre"},
                      {"date": "2026-12-21", "what": "ex-dividend"}]
    text = build_prompt.render(copy.deepcopy(data))
    assert json.loads(text) == data
    lines = [line.strip().rstrip(",") for line in text.splitlines()]
    assert '{"date": "2026-11-26", "what": "resultado do 3º trimestre"}' in lines
    assert sum(1 for line in lines if line.startswith('["20')) == 12


def test_render_with_no_events(input_data):
    data = copy.deepcopy(input_data)
    data["events"] = []
    assert json.loads(build_prompt.render(copy.deepcopy(data))) == data


# ---- upcoming_events ---------------------------------------------------------

class FakeTicker:
    def __init__(self, calendar):
        self.calendar = calendar


class BrokenTicker:
    @property
    def calendar(self):
        raise RuntimeError("Yahoo is down")


NOW = at("2026-11-17 12:00")


def test_upcoming_events_reads_earnings_and_dividends_sorted():
    ticker = FakeTicker({"Earnings Date": [date(2026, 12, 10)], "Ex-Dividend Date": date(2026, 11, 30)})
    assert build_prompt.upcoming_events(ticker, NOW, 92, None) == [
        {"date": "2026-11-30", "what": "ex-dividend"},
        {"date": "2026-12-10", "what": "earnings"},
    ]


def test_upcoming_events_keeps_only_dates_inside_the_horizon():
    ticker = FakeTicker({"Earnings Date": [date(2026, 11, 16),    # yesterday
                                           date(2026, 11, 17),    # today: kept
                                           date(2026, 12, 1),     # last day of 14: kept
                                           date(2026, 12, 2)]})   # one day too far
    found = build_prompt.upcoming_events(ticker, NOW, 14, None)
    assert [event["date"] for event in found] == ["2026-11-17", "2026-12-01"]


def test_upcoming_events_accepts_datetimes_and_timestamps():
    ticker = FakeTicker({"Earnings Date": [pd.Timestamp("2026-11-26 16:00")],
                         "Ex-Dividend Date": datetime(2026, 12, 21, 0, 0)})
    assert build_prompt.upcoming_events(ticker, NOW, 92, []) == [
        {"date": "2026-11-26", "what": "earnings"},
        {"date": "2026-12-21", "what": "ex-dividend"},
    ]


def test_upcoming_events_note_replaces_yahoo_entry_for_the_same_date():
    ticker = FakeTicker({"Earnings Date": [date(2026, 11, 26)], "Ex-Dividend Date": date(2026, 12, 21)})
    found = build_prompt.upcoming_events(ticker, NOW, 92, ["2026-11-26: Q3 earnings, after close"])
    assert found == [
        {"date": "2026-11-26", "what": "Q3 earnings, after close"},
        {"date": "2026-12-21", "what": "ex-dividend"},
    ]


def test_upcoming_events_note_text_and_date_are_normalized():
    found = build_prompt.upcoming_events(FakeTicker({}), NOW, 92,
                                         [" 2026/12/05 :  board meeting: 10:00 ", "2026-11-20"])
    assert found == [
        {"date": "2026-11-20", "what": "event"},
        {"date": "2026-12-05", "what": "board meeting: 10:00"},
    ]


def test_upcoming_events_notes_are_kept_whatever_their_date():
    found = build_prompt.upcoming_events(FakeTicker({}), NOW, 14, ["2027-06-01: far away"])
    assert found == [{"date": "2027-06-01", "what": "far away"}]


@pytest.mark.parametrize("note", ["next tuesday: earnings", "earnings on 2026-11-26", "17-11: x: y"])
def test_upcoming_events_bad_note_exits(note):
    with pytest.raises(SystemExit) as error:
        build_prompt.upcoming_events(FakeTicker({}), NOW, 92, [note])
    assert "--event must look like" in str(error.value)
    assert note in str(error.value)


@pytest.mark.parametrize("ticker", [FakeTicker(None), FakeTicker({}), BrokenTicker()])
def test_upcoming_events_survives_a_missing_or_failing_calendar(ticker):
    assert build_prompt.upcoming_events(ticker, NOW, 92, None) == []
    assert build_prompt.upcoming_events(ticker, NOW, 92, ["2026-11-26: note"]) == [
        {"date": "2026-11-26", "what": "note"}]
