#!/usr/bin/env python3
"""Build the reversal-zones JSON prompt from Yahoo Finance data.

    pip install yfinance
    python build_prompt.py NVDA --horizon 1-3m
    python build_prompt.py PETR4.SA --horizon 1-2w --volume -o petr4.json
    python build_prompt.py NVDA --event "2026-11-17: Q3 earnings, after close"

Prints the JSON to stdout (or writes it with -o). Paste it into the chat as is.
"""
import argparse
import json
import sys
from datetime import timedelta

import pandas as pd
import yfinance as yf

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


def fetch_series(ticker, interval, period, max_rows, tz, now, session, volume,
                 decimals, before=None):
    """Return (series, first candle start). With `before`, keep only candles
    that start on an earlier date, so a context series never repeats history
    the main series already covers. Returns (None, None) if nothing is left."""
    # auto_adjust=False keeps prices split-adjusted but not dividend-adjusted,
    # which is what most charting platforms show by default.
    frame = ticker.history(period=period, interval=interval,
                           auto_adjust=False, actions=False)
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    if not frame.empty:
        frame = frame.tz_convert(tz) if frame.index.tz is not None else frame.tz_localize(tz)
    if before is not None:
        frame = frame[frame.index.normalize() < before.normalize()]
    frame = frame.tail(max_rows)
    if frame.empty:
        if before is not None:
            return None, None
        raise SystemExit(f"No {interval} data returned for {ticker.ticker}.")

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
        calendar = ticker.calendar or {}
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
    preset = PRESETS[args.horizon]
    ticker = yf.Ticker(args.ticker)

    # The first history call also fills the metadata we need.
    probe = ticker.history(period="5d", interval="1d", auto_adjust=False, actions=False)
    if probe.empty:
        raise SystemExit(f"No data for '{args.ticker}'. Check the Yahoo symbol.")
    meta = ticker.get_history_metadata() or {}
    tz = meta.get("exchangeTimezoneName") or "America/New_York"
    now = pd.Timestamp.now(tz=tz)
    session = session_state(meta, now, tz)
    price = meta.get("regularMarketPrice") or float(probe["Close"].iloc[-1])

    interval, period, max_rows = preset["main"]
    main, main_start = fetch_series(
        ticker, interval, period, min(max_rows, args.max_rows or max_rows),
        tz, now, session, args.volume, args.decimals)
    series = [main]
    if preset["context"]:
        interval, period, max_rows = preset["context"]
        context, _ = fetch_series(ticker, interval, period, max_rows, tz, now,
                                  session, args.volume, args.decimals,
                                  before=main_start)
        if context:
            series.insert(0, context)  # older, coarser history first
    columns = ["start", "open", "high", "low", "close"] + (["volume"] if args.volume else [])

    return {
        "task": "reversal_zones",
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
        "events": upcoming_events(ticker, now, preset["event_days"], args.event),
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
