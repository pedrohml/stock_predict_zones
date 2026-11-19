#!/usr/bin/env python3
"""Where the candles come from: Yahoo Finance made dependable, a local cache, row checks and CSV files.

    python data_source.py info                 # where the cache is and what it holds
    python data_source.py clear [TICKER ...]   # delete the cache, or only these tickers

build_prompt.py, backtest.py and score_projections.py call this module for every price
request; they create the yfinance Ticker and keep their own error messages. What it adds:

- fetch(): one Yahoo request with a timeout, retried with exponential backoff and jitter on
  transient failures (HTTP 429, 5xx, timeouts, connection errors) inside a bounded total time.
  An unknown symbol or a range without data is not retried: it comes back as an empty frame,
  so the caller prints its usual message.
- throttle(): a minimum spacing between requests, shared by every process on the machine
  through a locked file in the cache folder (so `zones batch --jobs N` does not burst Yahoo).
- candles(): fetch() plus validate() plus the cache. Only finished daily, weekly and monthly
  candles are cached; every call still asks Yahoo for the candles since a few cached ones,
  checks that those overlap exactly (a split or a revision since then discards the cache)
  and takes everything recent, the forming candle included, from that fresh answer.
- validate(): duplicated or unsorted timestamps, missing or non-positive prices and OHLC
  that do not fit are dropped or fixed; long gaps and split-like jumps are reported. Every
  action comes back as a list of checks for the provenance and as one warning line.
- load_prices(): candles from CSV files (licensed or own data) instead of Yahoo.

Settings, all optional: ZONES_CACHE_DIR (default $XDG_CACHE_HOME/zones, else ~/.cache/zones),
ZONES_YAHOO_TIMEOUT (seconds per request, default 30), ZONES_YAHOO_SPACING (seconds between
two requests from any process, default 0.5). README "Data sources" documents all of it.
"""
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import pandas as pd

CACHE_ENV = "ZONES_CACHE_DIR"
TIMEOUT_ENV = "ZONES_YAHOO_TIMEOUT"
SPACING_ENV = "ZONES_YAHOO_SPACING"
DEFAULT_TIMEOUT = 30.0      # seconds one request may take before it is abandoned and retried
DEFAULT_SPACING = 0.5       # seconds between two requests, across all processes

ATTEMPTS = 5                # one request and up to four retries
BUDGET = 120.0              # seconds: no retry starts once the waits would go past this
BACKOFF = 2.0               # first wait after a timeout or a 5xx; doubles each time
BACKOFF_RATE_LIMITED = 5.0  # first wait after an HTTP 429
BACKOFF_CAP = 30.0          # no single wait is longer

CACHE_VERSION = 1
CACHED_INTERVALS = ("1d", "1wk", "1mo")   # intraday candles are never cached (Yahoo revises them)
ADJUSTMENT = "split"        # auto_adjust=False: split-adjusted, not dividend-adjusted
OVERLAP = 5                 # cached candles asked for again and compared on every use
GRACE = pd.Timedelta(days=1)   # a candle is cached only once it ended this long ago
SPAN = {"15m": pd.Timedelta(minutes=15), "1h": pd.Timedelta(hours=1), "1d": pd.DateOffset(days=1),
        "1wk": pd.DateOffset(days=7), "1mo": pd.DateOffset(months=1)}
PRICES = ["Open", "High", "Low", "Close"]

# validate(): a gap longer than this between two candles is reported; a close-to-close move
# of at least JUMP (or 1/JUMP) within SPLIT_TOLERANCE of a whole ratio looks like a split
GAP = {"15m": pd.Timedelta(days=5), "1h": pd.Timedelta(days=5), "1d": pd.Timedelta(days=10),
       "1wk": pd.Timedelta(days=22), "1mo": pd.Timedelta(days=65)}
JUMP = 1.8
SPLIT_TOLERANCE = 0.04
MAX_BAD_SHARE = 0.2         # more dropped rows than this share: the data is not used at all

sleep = time.sleep          # tests replace these two
now_utc = lambda: pd.Timestamp.now(tz="UTC")  # noqa: E731


class DataError(Exception):
    """Prices that cannot be had: Yahoo kept failing, or a CSV file is unusable. One line."""


def warn(text):
    print(f"Warning: {text}", file=sys.stderr)


def env_number(name, default):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


# ---- where the cache lives -------------------------------------------------------

