"""data_source.py offline: retries, the timeout, the shared rate limit, row checks, the candle
cache and CSV import, against fake Yahoo handles. conftest gives every test an empty cache
folder of its own, no spacing between requests and no real waits (data_source.sleep)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_runs  # noqa: E402
import build_prompt  # noqa: E402
import data_source as ds  # noqa: E402

TZ = "America/Sao_Paulo"
NOW = pd.Timestamp("2026-11-19 15:00", tz="UTC")     # a Thursday, during the session


# ---- fakes -------------------------------------------------------------------------

class RateLimited(Exception):
    pass


RateLimited.__name__ = "YFRateLimitError"


class Missing(Exception):
    pass


Missing.__name__ = "YFTzMissingError"


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("Response", (), {"status_code": status})()


def market(end=NOW, start="2023-01-02", scale=1.0):
    """Daily candles like Yahoo's, through the day of `end` (the forming one)."""
    index = pd.date_range(start, end.tz_convert(TZ).tz_localize(None).normalize(), freq="B", tz=TZ)
    close = [scale * (40 + (i % 17) * 0.3 + i * 0.01) for i in range(len(index))]
    return pd.DataFrame({"Open": [c - 0.1 for c in close], "High": [c + 0.5 for c in close],
                         "Low": [c - 0.6 for c in close], "Close": close,
                         "Volume": [1000 + i for i in range(len(index))]}, index=index)


