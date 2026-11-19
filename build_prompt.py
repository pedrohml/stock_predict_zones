#!/usr/bin/env python3
"""Build the reversal-zones JSON prompt from Yahoo Finance data.

    pip install yfinance
    python build_prompt.py NVDA --horizon 1-3m
    python build_prompt.py PETR4.SA --horizon 1-2w --volume -o petr4.json
    python build_prompt.py NVDA --event "2026-11-17: Q3 earnings, after close"
    python build_prompt.py PETR4.SA --prices data/petr4.csv     # your own candles, no Yahoo

Prints the JSON to stdout (or writes it with -o). Paste it into the chat as is.
Yahoo requests go through data_source.py (retries, timeout, shared rate limit, cache of
finished candles, row checks); --no-cache downloads everything afresh.
"""
import argparse
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

import data_source

# horizon -> the main series the levels are read from, an optional coarser
# context series that covers only the history BEFORE the main series starts,
# the candle the probabilities are judged on, and what "probability" means.
# Each series is (Yahoo interval, Yahoo period, max rows kept).
PRESETS = {
    "today": {
        "main": ("15m", "5d", 140),
        "context": ("1d", "3mo", 40),
        "close_basis": "15m",
        "probability": "touched_once",
        "event_days": 1,
    },
    "1-2w": {
        "main": ("1d", "6mo", 90),
        "context": ("1wk", "2y", 52),
        "close_basis": "1D",
        "probability": "close_in_zone",
        "event_days": 14,
    },
    "1-3m": {
        "main": ("1wk", "2y", 104),
        "context": ("1mo", "5y", 36),
        "close_basis": "1W",
        "probability": "close_in_zone",
        "event_days": 92,
    },
    "6-12m": {
        "main": ("1mo", "10y", 120),
        "context": None,
        "close_basis": "1M",
        "probability": "close_in_zone",
        "event_days": 366,
    },
}
LABEL = {"15m": "15m", "1h": "1h", "1d": "1D", "1wk": "1W", "1mo": "1M"}
INTRADAY = {"15m", "1h"}
SCHEMA_VERSION = 1   # of this input format; raise it when a field changes meaning or goes away
SOURCE = "Yahoo Finance (yfinance)"


def to_ts(value, tz):
    """Yahoo metadata gives epoch seconds or Timestamps depending on version."""
    if isinstance(value, (int, float)):
        return pd.Timestamp(value, unit="s", tz="UTC").tz_convert(tz)
    ts = pd.Timestamp(value)
    return ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)


def session_state(meta, now, tz):
    periods = meta.get("currentTradingPeriod") or {}
    try:
        spans = {
            name: (to_ts(periods[name]["start"], tz), to_ts(periods[name]["end"], tz))
            for name in ("pre", "regular", "post")
        }
    except (KeyError, TypeError, ValueError):
        return "unsure"
    if spans["regular"][0] <= now < spans["regular"][1]:
        return "open"
    if spans["pre"][0] <= now < spans["pre"][1]:
        return "pre_market"
    if spans["post"][0] <= now < spans["post"][1]:
        return "after_hours"
    return "closed"


def last_row_state(interval, start, now, session):
    """Is the newest candle still being built?"""
    regular_done = session in ("after_hours", "closed")
    if interval in INTRADAY:
        minutes = 15 if interval == "15m" else 60
        return "forming" if now < start + timedelta(minutes=minutes) else "closed"
    if interval == "1d":
        same_day = start.date() == now.date()
        return "forming" if same_day and not regular_done else "closed"
    if interval == "1wk":
        in_week = now < start + timedelta(days=7)
        week_over = now.weekday() >= 5 or (now.weekday() == 4 and regular_done)
        return "forming" if in_week and not week_over else "closed"
    if interval == "1mo":
        same_month = (start.year, start.month) == (now.year, now.month)
        return "forming" if same_month else "closed"
    return "unsure"


def history(ticker, interval, period, tz, symbol=None, cache=True, reports=None):
    """The candles of `interval` over `period` from Yahoo, checked and indexed in exchange
    time. data_source.candles() makes the request (retried, rate-limited) and serves finished
    candles from the cache unless `cache` is false; what it did goes to reports[interval]."""
    # auto_adjust=False keeps prices split-adjusted but not dividend-adjusted,
    # which is what most charting platforms show by default.
    symbol = symbol or getattr(ticker, "ticker", "?")
    try:
        frame, report = data_source.candles(ticker, symbol, interval, period=period, cache=cache,
                                            what=f"{symbol.upper()} {interval}")
    except data_source.DataError as error:
        raise SystemExit(str(error))
    if reports is not None:
        reports[interval] = report
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if not frame.empty:
        frame = frame.tz_convert(tz) if frame.index.tz is not None else frame.tz_localize(tz)
    return frame


