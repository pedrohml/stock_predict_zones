#!/usr/bin/env python3
"""Regenerate tests/fixtures/input.json. Offline and deterministic (no RNG).

    uv run python tests/fixtures/make_input.py

The candles are synthetic: a weekly path shaped like the example in SKILL.md
(low near 29 in June 2025, high near 60 in October 2026, about 44 now), so the
example projection in projection.json sits in a compatible price range.
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import build_prompt  # noqa: E402

WEEKS = 104
LAST_WEEK = date(2026, 11, 16)          # a Monday; the candle is still forming
ANCHORS = [(0, 36.0), (28, 29.6), (60, 41.0), (99, 58.6), (101, 50.5), (103, 44.06)]


def anchor(i):
    for (i0, p0), (i1, p1) in zip(ANCHORS, ANCHORS[1:]):
        if i0 <= i <= i1:
            return p0 + (p1 - p0) * (i - i0) / (i1 - i0)
    raise ValueError(i)


def weekly_rows():
    closes = [anchor(i) + 0.9 * math.sin(i * 1.7) + 0.5 * math.sin(i * 0.61) for i in range(WEEKS)]
    closes[-1] = 44.06
    rows, prev = [], closes[0] + 0.4
    for i, close in enumerate(closes):
        start = LAST_WEEK - timedelta(days=7 * (WEEKS - 1 - i))
        high = max(prev, close) + 0.5 + 0.3 * abs(math.sin(i * 2.3))
        low = min(prev, close) - 0.5 - 0.3 * abs(math.cos(i * 1.9))
        rows.append([start.isoformat()] + [round(x, 2) for x in (prev, high, low, close)])
        prev = close
    return rows


def monthly_rows(before, count=36):
    """Monthly candles that all start before the first weekly candle."""
    months, y, m = [], before.year, before.month
    for _ in range(count):
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        months.append(date(y, m, 1))
    rows, prev = [], 24.0
    for i, start in enumerate(reversed(months)):
        close = 24.0 + 12.0 * i / (count - 1) + 1.5 * math.sin(i * 1.3)
        high, low = max(prev, close) + 1.2, min(prev, close) - 1.1
        rows.append([start.isoformat()] + [round(x, 2) for x in (prev, high, low, close)])
        prev = close
    return rows


def prompt():
    weekly = weekly_rows()
    first = date.fromisoformat(weekly[0][0])
    return {
        "task": "reversal_zones",
        "asset": {"ticker": "PETR4.SA", "venue": "São Paulo", "currency": "BRL"},
        "now": {"time": "2026-11-17T17:36-03:00", "price": 44.06, "session": "after_hours"},
        "horizon": "1-3m",
        "data": {
            "timezone": "America/Sao_Paulo",
            "split_adjusted": True,
            "columns": ["start", "open", "high", "low", "close"],
            "series": [
                {"candles": "1M", "last_row": "closed", "rows": monthly_rows(first)},
                {"candles": "1W", "last_row": "forming", "rows": weekly},
            ],
        },
        "events": [{"date": "2026-12-21", "what": "ex-dividend"}],
        "want": {"supports": 3, "resistances": 3, "probability": "close_in_zone", "close_basis": "1W"},
        "research": "one_search",
        "reply": {"shape": "table", "max_words": 200, "language": "en", "links": "decisive_only"},
    }


if __name__ == "__main__":
    target = Path(__file__).with_name("input.json")
    target.write_text(build_prompt.render(prompt()) + "\n", encoding="utf-8")
    print(f"Wrote {target}")