def cache_root():
    """$ZONES_CACHE_DIR, else $XDG_CACHE_HOME/zones, else ~/.cache/zones on every platform.
    `zones` sets ZONES_CACHE_DIR from the config files' `cache_dir`."""
    named = os.environ.get(CACHE_ENV)
    if named:
        return Path(named).expanduser().absolute()
    base = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(base) if os.path.isabs(base) else Path.home() / ".cache"   # the XDG rule: ignore a relative one
    return base / "zones"


def safe_name(symbol):
    return re.sub(r"[^A-Za-z0-9._^=-]", "_", str(symbol).upper()).strip(".") or "_"


def entry_path(symbol, interval, actions):
    """One file per key: symbol, interval, adjustment, and whether splits were asked for."""
    name = f"{interval}-{ADJUSTMENT}" + ("-actions" if actions else "") + ".json"
    return cache_root() / "candles" / safe_name(symbol) / name


# ---- the rate limit shared by every process ----------------------------------------

def _lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def throttle():
    """Wait for this process's turn: at most one Yahoo request per ZONES_YAHOO_SPACING seconds
    from all processes together. The file `yahoo.next` in the cache folder holds the earliest
    time the next request may start; it is read and moved on under an OS file lock (released
    when a process dies, so nothing goes stale), and the wait happens after the lock is let go.
    Returns the seconds waited. Any problem with the file means no wait, never a failure."""
    spacing = env_number(SPACING_ENV, DEFAULT_SPACING)
    if spacing <= 0:
        return 0.0
    path = cache_root() / "yahoo.next"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+b") as handle:
            _lock(handle)
            try:
                handle.seek(0)
                try:
                    next_at = float(handle.read(64).decode("ascii") or 0)
                except (UnicodeDecodeError, ValueError):
                    next_at = 0.0
                now = time.time()
                if not math.isfinite(next_at) or next_at - now > 60 + 100 * spacing:
                    next_at = now          # a bad file or a clock change: start again
                slot = max(now, next_at)
                handle.seek(0)
                handle.truncate()
                handle.write(f"{slot + spacing:.6f}".encode("ascii"))
                handle.flush()
            finally:
                _unlock(handle)
    except OSError:
        return 0.0
    wait = slot - now
    if wait > 0:
        sleep(wait)
    return max(wait, 0.0)


# ---- one request, retried --------------------------------------------------------

RATE_LIMITED = re.compile(r"too many requests|rate[ -]?limit|\b429\b", re.I)
TRANSIENT_TEXT = re.compile(r"timed? ?out|timeout|temporar|unavailable|internal server error|bad gateway|"
                            r"gateway|status_code = 5\d\d|right back|try again|connection|reset by peer", re.I)
TRANSIENT_NAMES = ("Timeout", "Connection", "DNS", "Proxy", "SSL", "Curl", "ChunkedEncoding",
                   "RemoteDisconnected", "IncompleteRead", "JSONDecode", "ProtocolError")


def classify(error):
    """What a failed Yahoo call means: "rate_limited" and "transient" are retried, "missing"
    (unknown symbol, delisted, no candles in the range: Yahoo answered) becomes an empty frame,
    "other" (a bug, a bad argument) is raised as it is."""
    name, text = type(error).__name__, str(error)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if name == "YFRateLimitError" or status == 429 or RATE_LIMITED.search(text):
        return "rate_limited"
    if isinstance(status, int):
        return "transient" if status >= 500 else "missing" if status >= 400 else "other"
    if name in ("YFTzMissingError", "YFPricesMissingError", "YFTickerMissingError"):
        return "transient" if TRANSIENT_TEXT.search(text) else "missing"
    if name == "YFDataException" or isinstance(error, (TimeoutError, ConnectionError)):
        return "transient"
    if any(part in name for part in TRANSIENT_NAMES):
        return "transient"
    if isinstance(error, OSError):     # socket errors
        return "transient"
    return "other"


def backoff(attempt, kind):
    """Seconds to wait after failed attempt number `attempt`: exponential, capped, with
    jitter (a uniform draw from half to all of it), so parallel runs do not retry in step."""
    base = BACKOFF_RATE_LIMITED if kind == "rate_limited" else BACKOFF
    full = min(BACKOFF_CAP, base * 2 ** (attempt - 1))
    return full * random.uniform(0.5, 1.0)