def make_series(frame, interval, max_rows, now, session, volume, decimals, before=None,
                name="this ticker"):
    """Candles already in exchange time -> (series, first candle start). With `before`, keep
    only candles that start on an earlier date, so a context series never repeats history
    the main series already covers. Returns (None, None) if nothing is left. No network."""
    if before is not None:
        frame = frame[frame.index.normalize() < before.normalize()]
    frame = frame.tail(max_rows)
    if frame.empty:
        if before is not None:
            return None, None
        raise SystemExit(f"No {interval} data returned for {name}.")

    stamp = "%Y-%m-%dT%H:%M" if interval in INTRADAY else "%Y-%m-%d"
    rows = []
    for start, candle in frame.iterrows():
        row = [start.strftime(stamp)]
        row += [round(float(candle[c]), decimals) for c in ("Open", "High", "Low", "Close")]
        if volume:
            row.append(int(candle["Volume"]) if pd.notna(candle["Volume"]) else 0)
        rows.append(row)

    series = {
        "candles": LABEL[interval],
        "last_row": last_row_state(interval, frame.index[-1], now, session),
        "rows": rows,
    }
    return series, frame.index[0]


def make_all_series(args, candles_of, now, session, name="this ticker"):
    """The input's `data.series` for `args.horizon`, context first. `candles_of(interval,
    period)` returns the candles of one preset entry in exchange time: a Yahoo request when
    live, a slice of candles already downloaded in backtest.py. The context is asked for only
    after the main series, and only candles that start before the main series are kept."""
    preset = PRESETS[args.horizon]
    interval, period, max_rows = preset["main"]
    main, main_start = make_series(
        candles_of(interval, period), interval, min(max_rows, args.max_rows or max_rows),
        now, session, args.volume, args.decimals, name=name)
    series = [main]
    if preset["context"]:
        interval, period, max_rows = preset["context"]
        context, _ = make_series(candles_of(interval, period), interval, max_rows, now,
                                 session, args.volume, args.decimals, before=main_start,
                                 name=name)
        if context:
            series.insert(0, context)  # older, coarser history first
    return series


def upcoming_events(ticker, now, days, manual):
    """Earnings and ex-dividend dates inside the horizon, plus --event notes.
    A note replaces Yahoo's entry for the same date. Empty list = search."""
    notes = []
    for note in manual or []:
        date, _, what = note.partition(":")
        try:
            date = pd.Timestamp(date.strip()).date().isoformat()
        except ValueError:
            raise SystemExit(f"--event must look like '2026-11-17: text', got '{note}'")
        notes.append({"date": date, "what": what.strip() or "event"})
    noted_dates = {note["date"] for note in notes}
    try:
        if ticker is None:            # candles from files: no calendar to ask
            raise LookupError
        data_source.throttle()        # one more Yahoo request
        calendar = data_source.call(lambda: ticker.calendar, data_source.env_number(
            data_source.TIMEOUT_ENV, data_source.DEFAULT_TIMEOUT) or None) or {}
    except Exception:
        calendar = {}
    found = list(notes)
    wanted = {"Earnings Date": "earnings", "Ex-Dividend Date": "ex-dividend"}
    limit = now.date() + timedelta(days=days)
    for key, label in wanted.items():
        dates = calendar.get(key)
        if dates is None:
            continue
        for day in dates if isinstance(dates, (list, tuple)) else [dates]:
            if hasattr(day, "date"):
                day = day.date()
            if now.date() <= day <= limit and day.isoformat() not in noted_dates:
                found.append({"date": day.isoformat(), "what": label})
    return sorted(found, key=lambda event: event["date"])


def build(args):
    """The live input: everything from Yahoo, as of now (or from CSV files with --prices)."""
    if getattr(args, "prices", None):
        return build_from_files(args)
    preset = PRESETS[args.horizon]
    ticker = yf.Ticker(args.ticker)
    cache = not getattr(args, "no_cache", False)

    # The first history call also fills the metadata we need. Never cached: it is the
    # newest data there is, and the call that tells an unknown symbol.
    try:
        probe = data_source.fetch(ticker, "1d", period="5d", what=f"{args.ticker.upper()} (first request)")
    except data_source.DataError as error:
        raise SystemExit(str(error))
    if probe.empty:
        raise SystemExit(f"No data for '{args.ticker}'. Check the Yahoo symbol.")
    meta = ticker.get_history_metadata() or {}
    tz = meta.get("exchangeTimezoneName") or "America/New_York"
    now = pd.Timestamp.now(tz=tz)
    session = session_state(meta, now, tz)
    price = meta.get("regularMarketPrice") or float(probe["Close"].iloc[-1])

    reports = {}
    series = make_all_series(
        args, lambda interval, period: history(ticker, interval, period, tz, args.ticker, cache, reports),
        now, session, name=ticker.ticker)
    events = upcoming_events(ticker, now, preset["event_days"], args.event)
    prompt = assemble(args, meta, now, session, price, series, events)
    # provenance is stamped here, on the live path only: backtest.py's past inputs come
    # from candles it downloaded itself and are never saved as projections
    interval, period, max_rows = preset["main"]
    main_request = (interval, period, min(max_rows, args.max_rows or max_rows))
    requested = [preset["context"], main_request] if len(series) == 2 else [main_request]
    version = getattr(yf, "__version__", None)
    prompt["provenance"] = provenance(prompt, SOURCE, requested, f"yfinance {version}" if version else None,
                                      details=[series_details(reports.get(r[0])) for r in requested])
    return prompt