class FakeTicker:
    """A yfinance.Ticker over a frame of daily candles. `failures` are raised first, in order."""

    def __init__(self, daily, failures=(), delay=0.0):
        self.daily, self.failures, self.delay = daily, list(failures), delay
        self.calls = []
        self.ticker = "TEST.SA"

    def history(self, period=None, interval="1d", start=None, auto_adjust=True, actions=True, timeout=10):
        self.calls.append({"period": period, "start": start, "interval": interval, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.failures:
            raise self.failures.pop(0)
        frame = self.daily if interval == "1d" else ds.resample(self.daily, interval)
        if period:
            since = NOW.tz_convert(TZ).tz_localize(None).normalize() - ds.period_offset(period)
            frame = frame[frame.index >= since.tz_localize(TZ)]
        else:
            frame = frame[frame.index >= pd.Timestamp(start, tz=TZ)]
        return frame.copy()

    def get_history_metadata(self):
        return {"exchangeTimezoneName": TZ, "currency": "BRL", "regularMarketPrice": 41.0}


@pytest.fixture
def waits(monkeypatch):
    seen = []
    monkeypatch.setattr(ds, "sleep", seen.append)
    monkeypatch.setattr(ds.random, "uniform", lambda low, high: high)    # no jitter: exact waits
    return seen


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(ds, "now_utc", lambda: NOW)


# ---- retries and the timeout ---------------------------------------------------------

@pytest.mark.parametrize("error, kind", [
    (RateLimited("Too Many Requests. Rate limited."), "rate_limited"),
    (HTTPError(429), "rate_limited"),
    (HTTPError(503), "transient"),
    (HTTPError(404), "missing"),
    (TimeoutError("slow"), "transient"),
    (ConnectionError("reset"), "transient"),
    (type("ReadTimeout", (Exception,), {})("curl"), "transient"),
    (Missing("$ZZZZ: possibly delisted; no timezone found"), "missing"),
    (type("YFPricesMissingError", (Exception,), {})("$X: No data found, symbol may be delisted"), "missing"),
    (type("YFPricesMissingError", (Exception,), {})("$X: no price data found (Yahoo status_code = 502)"),
     "transient"),
    (ValueError("a bug"), "other"),
])
def test_errors_are_sorted_into_retried_missing_and_other(error, kind):
    assert ds.classify(error) == kind


def test_a_rate_limit_is_retried_with_growing_waits(waits, capsys):
    handle = FakeTicker(market(), [RateLimited("Too Many Requests"), RateLimited("Too Many Requests")])
    frame = ds.fetch(handle, "1d", period="5d", what="TEST.SA 1d")
    assert not frame.empty and len(handle.calls) == 3
    assert waits == [ds.BACKOFF_RATE_LIMITED, 2 * ds.BACKOFF_RATE_LIMITED]
    assert capsys.readouterr().err.count("retry") == 2


def test_a_5xx_is_retried_and_then_given_up_with_one_line(waits):
    handle = FakeTicker(market(), [HTTPError(503)] * 10)
    with pytest.raises(ds.DataError) as stop:
        ds.fetch(handle, "1d", period="5d", what="TEST.SA 1d")
    assert len(handle.calls) == ds.ATTEMPTS
    assert waits == [min(ds.BACKOFF_CAP, ds.BACKOFF * 2 ** n) for n in range(ds.ATTEMPTS - 1)]
    assert "Yahoo kept failing for TEST.SA 1d (HTTPError: HTTP 503)" in str(stop.value)
    assert "\n" not in str(stop.value)


def test_the_total_wait_is_bounded(waits, monkeypatch):
    monkeypatch.setattr(ds, "BUDGET", 10.0)
    handle = FakeTicker(market(), [RateLimited("429")] * 10)
    with pytest.raises(ds.DataError):
        ds.fetch(handle, "1d", period="5d")
    assert sum(waits) <= 10.0 and len(handle.calls) == 2     # 5 s, then 10 s would pass the budget


def test_an_unknown_symbol_is_not_retried(waits):
    handle = FakeTicker(market(), [Missing("$ZZZZ: possibly delisted; no timezone found")])
    assert ds.fetch(handle, "1d", period="5d").empty
    assert len(handle.calls) == 1 and waits == []


def test_a_bug_is_raised_as_it_is(waits):
    handle = FakeTicker(market(), [ValueError("a bug")])
    with pytest.raises(ValueError):
        ds.fetch(handle, "1d", period="5d")
    assert len(handle.calls) == 1


def test_a_hung_request_is_abandoned_and_retried(waits, monkeypatch):
    monkeypatch.setenv(ds.TIMEOUT_ENV, "0.2")
    handle = FakeTicker(market(), delay=1.0)
    original = handle.history

    def slow_once(**kwargs):
        if len(handle.calls) == 1:     # the second attempt answers at once
            handle.delay = 0
        return original(**kwargs)
    handle.history = slow_once
    began = time.monotonic()
    frame = ds.fetch(handle, "1d", period="5d")
    assert not frame.empty and len(handle.calls) == 2 and time.monotonic() - began < 0.9
    assert waits == [ds.BACKOFF]


def test_the_timeout_is_passed_only_to_a_history_that_takes_it():
    handle = FakeTicker(market())
    ds.fetch(handle, "1d", period="5d")
    assert handle.calls[0]["timeout"] == ds.DEFAULT_TIMEOUT
    seen = {}

    class Recorder:      # like the fakes elsewhere: **kwargs, and an exact check of what it got
        def history(self, **kwargs):
            seen.update(kwargs)
            return pd.DataFrame()
    ds.fetch(Recorder(), "1d", start="2026-11-01", actions=True)
    assert seen == {"start": "2026-11-01", "interval": "1d", "auto_adjust": False, "actions": True}


# ---- the shared rate limit --------------------------------------------------------------

def test_requests_are_spaced_within_a_process(monkeypatch, waits):
    monkeypatch.setenv(ds.SPACING_ENV, "0.5")
    first, second = ds.throttle(), ds.throttle()
    assert first == 0 and 0.4 < second <= 0.5
    assert (ds.cache_root() / "yahoo.next").is_file()


def test_requests_are_spaced_across_processes(tmp_path):
    """Four processes ask for a turn at once: their turns come 0.3 s apart."""
    script = ("import sys, time; sys.path.insert(0, sys.argv[1]); import data_source; "
              "data_source.throttle(); print(time.time())")
    env = {**os.environ, ds.SPACING_ENV: "0.3", ds.CACHE_ENV: str(tmp_path)}
    children = [subprocess.Popen([sys.executable, "-c", script, str(ROOT)], stdout=subprocess.PIPE,
                                 text=True, env=env) for _ in range(4)]
    turns = sorted(float(child.communicate(timeout=30)[0]) for child in children)
    gaps = [b - a for a, b in zip(turns, turns[1:])]
    assert all(gap > 0.25 for gap in gaps), gaps


def test_a_broken_rate_limit_file_never_stops_a_download(monkeypatch, tmp_path):
    monkeypatch.setenv(ds.SPACING_ENV, "0.5")
    (ds.cache_root()).mkdir(parents=True, exist_ok=True)
    (ds.cache_root() / "yahoo.next").write_text("garbage")
    assert ds.throttle() == 0
    (ds.cache_root() / "yahoo.next").write_text(str(time.time() + 10 ** 6))   # far future: reset
    assert ds.throttle() == 0


# ---- row checks -------------------------------------------------------------------------

def frame_of(rows, index=None):
    index = pd.date_range("2026-01-05", periods=len(rows), freq="B", tz=TZ) if index is None else index
    return pd.DataFrame(rows, index=pd.DatetimeIndex(index), columns=["Open", "High", "Low", "Close"])


def issues(checks):
    return {c["issue"]: (c["action"], c["rows"]) for c in checks}


def test_clean_candles_pass_unchanged():
    frame = market().iloc[:50]
    out, checks = ds.validate(frame, "1d")
    assert checks == [] and out.equals(frame)


def test_each_row_problem_is_dropped_or_fixed_and_reported(capsys):
    nan = float("nan")
    index = pd.DatetimeIndex(["2026-01-05", "2026-01-07", "2026-01-06", "2026-01-07", "2026-01-08",
                              "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15",
                              "2026-01-16", "2026-01-19", "2026-01-20", "2026-01-21", "2026-01-22"], tz=TZ)
    rows = [[10, 11, 9, 10]] * 15
    rows = [list(r) for r in rows]
    rows[1] = [10, 11, 9, 10.2]           # 01-07, replaced by the later 01-07 row
    rows[4] = [nan, nan, nan, nan]        # missing
    rows[5] = [10, 9, 11, 10]             # high below low
    rows[6] = [12, 11, 9, 10]             # open above high
    rows[7] = [0, 11, 9, 10]              # zero price
    out, checks = ds.validate(frame_of(rows, index), "1d", "TEST.SA 1d")
    assert issues(checks) == {"unsorted": ("sorted", 15), "missing_price": ("dropped", 1),
                              "duplicate_time": ("dropped", 1), "non_positive_price": ("dropped", 1),
                              "high_below_low": ("swapped", 1), "ohlc_outside_range": ("widened", 1)}
    assert out.index.is_monotonic_increasing and not out.index.duplicated().any() and len(out) == 12
    assert (out["High"] >= out[["Open", "Close", "Low"]].max(axis=1)).all()
    assert (out["Low"] <= out[["Open", "Close", "High"]].min(axis=1)).all()
    assert out.loc["2026-01-12", "High"] == 12
    err = capsys.readouterr().err
    assert err.startswith("Warning: TEST.SA 1d: sorted 15 row(s) out of time order") and err.count("\n") == 1


def test_gaps_and_split_like_jumps_are_kept_and_reported():
    index = pd.DatetimeIndex(["2026-01-05", "2026-01-06", "2026-02-02", "2026-02-03", "2026-02-04"], tz=TZ)
    rows = [[10, 11, 9, 10], [10, 11, 9, 10], [10, 11, 9, 10], [5, 5.5, 4.5, 5.01], [5, 5.5, 4.5, 5]]
    out, checks = ds.validate(frame_of(rows, index), "1d")
    assert len(out) == 5
    found = {c["issue"]: c for c in checks}
    assert found["gap"]["first"] == "2026-02-02" and found["gap"]["longest_days"] == 27
    assert found["split_like_jump"]["first"] == "2026-02-03" and found["split_like_jump"]["action"] == "kept"
    rows[3] = [6, 6.5, 5.5, 6.5]           # -35%: a move, not a split ratio
    assert "split_like_jump" not in issues(ds.validate(frame_of(rows, index), "1d")[1])


def test_too_many_bad_rows_stop_the_data():
    nan = float("nan")
    rows = [[10, 11, 9, 10]] * 6 + [[nan] * 4] * 4
    with pytest.raises(ds.DataError, match="4 of 10 rows"):
        ds.validate(frame_of(rows), "1d", "TEST.SA 1d")


# ---- the cache ----------------------------------------------------------------------------

def test_finished_candles_are_cached_and_the_newest_always_downloaded(frozen):
    handle = FakeTicker(market())
    first, report = ds.candles(handle, "TEST.SA", "1d", period="6mo")
    assert report["cache"] is None and handle.calls[-1]["period"] == "6mo"
    entry = json.loads(ds.entry_path("TEST.SA", "1d", False).read_text())
    assert entry["rows"][-1][0].startswith("2026-11-17")          # 11-18 ended less than a day ago, 11-19 forms
    assert (entry["interval"], entry["adjustment"], entry["actions"]) == ("1d", "split", False)

    handle.daily = market()                                        # Yahoo moves the forming candle
    handle.daily.iloc[-1, 3] += 1.0
    second, report = ds.candles(handle, "TEST.SA", "1d", period="6mo")
    asked = handle.calls[-1]
    assert asked["period"] is None and asked["start"] == entry["rows"][-ds.OVERLAP][0][:10]
    assert second.iloc[-1]["Close"] == handle.daily.iloc[-1]["Close"]    # the forming candle is fresh
    assert second.iloc[:-1].equals(first.iloc[:-1]) and len(second) == len(first)
    cache = report["cache"]
    assert cache["rows"] == len(second) - (ds.OVERLAP + 2)         # the overlap and the two newest are fresh
    assert cache["downloaded_from"] == cache["downloaded_to"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_start_request_from_the_cache_equals_a_download(frozen):
    handle = FakeTicker(market())
    full, _ = ds.candles(handle, "TEST.SA", "1d", start="2024-01-01")
    again, report = ds.candles(handle, "TEST.SA", "1d", start="2025-03-01")
    assert again.equals(full[full.index >= pd.Timestamp("2025-03-01", tz=TZ)]) and report["cache"]
    earlier, report = ds.candles(handle, "TEST.SA", "1d", start="2023-06-01")   # not covered: downloaded
    assert handle.calls[-1]["start"] == "2023-06-01" and report["cache"] is None
    assert earlier.equals(handle.daily[handle.daily.index >= pd.Timestamp("2023-06-01", tz=TZ)])


def test_a_split_since_the_cache_was_written_replaces_it(frozen, capsys):
    handle = FakeTicker(market())
    ds.candles(handle, "TEST.SA", "1wk", period="2y")
    handle.daily = market(scale=0.5)                               # a 2:1 split rescales all history
    frame, report = ds.candles(handle, "TEST.SA", "1wk", period="2y")
    assert [c["period"] for c in handle.calls] == ["2y", None, "2y"]
    assert report["cache"] is None and "no longer matched" in report["note"]
    assert frame["Close"].iloc[0] == ds.resample(handle.daily, "1wk").loc[frame.index[0], "Close"]
    assert "no longer matched" in capsys.readouterr().err


def test_no_cache_and_intraday_never_touch_it(frozen):
    handle = FakeTicker(market())
    ds.candles(handle, "TEST.SA", "1d", period="6mo", cache=False)
    ds.candles(handle, "TEST.SA", "1d", period="6mo", cache=False)
    assert [c["period"] for c in handle.calls] == ["6mo", "6mo"]
    assert not (ds.cache_root() / "candles").exists()
    assert "15m" not in ds.CACHED_INTERVALS and "1h" not in ds.CACHED_INTERVALS


def test_a_damaged_cache_file_is_downloaded_again(frozen, capsys):
    handle = FakeTicker(market())
    ds.candles(handle, "TEST.SA", "1d", period="6mo")
    ds.entry_path("TEST.SA", "1d", False).write_text("{not json")
    _, report = ds.candles(handle, "TEST.SA", "1d", period="6mo")
    assert report["cache"] is None and handle.calls[-1]["period"] == "6mo"
    assert "unreadable cache file" in capsys.readouterr().err


def test_splits_are_kept_with_the_cache(frozen):
    daily = market()
    daily["Stock Splits"] = 0.0
    daily.loc["2025-06-02", "Stock Splits"] = 2.0
    handle = FakeTicker(daily)
    _, first = ds.candles(handle, "TEST.SA", "1d", start="2025-01-01", actions=True)
    _, second = ds.candles(handle, "TEST.SA", "1d", start="2025-01-01", actions=True)
    assert second["cache"] and [s[1] for s in first["splits"]] == [s[1] for s in second["splits"]] == [2.0]
    assert ds.entry_path("TEST.SA", "1d", True).is_file()        # its own key, beside actions=False


def test_info_and_clear(frozen, capsys):
    handle = FakeTicker(market())
    ds.candles(handle, "TEST.SA", "1d", period="6mo")
    ds.candles(handle, "OTHER", "1mo", period="5y")
    assert [(row[0], row[1]) for row in ds.cache_info()] == [("OTHER", "1mo-split.json"),
                                                            ("TEST.SA", "1d-split.json")]
    assert ds.main(["clear", "other"]) == 0 and "Removed 1" in capsys.readouterr().out
    assert [row[0] for row in ds.cache_info()] == ["TEST.SA"]
    assert ds.main(["clear"]) == 0 and ds.cache_info() == []
    assert ds.main(["info"]) == 0 and "(empty)" in capsys.readouterr().out


def test_the_cache_folder_follows_the_variables(monkeypatch, tmp_path):
    monkeypatch.delenv(ds.CACHE_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ds.cache_root() == tmp_path / "xdg" / "zones"
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")              # the XDG rule: ignored
    assert ds.cache_root() == Path.home() / ".cache" / "zones"


# ---- step 1 with the cache -------------------------------------------------------------

def live_build(monkeypatch, daily, *extra):
    handles = []

    def make(symbol):
        handles.append(FakeTicker(daily))
        return handles[-1]
    monkeypatch.setattr(build_prompt.yf, "Ticker", make)
    args = build_prompt.arguments().parse_args(["TEST.SA", "--horizon", "1-3m", *extra])
    return build_prompt.build(args), handles[-1]


def test_a_second_live_run_reads_the_cache_and_writes_the_same_data(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    daily = market(end=now)
    first, handle = live_build(monkeypatch, daily)
    assert [c["period"] for c in handle.calls] == ["5d", "2y", "5y"]
    assert all("cache" not in s for s in first["provenance"]["series"])
    second, handle = live_build(monkeypatch, daily)
    assert [c["period"] for c in handle.calls] == ["5d", None, None]     # the probe is never cached
    assert second["data"] == first["data"]
    assert second["provenance"]["data_sha256"] == first["provenance"]["data_sha256"]
    assert all(s["cache"]["rows"] > 0 for s in second["provenance"]["series"])
    third, handle = live_build(monkeypatch, daily, "--no-cache")
    assert [c["period"] for c in handle.calls] == ["5d", "2y", "5y"] and third["data"] == first["data"]
    assert all("cache" not in s for s in third["provenance"]["series"])


# ---- CSV files ------------------------------------------------------------------------------

def write_csv(path, frame, header="date,open,high,low,close,volume", sep=","):
    lines = [header.replace(",", sep)]
    for when, row in frame.iterrows():
        lines.append(sep.join([when.strftime("%Y-%m-%d")] + [repr(float(row[c])) for c in
                                                             ("Open", "High", "Low", "Close")]
                              + [str(int(row["Volume"]))]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def from_csv(*argv):
    args = build_prompt.arguments().parse_args(list(argv))
    return build_prompt.build(args)


@pytest.fixture
def no_yahoo(monkeypatch):
    monkeypatch.setattr(build_prompt.yf, "Ticker", lambda symbol: pytest.fail("Yahoo was asked"))


def test_one_daily_csv_gives_every_horizon_without_yahoo(tmp_path, no_yahoo):
    daily = market(end=pd.Timestamp.now(tz="UTC"))
    path = write_csv(tmp_path / "petr4.csv", daily)
    (tmp_path / "petr4.json").write_text('{"timezone": "America/Sao_Paulo", "currency": "BRL", "venue": "B3"}')
    prompt = from_csv("PETR4.SA", "--horizon", "1-3m", "--prices", str(path), "--volume")
    assert [s["candles"] for s in prompt["data"]["series"]] == ["1M", "1W"]
    weekly = ds.resample(daily, "1wk")
    assert prompt["data"]["series"][1]["rows"][-1][1:5] == [round(x, 2) for x in weekly.iloc[-1][:4]]
    assert prompt["asset"] == {"ticker": "PETR4.SA", "venue": "B3", "currency": "BRL"}
    assert prompt["now"]["session"] == "unsure" and prompt["data"]["timezone"] == TZ
    record = prompt["provenance"]
    assert record["source"] == "CSV (petr4.csv)" and "auto_adjust" not in record and "read_at" in record
    assert record["files"][0] == {"path": str(path), "sha256": ds.sha256_file(path), "interval": "1d",
                                  "rows": len(daily)}
    assert record["files"][1]["role"] == "metadata"
    assert [s.get("built_from") for s in record["series"]] == ["1d", "1d"]
    assert record["data_sha256"] == build_prompt.data_hash(prompt["data"])


def test_a_folder_per_interval_and_a_folder_per_ticker(tmp_path, no_yahoo):
    daily = market(end=pd.Timestamp.now(tz="UTC"))
    folder = tmp_path / "data" / "PETR4.SA"
    folder.mkdir(parents=True)
    write_csv(folder / "1d.csv", daily)
    write_csv(folder / "1wk.csv", ds.resample(daily, "1wk"), sep=";")
    prompt = from_csv("petr4.sa", "--horizon", "1-2w", "--prices", str(tmp_path / "data"))
    record = prompt["provenance"]
    assert [s.get("built_from") for s in record["series"]] == [None, None]     # both given
    assert sorted(f["interval"] for f in record["files"]) == ["1d", "1wk"]
    write_csv(tmp_path / "data" / "VALE3.SA.csv", daily)
    prompt = from_csv("VALE3.SA", "--horizon", "6-12m", "--prices", str(tmp_path / "data"))
    assert prompt["provenance"]["source"] == "CSV (VALE3.SA.csv)"


def test_csv_rows_are_checked_like_yahoos(tmp_path, no_yahoo, capsys):
    daily = market(end=pd.Timestamp.now(tz="UTC"))
    daily.iloc[-10, 1] = daily.iloc[-10, 2] - 1                    # high below low
    path = write_csv(tmp_path / "1d.csv", daily)
    prompt = from_csv("X", "--horizon", "1-2w", "--prices", str(path))
    main = prompt["provenance"]["series"][-1]
    assert [c["issue"] for c in main["checks"]] == ["high_below_low", "ohlc_outside_range"]   # then widened
    assert "high below low" in capsys.readouterr().err


@pytest.mark.parametrize("text, message", [
    ("when,open,high,low\n2026-01-05,1,2,0.5\n", "needs the columns date, open, high, low, close"),
    ("date,open,high,low,close\n2026-01-05,1,2,x,1\n", "line 2"),
    ("date,open,high,low,close\n", "no rows"),
])
def test_a_bad_csv_stops_with_one_line(tmp_path, no_yahoo, text, message):
    path = tmp_path / "1d.csv"
    path.write_text(text)
    with pytest.raises(SystemExit) as stop:
        from_csv("X", "--horizon", "1-2w", "--prices", str(path))
    assert message in str(stop.value) and "\n" not in str(stop.value)


def test_missing_intervals_are_named(tmp_path, no_yahoo):
    weekly = ds.resample(market(end=pd.Timestamp.now(tz="UTC")), "1wk")
    path = write_csv(tmp_path / "x_1wk.csv", weekly)
    with pytest.raises(SystemExit, match="no 1d candles"):
        from_csv("X", "--horizon", "1-2w", "--prices", str(path))


def test_the_interval_is_inferred_from_the_rows(tmp_path):
    daily = market().iloc[-200:]
    assert ds.interval_from_times(daily.index, "f") == "1d"
    assert ds.interval_from_times(ds.resample(daily, "1wk").index, "f") == "1wk"
    assert ds.interval_from_times(ds.resample(daily, "1mo").index, "f") == "1mo"
    assert ds.interval_from_name("PETR4_1wk.csv") == "1wk" and ds.interval_from_name("petr4.csv") is None


def test_a_batch_refuses_one_file_for_many_tickers(tmp_path, capsys):
    path = write_csv(tmp_path / "petr4.csv", market().iloc[-30:])
    assert batch_runs.main(["PETR4.SA", "VALE3.SA", "--prices", str(path), "--runs-dir", str(tmp_path)]) == 1
    assert "one ticker's file" in capsys.readouterr().err
