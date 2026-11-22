"""The shorter grid of README "A fixed R target at 1-3m": the placement harness at 1-3m, as-of dates
every 20 trading days, the nine variants of the protocol.

    uv run python examples/fixed_target_2026-11-22/run.py OUT --part fit|held-out --jobs 14
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "examples", "placement_2026-11-21"))
import harness  # noqa: E402

STEP = 20
VARIANTS = (0, 3, 4, 10, 13, 14, 15, 18, 19)   # k 0.25 / 1 / 1.5 x zone / 2 R / 3 R; 0 is rules 6
harness.backtest.DEFAULT_STEP["1-3m"] = STEP

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *sys.argv[1:], "--horizon", "1-3m", "--variants", ",".join(map(str, VARIANTS))]
    harness.main()
