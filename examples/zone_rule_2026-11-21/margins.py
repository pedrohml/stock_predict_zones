"""Zone-rule study: the criterion's tests on the fit part, per candidate, and the choice rule of
PROTOCOL.md, from analyse.py's screen.json and calib.py's calib*.json.

    uv run python examples/zone_rule_2026-11-21/margins.py DATA
"""
import glob, json, os, sys

data = sys.argv[1]
rep = json.load(open(os.path.join(data, "screen.json")))
calib = {}
for path in sorted(glob.glob(os.path.join(data, "calib*.json"))):
    calib.update(json.load(open(path)))
names = sorted({k.split("|")[1] for k in rep}, key=lambda n: list(rep).index(f"1-2w|{n}"))
print(f"{'rule':<11} " + "  ".join(f"{h + ': dNet (t), dGap, dBrier fill / win, dReach / dTurn':<62}" for h in ("1-2w", "1-3m", "6-12m"))
      + "  passes  smallest margin")
for name in names:
    cells, ok, margins = [], True, []
    for h in ("1-2w", "1-3m", "6-12m"):
        b, c = rep[f"{h}|base"]["summary"], rep[f"{h}|{name}"]["summary"]
        d, _, t = rep[f"{h}|{name}"]["diff"]
        dgap = c["gap"] - b["gap"]
        dbf = (c["brier_fill"] - c["brier_fill_const"]) - (b["brier_fill"] - b["brier_fill_const"])
        dbw = (c["brier_win"] - c["brier_win_const"]) - (b["brier_win"] - b["brier_win_const"])
        cb, cc = calib.get(f"base|{h}"), calib.get(f"{name}|{h}")
        dr = dt = float("nan")
        if cb and cc:
            dr, dt = cc["reach_brier"] - cb["reach_brier"], cc["turn_brier"] - cb["turn_brier"]
        m = [(0.01 - dgap) / 0.01, (0.002 - dbf) / 0.002, (0.002 - dbw) / 0.002,
             (0.002 - dr) / 0.002, (0.002 - dt) / 0.002]
        if h != "6-12m":
            m.append((d - 0.02) / 0.02 if t >= 2 else -1.0)
        passed = all(x >= 0 for x in m)   # nan (test 4 not run) fails
        ok = ok and passed
        margins += m
        cells.append(f"{d:+.3f} ({t:+5.1f}), {dgap:+.3f}, {dbf:+.4f} / {dbw:+.4f}, {dr:+.4f} / {dt:+.4f}{' ' if passed else '*'}")
    smallest = min(margins)
    print(f"{name:<11} " + "  ".join(f"{c:<62}" for c in cells) + ("  yes   " if ok else "  no    ") + f"{smallest:+.2f}")
print("* a test of the criterion fails at that horizon (fit part, in sample; test 4 on the dates after the held-out years)")