class Worker:
    """One long-lived daemon thread that makes every Yahoo call of the process. yfinance's
    HTTP session keeps per-thread state, and a call from a thread it has not seen pays the
    whole cookie handshake again (about 5 s, measured), so the thread is reused; it is
    replaced only after a call was abandoned and may still be running."""

    def __init__(self):
        import queue
        self.jobs = queue.Queue()
        threading.Thread(target=self.loop, name="yahoo-requests", daemon=True).start()

    def loop(self):
        while True:
            function, box, done = self.jobs.get()
            try:
                box["value"] = function()
            except BaseException as error:   # noqa: BLE001 - handed to the caller as it is
                box["error"] = error
            done.set()


_worker = None


def call(function, seconds=None):
    """function() on the Yahoo worker thread, abandoned after `seconds` (None: no limit).
    yfinance's own timeout covers one HTTP request, while a history() call can make several
    (cookie, crumb, timezone). build_prompt.py asks for the calendar through it too."""
    global _worker
    if _worker is None:
        _worker = Worker()
    box, done = {}, threading.Event()
    _worker.jobs.put((function, box, done))
    if not done.wait(seconds):
        _worker = None       # still busy with the abandoned call: the next one gets a new thread
        raise TimeoutError(f"no answer within {seconds:g} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def call_with_deadline(function, seconds):
    return call(function, seconds or None)


def accepts(function, name):
    """Does `function` take the keyword `name` explicitly? (Real yfinance does; test fakes
    that take **kwargs record every keyword, so they are only given what they always got.)"""
    import inspect
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def configure_yfinance():
    """Make yfinance raise its errors instead of logging them and returning an empty frame,
    so a timeout or a 5xx can be told from an unknown symbol, and keep its log quiet."""
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    module = sys.modules.get("yfinance")
    config = getattr(module, "config", None)
    debug = getattr(config, "debug", None)
    if debug is not None:
        try:
            debug.hide_exceptions = False
        except Exception:   # noqa: BLE001 - an older yfinance without this switch
            pass


def describe(error):
    text = " ".join(str(error).split()) or "no message"
    return f"{type(error).__name__}: {text}"[:160]


def fetch(handle, interval, *, period=None, start=None, actions=False, what=None):
    """handle.history() for one interval, as dependable as it can be made -> a DataFrame,
    empty when Yahoo says there is no such data. Waits its turn (throttle()), gives each
    attempt ZONES_YAHOO_TIMEOUT seconds, and retries transient failures up to ATTEMPTS times
    within BUDGET seconds; then raises DataError. The keywords are the ones the scripts always
    passed (period or start, interval, auto_adjust=False, actions), plus timeout when the
    handle's history() takes it."""
    configure_yfinance()
    kwargs = {"period": period} if period is not None else {"start": start}
    kwargs.update(interval=interval, auto_adjust=False, actions=actions)
    timeout = env_number(TIMEOUT_ENV, DEFAULT_TIMEOUT)
    if timeout and accepts(handle.history, "timeout"):
        kwargs["timeout"] = timeout
    what = what or interval
    began, attempt = time.monotonic(), 0
    while True:
        attempt += 1
        throttle()
        try:
            frame = call_with_deadline(lambda: handle.history(**kwargs), timeout)
            return frame if frame is not None else pd.DataFrame()
        except Exception as error:   # noqa: BLE001 - sorted by classify()
            kind = classify(error)
            if kind == "missing":
                return pd.DataFrame()
            if kind == "other":
                raise
            spent = time.monotonic() - began
            delay = backoff(attempt, kind)
            if attempt >= ATTEMPTS or spent + delay > BUDGET:
                raise DataError(f"Yahoo kept failing for {what} ({describe(error)}); gave up after "
                                f"{attempt} attempt(s) in {spent:.0f} s")
            warn(f"Yahoo, {what}: {describe(error)}; retry {attempt} of {ATTEMPTS - 1} in {delay:.1f} s")
            sleep(delay)


# ---- row checks --------------------------------------------------------------------

def stamp(when, interval):
    return when.strftime("%Y-%m-%dT%H:%M" if interval in ("15m", "1h") else "%Y-%m-%d")


def check(issue, action, rows, where, interval):
    entry = {"issue": issue, "action": action, "rows": int(len(rows) if not isinstance(rows, int) else rows)}
    if len(where):
        entry["first"] = stamp(where[0], interval)
    return entry


def validate(frame, interval, what=None, fail=True):
    """Clean candles -> (frame, checks). Applied to every frame from Yahoo, the cache's
    included, and to CSV files. What happens to each issue:

    unsorted times            sorted (fixed)
    missing price (NaN)       row dropped
    duplicated time           earlier rows dropped, the last one kept (Yahoo repeats the forming row)
    price <= 0                row dropped
    high < low                high and low swapped, then as below (fixed)
    open/close outside range  high and low widened to the OHLC extremes (fixed)
    long gap                  kept, reported
    split-like jump           kept, reported: a 2:1 move may be real, but check the data
    too many bad rows         DataError when more than MAX_BAD_SHARE of the rows were dropped

    `checks` lists the issues found, each {"issue", "action", "rows", "first"}; empty when the
    frame was clean. Volume is never changed (a missing volume is written as 0 later)."""
    if frame is None or frame.empty:
        return (frame if frame is not None else pd.DataFrame()), []
    checks = []
    total = len(frame)
    if not frame.index.is_monotonic_increasing:
        frame = frame.sort_index(kind="stable")
        checks.append({"issue": "unsorted", "action": "sorted", "rows": total})
    missing = frame[PRICES].isna().any(axis=1)
    if missing.any():
        checks.append(check("missing_price", "dropped", int(missing.sum()), frame.index[missing], interval))
        frame = frame[~missing]
    repeated = frame.index.duplicated(keep="last")
    if repeated.any():
        checks.append(check("duplicate_time", "dropped", int(repeated.sum()), frame.index[repeated], interval))
        frame = frame[~repeated]
    bad = (frame[PRICES] <= 0).any(axis=1)
    if bad.any():
        checks.append(check("non_positive_price", "dropped", int(bad.sum()), frame.index[bad], interval))
        frame = frame[~bad]
    dropped = total - len(frame)
    if fail and dropped > 3 and dropped > MAX_BAD_SHARE * total:
        raise DataError(f"{what or interval}: {dropped} of {total} rows had missing, repeated or "
                        "non-positive prices; not using this data")
    if frame.empty:
        return frame, checks

    frame = frame.copy()
    inverted = frame["High"] < frame["Low"]
    if inverted.any():
        checks.append(check("high_below_low", "swapped", int(inverted.sum()), frame.index[inverted], interval))
        high = frame["High"].copy()
        frame.loc[inverted, "High"] = frame.loc[inverted, "Low"]
        frame.loc[inverted, "Low"] = high[inverted]
    top, bottom = frame[PRICES].max(axis=1), frame[PRICES].min(axis=1)
    outside = (frame["High"] < top) | (frame["Low"] > bottom)
    if outside.any():
        checks.append(check("ohlc_outside_range", "widened", int(outside.sum()), frame.index[outside], interval))
        frame["High"], frame["Low"] = top, bottom

    if len(frame) > 1:
        gaps = pd.Series(frame.index[1:] - frame.index[:-1], index=frame.index[1:])
        long = gaps[gaps > GAP.get(interval, pd.Timedelta(days=10))]
        if len(long):
            entry = check("gap", "kept", len(long), long.index, interval)
            entry["longest_days"] = round(long.max() / pd.Timedelta(days=1), 1)
            checks.append(entry)
        close = frame["Close"].to_numpy()
        ratio = close[1:] / close[:-1]
        suspect = []
        for index, r in enumerate(ratio):
            big = r if r >= 1 else 1 / r
            if big >= JUMP and abs(big - round(big)) <= SPLIT_TOLERANCE * big:
                suspect.append(frame.index[index + 1])
        if suspect:
            checks.append(check("split_like_jump", "kept", len(suspect), suspect, interval))
    if checks and what:
        warn(f"{what}: " + "; ".join(summary(c) for c in checks))
    return frame, checks


WORDS = {"unsorted": "row(s) out of time order", "missing_price": "row(s) with a missing price",
         "duplicate_time": "row(s) with a repeated time", "non_positive_price": "row(s) with a price of zero or less",
         "high_below_low": "row(s) with high below low", "ohlc_outside_range": "row(s) with open or close outside high-low",
         "gap": "gap(s) in the data", "split_like_jump": "close-to-close move(s) that look like an "
         "unadjusted split (check the data)"}


def summary(entry):
    where = f" (first {entry['first']})" if entry.get("first") else ""
    return f"{entry['action']} {entry['rows']} {WORDS.get(entry['issue'], entry['issue'])}{where}"


# ---- the cache ---------------------------------------------------------------------

def finished(index, interval, now):
    """Which candles have ended at least GRACE before `now`: only those are cached."""
    span = SPAN[interval]
    ends = pd.DatetimeIndex([when + span for when in index])
    return ends + GRACE <= now


def iso(when):
    return when.isoformat()


def load_entry(path, interval, actions):
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        warn(f"ignoring an unreadable cache file {path} ({type(error).__name__})")
        return None
    if (not isinstance(entry, dict) or entry.get("version") != CACHE_VERSION
            or entry.get("interval") != interval or entry.get("actions") != actions
            or entry.get("adjustment") != ADJUSTMENT or not entry.get("rows")):
        return None
    return entry


def entry_frame(entry):
    """The cached rows -> (frame in the entry's timezone, download times as epoch seconds)."""
    columns = entry["columns"]
    rows = entry["rows"]
    index = pd.DatetimeIndex(pd.to_datetime([row[0] for row in rows], utc=True)).tz_convert(entry["tz"])
    frame = pd.DataFrame([row[1:1 + len(columns)] for row in rows], index=index, columns=columns, dtype=float)
    return frame, [row[-1] for row in rows]


def store(path, symbol, interval, actions, frame, downloaded, requested_from, splits, now):
    """Write the finished candles of `frame` (validated, in exchange time) atomically."""
    keep = finished(frame.index, interval, now)
    if not keep.any() or frame.index.tz is None:
        return
    columns = [c for c in PRICES + ["Volume"] if c in frame]
    rows = []
    for (when, candle), at in zip(frame[keep][columns].iterrows(), [d for d, k in zip(downloaded, keep) if k]):
        rows.append([iso(when)] + [None if pd.isna(candle[c]) else float(candle[c]) for c in columns] + [at])
    entry = {"version": CACHE_VERSION, "symbol": symbol.upper(), "interval": interval,
             "adjustment": ADJUSTMENT, "actions": actions, "tz": str(frame.index.tz),
             "from": requested_from, "verified_at": iso(now), "columns": columns,
             "splits": splits, "rows": rows}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        draft = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        draft.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        os.replace(draft, path)        # readers see the old file or the new one, never half
    except OSError as error:
        warn(f"could not write the cache file {path} ({type(error).__name__}: {error})")


def period_offset(period):
    """A Yahoo period ("5d", "6mo", "2y") -> a pandas offset."""
    for suffix, unit in (("mo", "months"), ("d", "days"), ("y", "years"), ("wk", "weeks")):
        if period.endswith(suffix) and period[:-len(suffix)].isdigit():
            return pd.DateOffset(**{unit: int(period[:-len(suffix)])})
    raise ValueError(f"unknown period {period}")


def split_list(frame):
    if frame is None or frame.empty or "Stock Splits" not in frame:
        return []
    return [[iso(when), float(ratio)] for when, ratio in frame["Stock Splits"].items()
            if pd.notna(ratio) and ratio != 0]


def same_prices(cached, fresh):
    """Do the overlapping candles agree? Prices only: Yahoo revises recent volumes."""
    if not set(cached.index) <= set(fresh.index):
        return False
    a, b = cached[PRICES].to_numpy(), fresh.loc[cached.index, PRICES].to_numpy()
    return bool((abs(a - b) <= 1e-6 * abs(a).clip(min=1)).all())


def candles(handle, symbol, interval, *, period=None, start=None, actions=False, cache=True, what=None):
    """Validated candles of one interval -> (frame, report).

    frame: Yahoo's columns, sorted, unique, in Yahoo's (exchange) timezone. report:
    {"checks": [...], "splits": [[iso time, ratio]], "cache": None or {"rows", "downloaded_from",
    "downloaded_to", "verified_at"}, "note": text or None}.

    With `cache` and a daily, weekly or monthly interval, finished candles come from the cache
    when it covers the request, and Yahoo is asked only for the candles since the OVERLAP-th
    newest cached one. Those must match the cache to the cent, or the cache is replaced by a
    full download (a split rescales the whole history). The newest candles, the forming one
    included, always come from that fresh answer. `period` is counted back from now, `start`
    is a date (YYYY-MM-DD); exactly one is given."""
    what = what or f"{symbol} {interval}"
    report = {"checks": [], "splits": [], "cache": None, "note": None}
    use_cache = cache and interval in CACHED_INTERVALS
    now = now_utc()
    if start is not None:
        needed = pd.Timestamp(start)
    else:
        needed = (now.tz_localize(None).normalize() - period_offset(period))
    path = entry_path(symbol, interval, actions) if use_cache else None
    entry = load_entry(path, interval, actions) if use_cache else None
    if entry and pd.Timestamp(entry["from"]) <= needed:
        cached, downloaded = entry_frame(entry)
        overlap = cached.iloc[-OVERLAP:]
        since = overlap.index[0]
        raw = fetch(handle, interval, start=since.strftime("%Y-%m-%d"), actions=actions, what=what)
        fresh, checks = validate(raw, interval, what)
        if not fresh.empty and fresh.index.tz is not None:
            fresh = fresh.tz_convert(entry["tz"])
            fresh = fresh[fresh.index >= since]
        if not fresh.empty and same_prices(overlap, fresh):
            older = cached.index < since
            frame = pd.concat([cached[older].reindex(columns=fresh.columns), fresh])
            for column, kind in fresh.dtypes.items():     # Yahoo's integer volume stays an integer
                if kind.kind in "iu" and not frame[column].isna().any():
                    frame[column] = frame[column].astype(kind)
            known = dict(zip(cached.index, downloaded))   # overlap rows keep their first download time
            times = [known.get(when, round(now.timestamp())) for when in frame.index]
            splits = sorted({tuple(s) for s in entry.get("splits", []) + split_list(raw)})
            store(path, symbol, interval, actions, frame, times, entry["from"], [list(s) for s in splits], now)
            local_needed = needed.tz_localize(frame.index.tz) if needed.tzinfo is None else needed
            keep = frame.index >= local_needed
            frame, times = frame[keep], [t for t, k in zip(times, keep) if k]
            from_cache = [t for when, t in zip(frame.index, times) if when < since]
            report.update(checks=checks, splits=[s for s in splits if pd.Timestamp(s[0]) >= local_needed])
            if from_cache:
                stamps = [pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
                          for t in (min(from_cache), max(from_cache))]
                report["cache"] = {"rows": len(from_cache), "downloaded_from": stamps[0],
                                   "downloaded_to": stamps[1],
                                   "verified_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            return frame, report
        if not fresh.empty:
            report["note"] = "the cached candles no longer matched Yahoo's (a split or a revision): downloaded again"
            warn(f"{what}: {report['note']}")

    raw = fetch(handle, interval, period=period, start=start, actions=actions, what=what)
    frame, checks = validate(raw, interval, what)
    report.update(checks=checks, splits=split_list(raw))
    if use_cache and not frame.empty:
        requested_from = needed.strftime("%Y-%m-%d")
        store(path, symbol, interval, actions, frame, [round(now.timestamp())] * len(frame),
              requested_from, report["splits"], now)
    return frame, report


# ---- managing the cache --------------------------------------------------------------

def cache_info():
    """[(symbol, file name, rows, first, last, verified_at)] for every cache file."""
    found = []
    base = cache_root() / "candles"
    for path in sorted(base.glob("*/*.json")) if base.is_dir() else []:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            rows = entry["rows"]
            found.append((path.parent.name, path.name, len(rows), rows[0][0][:10], rows[-1][0][:10],
                          entry.get("verified_at", "?")[:19]))
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            found.append((path.parent.name, path.name, 0, "?", "?", "unreadable"))
    return found


def clear(symbols=None):
    """Delete the cached candles (all, or only these symbols) -> the folders removed."""
    base = cache_root() / "candles"
    if not base.is_dir():
        return []
    targets = [base / safe_name(s) for s in symbols] if symbols else \
        [path for path in base.iterdir() if path.is_dir()]
    removed = []
    for folder in targets:
        if folder.is_dir():
            shutil.rmtree(folder)
            removed.append(folder)
    return removed


# ---- CSV files -------------------------------------------------------------------------

TIME_COLUMNS = ("date", "datetime", "time", "timestamp", "start")
INTERVAL_NAMES = {"15m": "15m", "1h": "1h", "60m": "1h", "1d": "1d", "d": "1d", "daily": "1d",
                  "1wk": "1wk", "1w": "1wk", "w": "1wk", "weekly": "1wk", "1mo": "1mo", "1m": "1mo",
                  "monthly": "1mo"}
DERIVED = {"1wk": "1d", "1mo": "1d"}    # what can be built from what


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def interval_from_name(path):
    """`1d.csv`, `PETR4_1wk.csv`, `petr4-daily.csv` -> the interval; None when the name has none.
    Matched without regard to case, except that 1m is monthly and 1M never means minutes."""
    words = re.split(r"[_\-. ]+", Path(path).stem)
    return INTERVAL_NAMES.get(words[-1].lower()) if words else None


def interval_from_times(index, path):
    """Infer the interval from the median spacing of the timestamps."""
    if len(index) < 3:
        raise DataError(f"{path}: too few rows to tell the interval; name the file 1d.csv, 1wk.csv ...")
    step = pd.Series(index[1:] - index[:-1]).median()
    if step == pd.Timedelta(minutes=15):
        return "15m"
    if step == pd.Timedelta(hours=1):
        return "1h"
    if pd.Timedelta(days=1) <= step <= pd.Timedelta(days=4):
        return "1d"
    if pd.Timedelta(days=6) <= step <= pd.Timedelta(days=8):
        return "1wk"
    if pd.Timedelta(days=27) <= step <= pd.Timedelta(days=32):
        return "1mo"
    raise DataError(f"{path}: cannot tell the interval from rows {step} apart; name the file "
                    "15m.csv, 1h.csv, 1d.csv, 1wk.csv or 1mo.csv")


def read_csv(path, tz):
    """One CSV file -> a frame like Yahoo's (Open High Low Close [Volume], indexed in `tz`).
    Header required; columns in any order and letter case; extra columns ignored. Times are
    ISO dates or date-times; without an offset they are taken as exchange time `tz`."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise DataError(f"cannot read {path}: {error}")
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise DataError(f"{path} is empty")
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(lines, dialect)
    header = [name.strip().lower() for name in next(reader)]
    where = {name: header.index(name) for name in header}
    stamp_column = next((where[name] for name in TIME_COLUMNS if name in where), None)
    absent = [name for name in ("open", "high", "low", "close") if name not in where]
    if stamp_column is None or absent:
        raise DataError(f"{path}: needs the columns date, open, high, low, close (and optionally volume); "
                        f"found {', '.join(header)}")
    columns = [("Open", where["open"]), ("High", where["high"]), ("Low", where["low"]),
               ("Close", where["close"])]
    if "volume" in where:
        columns.append(("Volume", where["volume"]))
    times, rows = [], []
    for number, row in enumerate(reader, 2):
        if not any(cell.strip() for cell in row):
            continue
        try:
            when = pd.Timestamp(row[stamp_column].strip())
            values = [float(row[i]) if row[i].strip() else float("nan") for _, i in columns]
        except (ValueError, IndexError) as error:
            raise DataError(f"{path}, data row at line {number}: {error}")
        times.append(when.tz_localize(tz) if when.tzinfo is None else when.tz_convert(tz))
        rows.append(values)
    if not rows:
        raise DataError(f"{path} has a header but no rows")
    return pd.DataFrame(rows, index=pd.DatetimeIndex(times), columns=[name for name, _ in columns])


def resample(frame, interval):
    """Daily candles -> weekly (labelled by the week's Monday) or monthly (by the 1st), the
    way Yahoo labels them and score_projections.aggregate() builds them."""
    days = frame.index.tz_localize(None).normalize()
    if interval == "1wk":
        label = days - pd.to_timedelta(days.day_of_week, unit="D")
    else:
        label = days.to_period("M").to_timestamp()
    groups = frame.groupby(pd.DatetimeIndex(label).tz_localize(frame.index.tz))
    out = pd.DataFrame({"Open": groups["Open"].first(), "High": groups["High"].max(),
                        "Low": groups["Low"].min(), "Close": groups["Close"].last()})
    if "Volume" in frame:
        out["Volume"] = groups["Volume"].sum(min_count=1)
    return out


class Prices:
    """Candles from CSV files for one ticker. `frame(interval)` builds weekly and monthly ones
    from daily ones when no file gives them. `files` and `built` feed the provenance."""

    def __init__(self, frames, files, meta, checks):
        self.frames, self.files, self.meta, self.checks = frames, files, meta, checks
        self.built = {}

    def frame(self, interval):
        if interval not in self.frames and DERIVED.get(interval) in self.frames:
            self.frames[interval] = resample(self.frames[DERIVED[interval]], interval)
            self.built[interval] = DERIVED[interval]
            self.checks[interval] = list(self.checks.get(DERIVED[interval], []))
        if interval not in self.frames:
            have = ", ".join(sorted(self.frames)) or "none"
            hint = " or daily candles to build them from" if interval in DERIVED else ""
            raise DataError(f"the CSV data has no {interval} candles (it has: {have}); "
                            f"add a {interval}.csv file{hint}")
        return self.frames[interval]


def find_files(path, ticker):
    """--prices PATH -> (CSV files, metadata file or None). PATH is a file, or a folder that
    holds, looked up in this order: a subfolder named as the ticker with one file per
    interval, a file TICKER.csv, or one file per interval (1d.csv, 1wk.csv ...)."""
    path = Path(path).expanduser()
    if path.is_file():
        meta = path.with_suffix(".json")
        return [path], meta if meta.is_file() else None
    if not path.is_dir():
        raise DataError(f"--prices: {path} is neither a CSV file nor a folder")
    wanted = str(ticker).upper()
    for child in sorted(path.iterdir()):
        if child.is_dir() and child.name.upper() == wanted:
            return find_files(child, ticker) if any(child.glob("*.csv")) else ([], None)
    for child in sorted(path.iterdir()):
        if child.is_file() and child.suffix.lower() == ".csv" and child.stem.upper() == wanted:
            meta = child.with_suffix(".json")
            return [child], meta if meta.is_file() else (path / "meta.json" if (path / "meta.json").is_file() else None)
    files = [child for child in sorted(path.iterdir())
             if child.is_file() and child.suffix.lower() == ".csv" and interval_from_name(child)]
    if not files:
        raise DataError(f"--prices: no CSV for {wanted} in {path} (looked for {wanted}/, {wanted}.csv, "
                        "and files named by interval such as 1d.csv)")
    meta = path / "meta.json"
    return files, meta if meta.is_file() else None


def load_prices(path, ticker):
    """--prices PATH -> Prices. Metadata (timezone, currency, venue) comes from a JSON file:
    FILE.json beside a single FILE.csv, or meta.json in the folder. Without it the timezone is
    UTC and currency and venue are "unsure", and a warning says so."""
    files, meta_path = find_files(path, ticker)
    meta = {}
    if meta_path:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DataError(f"cannot read {meta_path}: {error}")
        if not isinstance(meta, dict):
            raise DataError(f"{meta_path} must hold a JSON object such as "
                            '{"timezone": "America/Sao_Paulo", "currency": "BRL", "venue": "B3"}')
    tz = meta.get("timezone") or "UTC"
    try:
        pd.Timestamp("2000-01-01", tz=tz)
    except Exception:   # noqa: BLE001 - pandas raises several kinds for a bad zone name
        raise DataError(f"{meta_path}: unknown timezone '{tz}' (use an IANA name such as America/New_York)")
    if not meta.get("timezone"):
        warn(f"no timezone given for the CSV data (add {'meta.json' if len(files) > 1 else 'FILE.json'} "
             'with {"timezone": ...}); times without an offset are taken as UTC')
    frames, records, checks = {}, [], {}
    for file in files:
        raw = read_csv(file, tz)
        interval = interval_from_name(file) or interval_from_times(raw.index.sort_values(), file)
        if interval in frames:
            raise DataError(f"two CSV files give {interval} candles: {file} and another")
        frame, found = validate(raw, interval, f"{file.name} ({interval})")
        if frame.empty:
            raise DataError(f"{file}: no usable rows")
        frames[interval], checks[interval] = frame, found
        records.append({"path": str(file), "sha256": sha256_file(file), "interval": interval,
                        "rows": len(frame)})
    if meta_path:
        records.append({"path": str(meta_path), "sha256": sha256_file(meta_path), "role": "metadata"})
    return Prices(frames, records, {"timezone": tz, "currency": meta.get("currency") or "unsure",
                                    "venue": meta.get("venue") or "unsure"}, checks)


# ---- command line ----------------------------------------------------------------------

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="The local candle cache (Yahoo Finance downloads).")
    parser.add_argument("command", choices=("info", "clear", "path"),
                        help="info: what the cache holds; clear: delete it; path: print its folder")
    parser.add_argument("tickers", nargs="*", metavar="TICKER", help="clear: only these tickers")
    args = parser.parse_args(argv)
    root = cache_root()
    if args.command == "path":
        print(root)
        return 0
    if args.command == "clear":
        removed = clear(args.tickers or None)
        print(f"Removed {len(removed)} ticker folder(s) from {root / 'candles'}" if removed
              else f"Nothing to remove under {root / 'candles'}")
        return 0
    found = cache_info()
    print(f"Cache: {root}")
    if not found:
        print("  (empty)")
    for symbol, name, rows, first, last, verified in found:
        print(f"  {symbol:<12} {name:<24} {rows:>6} rows  {first} to {last}  checked {verified}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
