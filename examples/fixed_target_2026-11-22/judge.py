"""Choice (fit part) and judgement (held-out part) of README "A fixed R target at 1-3m".

    uv run python examples/fixed_target_2026-11-22/judge.py fit OUT_FIT
    uv run python examples/fixed_target_2026-11-22/judge.py held-out OUT_FIT OUT_HELD
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "examples", "placement_2026-11-21"))
sys.path.insert(0, HERE)
import analyse as A  # noqa: E402
import harness as H  # noqa: E402
from run import VARIANTS  # noqa: E402

HORIZON = "1-3m"


def tests(s, base, c):
    """The protocol's tests 1-4: {name: passed}."""
    return {"1 net": c["diff"] >= 0.02 and c["t"] >= 2,
            "2 gap": s["gap"] <= base["gap"] + 0.01,
            "3 honest": abs(s["fill_st"] - s["fill_obs"]) <= 0.03 and abs(s["win_st"] - s["win_obs"]) <= 0.03,
            "4 brier": s["bf_rel"] <= 0.002 and s["bw_rel"] <= 0.002}


def fit(out):
    cases, rows = A.load(out, HORIZON, VARIANTS)
    base, base_net, _ = A.evaluate(cases, rows[0], HORIZON)
    print(f"{HORIZON}: {len(cases)} fit cases (every 20 trading days)")
    table, eligible = {}, []
    for v in VARIANTS:
        s, net, corr = A.evaluate(cases, rows[v], HORIZON)
        c = A.compare(cases, base_net, net)
        t = tests(s, base, c)
        ok = v != 0 and all(t.values()) and all(x > 0 for x in c["blocks"])
        if ok:
            eligible.append(v)
        table[v] = {"name": H.name(H.VARIANTS[v]), **s, **{f"cmp_{k}": x for k, x in c.items()}, "tests": t,
                    "eligible": ok, "correction": {f"{k[1]} {k[2]}": x for k, x in (corr or {}).items()}}
        failed = [k for k, x in t.items() if not x] + ([] if all(x > 0 for x in c["blocks"]) else ["blocks"])
        print(("* " if ok else "  ") + A.line(H.name(H.VARIANTS[v]), s, c)
              + f" | fill st-obs {s['fill_st'] - s['fill_obs']:+.3f} win st-obs {s['win_st'] - s['win_obs']:+.3f}"
              + (f" | fails {', '.join(failed)}" if v and failed else ""))
    best = max(eligible, key=lambda v: table[v]["net"]) if eligible else None
    print(f"candidate: {H.name(H.VARIANTS[best]) if best is not None else 'none'}")
    frozen = {"protocol": 'README "A fixed R target at 1-3m"', "step": 20, "candidate": best,
              "name": H.name(H.VARIANTS[best]) if best is not None else None,
              "correction": table[best]["correction"] if best is not None else None,
              "band": table[best]["band"] if best is not None else None,
              "base_correction": table[0]["correction"], "base_band": table[0]["band"], "variants": table}
    with open(os.path.join(HERE, "FROZEN.json"), "w") as handle:
        json.dump(frozen, handle, indent=1, default=float)


def held(fit_out, held_out):
    frozen = json.load(open(os.path.join(HERE, "FROZEN.json")))
    best = frozen["candidate"]
    assert best is not None, "nothing frozen: the held-out part is not read"
    _, fit_rows = A.load(fit_out, HORIZON, (0, best))
    cases, rows = A.load(held_out, HORIZON, (0, best))
    # corrections fitted on the fit part only
    base, base_net, _ = A.evaluate(cases, rows[0], HORIZON, fit_rows=fit_rows[0])
    s, net, _ = A.evaluate(cases, rows[best], HORIZON, fit_rows=fit_rows[best])
    c = A.compare(cases, base_net, net)
    t = tests(s, base, c)
    print(f"{HORIZON}: {len(cases)} held-out cases (every 20 trading days)")
    print("  " + A.line("rules 6", base))
    print("  " + A.line(frozen["name"], s, c) + f" | fill st-obs {s['fill_st'] - s['fill_obs']:+.3f} win st-obs {s['win_st'] - s['win_obs']:+.3f}")
    print("  tests: " + ", ".join(f"{k} {'pass' if x else 'FAIL'}" for k, x in t.items()))
    print("  decision: " + ("adopt" if all(t.values()) else "not adopted"))
    with open(os.path.join(HERE, "judge.json"), "w") as handle:
        json.dump({"base": base, "candidate": s, "compare": c, "tests": t}, handle, indent=1, default=float)


if __name__ == "__main__":
    if sys.argv[1] == "fit":
        fit(sys.argv[2])
    else:
        held(sys.argv[2], sys.argv[3])