def series_details(report, built_from=None):
    """What data_source did to one series, for its provenance entry: nothing when the candles
    were downloaded in this run and needed no fixing (then the entry gains no key at all)."""
    out = {}
    if built_from:
        out["built_from"] = built_from
    for key in ("checks", "cache", "note"):
        if report and report.get(key):
            out[key] = report[key]
    return out


def build_from_files(args):
    """The input from CSV files (--prices): no Yahoo request at all. Periods are counted back
    from each series' newest candle, the session is "unsure" (files carry no trading hours),
    the price is the main series' last close, and events are only the --event notes."""
    try:
        prices = data_source.load_prices(args.prices, args.ticker)
        preset = PRESETS[args.horizon]
        main_interval = preset["main"][0]
        main_frame = prices.frame(main_interval)
        if preset["context"]:
            prices.frame(preset["context"][0])
    except data_source.DataError as error:
        raise SystemExit(str(error))
    tz = prices.meta["timezone"]
    now = pd.Timestamp.now(tz=tz)
    session = "unsure"

    def candles_of(interval, period):
        frame = prices.frame(interval)
        since = frame.index[-1].normalize() - data_source.period_offset(period)
        return frame[frame.index >= since]

    series = make_all_series(args, candles_of, now, session, name=args.ticker.upper())
    newest = main_frame.index[-1]
    if now - newest > pd.Timedelta(days=7):
        print(f"Warning: the newest {main_interval} candle in the CSV data starts {newest.date()}, "
              f"{(now - newest).days} days before now", file=sys.stderr)
    meta = {"exchangeTimezoneName": tz, "currency": prices.meta["currency"],
            "fullExchangeName": prices.meta["venue"]}
    events = upcoming_events(None, now, preset["event_days"], args.event)
    prompt = assemble(args, meta, now, session, main_frame["Close"].iloc[-1], series, events)
    interval, period, max_rows = preset["main"]
    main_request = (interval, period, min(max_rows, args.max_rows or max_rows))
    requested = [preset["context"], main_request] if len(series) == 2 else [main_request]
    names = ", ".join(Path(f["path"]).name for f in prices.files if f.get("role") != "metadata")
    details = [series_details({"checks": prices.checks.get(r[0])}, prices.built.get(r[0]))
               for r in requested]
    prompt["provenance"] = provenance(prompt, f"CSV ({names})", requested, details=details,
                                      auto_adjust=None, files=prices.files)
    return prompt


def assemble(args, meta, now, session, price, series, events):
    """The input JSON from its parts. No network: backtest.py calls it for past dates."""
    preset = PRESETS[args.horizon]
    tz = meta.get("exchangeTimezoneName") or "America/New_York"
    columns = ["start", "open", "high", "low", "close"] + (["volume"] if args.volume else [])

    prompt = {
        "task": "reversal_zones",
        "schema_version": SCHEMA_VERSION,
        "asset": {
            "ticker": args.ticker.upper(),
            "venue": meta.get("fullExchangeName") or meta.get("exchangeName") or "unsure",
            "currency": meta.get("currency") or "unsure",
        },
        "now": {
            "time": now.strftime("%Y-%m-%dT%H:%M%z")[:-2] + ":" + now.strftime("%z")[-2:],
            "price": round(float(price), args.decimals),
            "session": session,
        },
        "horizon": args.horizon,
        "data": {
            "timezone": tz,
            "split_adjusted": True,
            "columns": columns,
            "series": series,
        },
        "events": events,
        "want": {
            "supports": args.supports,
            "resistances": args.resistances,
            "probability": preset["probability"],
            "close_basis": preset["close_basis"],
        },
        "research": args.research,
        "reply": {
            "shape": args.shape,
            "max_words": args.max_words,
            "language": args.language,
            "links": args.links,
        },
    }
    return prompt


