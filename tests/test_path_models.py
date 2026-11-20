"""The setups' path models: paths() (the rules) and garch_paths() (evaluated for rules 4, not adopted),
and the outputs neither may change: rz.py's baseline, mechanical.py and backtest.py, pinned by hash.

The pins were taken with the locked numpy (uv.lock); a numpy upgrade that changes its random
streams or float formatting would move them for a reason unrelated to this project."""
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

import backtest as bt
import mechanical
import trade_entries
from test_backtest import DATA, ROWS

EXAMPLES = Path(trade_entries.__file__).resolve().parent / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def example():
    return (json.loads((EXAMPLES / "petr4_input.json").read_text(encoding="utf-8")),
            json.loads((EXAMPLES / "petr4_projection.json").read_text(encoding="utf-8")))


def kurtosis(x):
    x = x - x.mean()
    return float((x ** 4).mean() / (x ** 2).mean() ** 2)


# ---- byte-identical outputs ------------------------------------------------------------

def test_rz_baseline_is_unchanged(example):
    """rz.py sim's numbers at 200,000 paths, both sim methods: walk()'s seed and draws untouched."""
    lib = trade_entries.rz()
    proj = example[1]
    zones = {side: [z["zone"] for z in proj[side] if None not in z["zone"]] for side in ("supports", "resistances")}
    loaded = lib.load(EXAMPLES / "petr4_input.json")
    got = {m: sha(json.dumps(lib.baseline(loaded, zones, method=m), sort_keys=True)) for m in (1, 2)}
    assert got == {1: "90ae0bde26e82b7e50dfc55ec2e2bc1fbbea6e5e2d830358fb85ce9a452efa9d",
                   2: "43e71b8c20709db1ad8798b4a6bdc3ba0505a816ffd532afe0e095ccf371a882"}


def test_mechanical_projection_is_unchanged():
    text = mechanical.render(mechanical.build(json.loads((FIXTURES / "input.json").read_text(encoding="utf-8"))))
    assert sha(text) == "4be9413a8be0c3fe84fc0204d1d4565880d6cae9da955a7748de64c7cce72a0f"


def test_backtest_report_is_unchanged(monkeypatch):
    monkeypatch.setattr(bt, "download", lambda ticker, start: {**DATA, "rows": [r for r in ROWS if r[0] >= start.isoformat()]})
    report = bt.run(["AAA"], "1-3m", date(2024, 1, 1), date(2024, 6, 1), paths=2000, today=date(2026, 11, 19),
                    progress=False)
    assert sha(bt.render(report)) == "e99c34a731e47a3cd26afb050a41ca3c799878c3f89d9861abdc1d2272698f80"


def test_the_rules_never_use_garch_paths(example, monkeypatch):
    """garch_paths() was not adopted: setups(), build() and check stay on paths()."""
    before = trade_entries.setups(*example)
    monkeypatch.setattr(trade_entries, "garch_paths", lambda *a, **k: pytest.fail("garch_paths was called"))
    assert trade_entries.setups(*example) == before
    assert trade_entries.problems(*example, trade_entries.build(*example)) == []


# ---- garch_paths(): the evaluated model -------------------------------------------------

def test_garch_paths_are_reproducible_and_leave_walk_alone(example):
    source = example[0]
    lib = trade_entries.rz()
    d, main, t, h, l, c = lib.unpack(source)
    walked = lib.walk(d, main, t, c, {}, 1000, lib.drift_of(main))[0]
    one, two = trade_entries.garch_paths(source, count=2000), trade_entries.garch_paths(source, count=2000)
    assert all(np.array_equal(one[key], two[key]) for key in ("high", "low", "last"))
    assert np.array_equal(walked, lib.walk(d, main, t, c, {}, 1000, lib.drift_of(main))[0])
    assert (one["high"] >= one["low"]).all()
    assert one["steps"] == trade_entries.paths(source)["steps"]          # the same trading days as the rules' paths


def test_garch_paths_keep_the_projections_variance(example):
    """The mean of the GARCH factor stays 1, so the variance of the log price at the end is the sum
    of the schedule's variances (what walk() would give without its fat tails), with or without
    clustering and fat tails."""
    source = example[0]
    lib = trade_entries.rz()
    d, main, t, h, l, c = lib.unpack(source)
    _, dates, frac, v, sub = lib.walk(d, main, t, c, {}, 1)
    sigma = np.linspace(v["assumed"][0], v["assumed"][1], len(dates))
    sigma[0] *= np.sqrt(frac)
    target = float((sigma ** 2).sum())
    for model in (trade_entries.GARCH, {"alpha": 0.0, "beta": 0.0, "nu": None}):
        logs = np.log(trade_entries.garch_paths(source, model, count=100_000)["last"] / d["now"]["price"])
        assert logs.var() == pytest.approx(target, rel=0.05)


def test_garch_paths_have_fat_tails_and_clustering(example):
    source = example[0]
    plain = trade_entries.garch_paths(source, {"alpha": 0.0, "beta": 0.0, "nu": None}, count=50_000)
    garch = trade_entries.garch_paths(source, count=50_000)
    end = lambda sim: np.log(sim["last"])
    assert kurtosis(end(plain)) == pytest.approx(3.0, abs=0.15)          # Gaussian days, constant volatility
    assert kurtosis(end(garch)) > 4.5                                    # fat tails over the whole horizon

    def range_correlation(sim, lag=1):
        """Correlation of one day's high-low range with the next day's, across paths: a calm or
        volatile day is followed by more of the same only when volatility clusters."""
        span = sim["high"] - sim["low"]
        return float(np.mean([np.corrcoef(span[:, k], span[:, k + lag])[0, 1] for k in range(5, 40)]))
    assert abs(range_correlation(plain)) < 0.02
    assert range_correlation(garch) > 0.1
