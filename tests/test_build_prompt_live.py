"""build_prompt.py end to end with Yahoo replaced by a deterministic fake and the clock frozen.

The expected files in tests/fixtures/build_prompt_live/ were written by the version of
build_prompt.py before it was split so that backtest.py can build an input from candles it
already has. They pin the live command line byte for byte: the split must not change it.
Regenerate them only for a deliberate change of the output:

    uv run python tests/test_build_prompt_live.py
"""
import io
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_prompt  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "build_prompt_live"
TZ = "America/Sao_Paulo"
NOW = pd.Timestamp("2026-11-17 17:36", tz=TZ)        # a Tuesday, after the close

CASES = {
    "default_1-3m": ["PETR4.SA"],
    "1-2w_volume_event": ["petr4.sa", "--horizon", "1-2w", "--volume",
                          "--event", "2026-11-20: board meeting", "--research", "none"],
    "6-12m_pt": ["PETR4.SA", "--horizon", "6-12m", "--language", "pt-BR", "--decimals", "3",
                 "--supports", "2", "--links", "all"],
    "today_capped": ["PETR4.SA", "--horizon", "today", "--max-rows", "50", "--shape", "list"],
}

PERIOD_DAYS = {"5d": 7, "3mo": 92, "6mo": 183, "2y": 731, "5y": 1827, "10y": 3653}


def candles(interval, period):
    """A deterministic frame shaped like yfinance's: tz-aware index, one NaN row, one NaN volume."""
    begin = NOW - pd.Timedelta(days=PERIOD_DAYS[period])
    if interval == "15m":
        days = [d for d in pd.date_range(begin.normalize(), NOW.normalize(), freq="D", tz=TZ) if d.weekday() < 5]
        index = [d + pd.Timedelta(hours=10, minutes=15 * i) for d in days for i in range(28)]
        index = [t for t in index if t <= NOW - pd.Timedelta(minutes=50)]
    else:
        freq = {"1d": "B", "1wk": "W-MON", "1mo": "MS"}[interval]
        index = list(pd.date_range(begin.normalize(), NOW.normalize(), freq=freq, tz=TZ))
    rows = []
    for i, _ in enumerate(index):
        close = 40 + 6 * math.sin(i / 7.3) + 2 * math.sin(i / 2.1) + i * 0.01
        open_ = close - 0.8 * math.sin(i * 1.7)
        rows.append([open_, max(open_, close) + 0.31 + 0.2 * abs(math.sin(i)),
                     min(open_, close) - 0.27 - 0.2 * abs(math.cos(i)), close, 1000 + 37 * i])
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index), columns=["Open", "High", "Low", "Close", "Volume"])
    if len(frame) > 6:
        frame.iloc[-5, 0:4] = float("nan")
        frame.iloc[-3, 4] = float("nan")
    return frame


class FakeYahooTicker:
    def __init__(self, symbol):
        self.ticker = symbol
        self.calendar = {"Earnings Date": [pd.Timestamp("2026-11-26").date()],
                         "Ex-Dividend Date": pd.Timestamp("2026-12-21").date()}

    def history(self, period, interval, auto_adjust, actions):
        assert auto_adjust is False
        return candles(interval, period)

    def get_history_metadata(self):
        epoch = lambda text: int(pd.Timestamp(text, tz=TZ).timestamp())
        return {"exchangeTimezoneName": TZ, "currency": "BRL", "fullExchangeName": "São Paulo",
                "regularMarketPrice": 44.0612,
                "currentTradingPeriod": {
                    "pre": {"start": epoch("2026-11-17 09:45"), "end": epoch("2026-11-17 10:00")},
                    "regular": {"start": epoch("2026-11-17 10:00"), "end": epoch("2026-11-17 17:00")},
                    "post": {"start": epoch("2026-11-17 17:00"), "end": epoch("2026-11-17 18:00")}}}


class FrozenPandas:
    """pandas, except that Timestamp.now() is NOW."""

    def __init__(self):
        def timestamp(*args, **kwargs):
            return pd.Timestamp(*args, **kwargs)
        timestamp.now = lambda tz=None: NOW.tz_convert(tz)
        self.Timestamp = timestamp

    def __getattr__(self, name):
        return getattr(pd, name)


def run_cli(argv, monkeypatch=None):
    """build_prompt.main() with fake Yahoo and frozen time -> what it printed."""
    fake_yf = type(sys)("yfinance")
    fake_yf.Ticker = FakeYahooTicker
    saved = build_prompt.yf, build_prompt.pd, sys.argv
    build_prompt.yf, build_prompt.pd, sys.argv = fake_yf, FrozenPandas(), ["build_prompt.py", *argv]
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            build_prompt.main()
        return out.getvalue()
    finally:
        build_prompt.yf, build_prompt.pd, sys.argv = saved


@pytest.mark.parametrize("name", CASES)
def test_live_output_is_unchanged(name):
    assert run_cli(CASES[name]) == (GOLDEN / f"{name}.json").read_text(encoding="utf-8")


def test_live_output_file_is_unchanged(tmp_path, capsys):
    target = tmp_path / "out.json"
    assert run_cli(["PETR4.SA", "-o", str(target)]) == ""
    assert target.read_text(encoding="utf-8") == (GOLDEN / "default_1-3m.json").read_text(encoding="utf-8")
    assert "Wrote" in capsys.readouterr().err


if __name__ == "__main__":
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, argv in CASES.items():
        (GOLDEN / f"{name}.json").write_text(run_cli(argv), encoding="utf-8")
        print("wrote", GOLDEN / f"{name}.json")