def data_hash(data):
    """sha256 of the `data` object in canonical JSON (sorted keys, no spaces): it changes
    with any candle, column or series label, and not with the file's layout."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provenance(prompt, source, requested=None, library=None, downloaded=None, details=None,
               auto_adjust=False, files=None):
    """Where the candles came from, for the record: the source, when they were downloaded
    (UTC), how prices are adjusted, and per series the rows kept and the dates they span.
    `requested` is the (interval, period, max rows) asked for, per series in the same order;
    `details` adds per series what data_source did (row checks, rows served from the cache).
    With CSV files, `files` lists each one with its sha256, the time is `read_at`, and
    `auto_adjust` is None: the adjustment is whatever the files hold. Read from the finished
    prompt, so it describes what the file holds."""
    downloaded = downloaded or pd.Timestamp.now(tz="UTC")
    out = {"source": source}
    if library:
        out["library"] = library
    if files:
        out["files"] = files
    out["read_at" if files else "downloaded_at"] = downloaded.strftime("%Y-%m-%dT%H:%M:%SZ")
    if auto_adjust is None:
        out["adjustment"] = "as in the files (expected: split-adjusted, not dividend-adjusted)"
    else:
        out["auto_adjust"] = auto_adjust   # False: split-adjusted, not dividend-adjusted (see history())
    out.update({
        "series": [],
        "data_sha256": data_hash(prompt["data"]),
    })
    for index, one in enumerate(prompt["data"]["series"]):
        entry = {"candles": one["candles"], "rows": len(one["rows"]),
                 "first": one["rows"][0][0] if one["rows"] else None,
                 "last": one["rows"][-1][0] if one["rows"] else None}
        if requested and index < len(requested) and requested[index]:
            entry.update(zip(("interval", "period", "max_rows"), requested[index]))
        if details and index < len(details) and details[index]:
            entry.update(details[index])
        out["series"].append(entry)
    return out


def render(prompt):
    """Indented JSON, but each candle row and each event stays on one line."""
    slots = {}
    columns_key = "@@COLUMNS@@"
    slots[f'"{columns_key}"'] = json.dumps(prompt["data"]["columns"])
    prompt["data"]["columns"] = columns_key
    for index, event in enumerate(prompt["events"]):
        key = f"@@EVENT{index}@@"
        slots[f'"{key}"'] = json.dumps(event, ensure_ascii=False)
        prompt["events"][index] = key
    for series in prompt["data"]["series"]:
        for index, row in enumerate(series["rows"]):
            key = f"@@ROW{len(slots)}@@"
            slots[f'"{key}"'] = json.dumps(row, separators=(", ", ": "))
            series["rows"][index] = key
    text = json.dumps(prompt, indent=2, ensure_ascii=False)
    for key, row in slots.items():
        text = text.replace(key, row)
    return text


def arguments():
    """The command line. run_pipeline.py repeats the standing options (names, choices,
    defaults) in its SETTINGS so that a config file can set them; tests/test_config.py
    compares the two."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ticker", help="Yahoo symbol, e.g. NVDA, PETR4.SA, BTC-USD")
    parser.add_argument("--horizon", choices=PRESETS, default="1-3m")
    parser.add_argument("--volume", action="store_true", help="add a volume column")
    parser.add_argument("--max-rows", type=int,
                        help="cap rows of the main series to shrink the prompt")
    parser.add_argument("--event", action="append", metavar="'DATE: TEXT'",
                        help="add or refine an event, e.g. "
                             "'2026-11-17: Q3 earnings, after close' (repeatable)")
    parser.add_argument("--supports", type=int, default=3)
    parser.add_argument("--resistances", type=int, default=3)
    parser.add_argument("--research", choices=["none", "one_search", "deep"],
                        default="one_search")
    parser.add_argument("--shape", choices=["table", "list"], default="table")
    parser.add_argument("--max-words", type=int, default=200)
    parser.add_argument("--language", default="en",
                        help="language of the projection's free text; plot_projection.py uses it "
                             "for the chart labels too (default: en)")
    parser.add_argument("--links", choices=["none", "decisive_only", "all"],
                        default="decisive_only")
    parser.add_argument("--decimals", type=int, default=2)
    parser.add_argument("--prices", metavar="FILE.csv|DIR",
                        help="read the candles from CSV files instead of Yahoo Finance: one file, "
                             "or a folder holding TICKER/, TICKER.csv or 1d.csv, 1wk.csv ... "
                             "(README: Data sources)")
    parser.add_argument("--no-cache", action="store_true",
                        help="download every candle from Yahoo again instead of reusing finished "
                             "ones from the local candle cache")
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    return parser


def main():
    args = arguments().parse_args()

    text = render(build(args))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"Wrote {args.output} ({len(text):,} characters)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
