#!/usr/bin/env python3
"""Ranked trade setups from one run: build them, or fill the prompt and check Claude's reply.

    python trade_entries.py build  INPUT.json PROJECTION.json [-o FILE]  -> the setups as JSON, no Claude needed
    python trade_entries.py prompt INPUT.json PROJECTION.json            -> the filled prompt, on stdout
    python trade_entries.py check  INPUT.json PROJECTION.json REPLY.json -> OK, or what is wrong (exit 1)
    python trade_entries.py expected INPUT.json PROJECTION.json          -> every valid setup, best first

The prompt is prompts/trade_entries.md and the reply's shape is
prompts/trade_entries.schema.json. Every number in a reply follows from the
input and the projection by the template's rules, so `build` computes them here
and `check` recomputes them and compares. Prices come from zone edges; `fill`,
`win_if_filled` and the edge are counted on simulated price paths (paths(),
odds()): rz.walk() with its fixed seed, refined to daily steps by a Brownian
bridge with a fixed seed of its own, so the same files always give the same
numbers. `build` also writes the sentences around the numbers from fixed wording
(TEXT), which is why it needs no language model; the prompt is for when Claude
should write them instead, and it carries the computed candidates. The setups
are hypothetical, for study: not a recommendation.
"""
import importlib.util
import json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "prompts" / "trade_entries.md"
SCHEMA = ROOT / "prompts" / "trade_entries.schema.json"
RZ_PATH = ROOT / ".claude" / "skills" / "reversal-zones-projection" / "scripts" / "rz.py"
RULES = 3   # version of the rules; 1 (until 19 Nov 2026) took fill and win from reach and turn by formulas,
            # 2 (19 Nov 2026) also swapped the third setup for the other side when all three shared one

# The path estimate (paths(), odds()). A change to any of these is a new RULES number: an
# entries file is checked by recomputing it.
PATHS = 20_000       # the first 20,000 of rz.walk()'s paths: the same draws as a 200,000-path sim's first rows
BRIDGE_SEED = 8      # the days between two closes and each day's high and low; walk() keeps its seed 7
DAYS = {"1D": 1, "1W": 5, "1M": 21}   # trading days per candle of the close basis; intraday: one step
# Each day's high and low are Brownian-bridge extremes with SPREAD x the simulation's volatility.
# 1 would be the simulation's own volatility; 0.65 was chosen on the first two years of as-of
# dates of the setup backtest (README, "Backtest of the trade setups").
SPREAD = 0.65
EDGE_BAND = 0.05     # quality: favourable from +0.05 R per fill, unfavourable from -0.05 down
MIN_FILL = 0.05      # a setup whose order fills on fewer paths says nothing (its edge rounds to zero and would rank first)
TOLERANCE = 0.011        # one hundredth, for a rounding that fell the other way
RANK_TOLERANCE = 0.021   # two setups this close in edge.per_order may swap places
MIN_PLACES, MAX_PLACES = 2, 8   # prices are rounded to the candle rows' precision, within these


def half_up(value, digits=2):
    return round(value + 1e-9, digits) + 0.0   # + 0.0: never -0.0


def places(source):
    """The precision of the input's candle rows: the most decimals any open, high, low or close
    shows (build_prompt.py --decimals), at least 2 and at most 8. Prices, risk, reward and the
    units are rounded to it, so an asset priced around 0.04 keeps its digits."""
    found = MIN_PLACES
    try:
        columns = source["data"]["columns"]
        wanted = [columns.index(name) for name in ("open", "high", "low", "close")]
        for series in source["data"]["series"]:
            for row in series["rows"]:
                for i in wanted:
                    exponent = Decimal(repr(float(row[i]))).as_tuple().exponent
                    found = max(found, -exponent if isinstance(exponent, int) else 0)
    except (KeyError, TypeError, ValueError, IndexError, InvalidOperation):
        pass
    return min(found, MAX_PLACES)


def price_tolerance(digits):
    """One unit of the last price digit, plus a margin: 0.011 at two decimals."""
    return TOLERANCE * 10 ** (MIN_PLACES - digits)


def candidates(source, proj):
    """Every valid setup with its computed numbers, best first, as the prompt carries them: what
    Claude cannot compute without tools (the simulated paths). [] when no setup can be built."""
    if precondition(source, proj):
        return []
    rows = []
    for item in setups(source, proj):
        detail = {key: value for key, value in item["detail"].items() if key not in ("paths", "method")}
        detail.update(loss=half_up(detail["loss"]), open=half_up(detail["open"]))
        rows.append({**{key: value for key, value in item.items() if key != "detail"}, "detail": detail})
    return rows


def fill(input_text, projection_text):
    """The prompt to send: the template below its opening comment, with both files and the
    computed candidates (candidates()) in place."""
    template = TEMPLATE.read_text(encoding="utf-8")
    template = template[template.index("-->") + 3:].lstrip()
    try:
        found = candidates(json.loads(input_text), json.loads(projection_text))
    except (ValueError, KeyError, TypeError, IndexError):
        found = []
    listed = "[\n" + ",\n".join("  " + json.dumps(row, ensure_ascii=False) for row in found) + "\n]" if found else "[]"
    return (template.replace("{{INPUT_JSON}}", input_text.strip())
                    .replace("{{PROJECTION_JSON}}", projection_text.strip())
                    .replace("{{SETUPS_JSON}}", listed))


def bounded(zones):
    return [zone for zone in zones if None not in zone["zone"]]


def precondition(source, proj):
    """Why no setup can be built from this pair, or None. Mirrors 'Before you start'."""
    if str(source["asset"]["ticker"]).upper() != str(proj["asset"]["ticker"]).upper():
        return "input and projection are for different tickers"
    if source["now"]["time"] != proj["as_of"]["time"]:
        return "projection.as_of.time differs from input.now.time"
    if not bounded(proj["supports"]) or not bounded(proj["resistances"]):
        return "a side of the projection has no zone with two numeric edges"
    return None


def units(proj, digits=MIN_PLACES):
    """move, buffer and range, rounded to `digits`, the precision of the candle rows (places())."""
    price, vol = proj["as_of"]["price"], proj["structure"]["volatility"]["assumed"]
    move = half_up(price * vol[0], digits)
    return {"move": move, "buffer": half_up(move / 4, digits),
            "range": half_up(price * sum(vol) / 2 * math.sqrt(proj["horizon"]["closes"]), digits)}


_RZ = None


def rz():
    """The skill's helper (rz.py), which holds walk(); loaded once, by path."""
    global _RZ
    if _RZ is None:
        spec = importlib.util.spec_from_file_location("rz", RZ_PATH)
        _RZ = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RZ)
    return _RZ


def paths(source):
    """The simulated days the setups are played on -> {"high", "low", "last", "steps", "method"}.

    1. rz.walk() on the input, as `rz.py sim` runs it (default sim method and its drift, seed 7,
       no shocks), PATHS paths: closes of the main series.
    2. Between two closes, daily points on a Brownian bridge in log price with the simulation's
       own volatility for that candle (DAYS per candle; the forming candle gets its share). The
       closes stay exactly as walk() made them; the bridge only fills in the days.
    3. Each day's high and low: the extremes of a Brownian bridge between its two points, drawn
       by the exact inverse of their distribution, with SPREAD x the day's volatility. They count
       the touches inside a candle that closes alone would miss.
    Steps 2 and 3 draw from their own generator (BRIDGE_SEED), so walk()'s draws are untouched
    and the same input always gives the same days."""
    lib = rz()
    d, main, t, h, l, c = lib.unpack(source)
    method = lib.sim_method(None)
    walked, dates, frac, v, sub = lib.walk(d, main, t, c, {}, PATHS, lib.drift_of(main, method))
    n, start_price = len(dates), math.log(d["now"]["price"])
    sigma = np.repeat(np.linspace(v["assumed"][0], v["assumed"][1], n), sub) / np.sqrt(sub)   # as walk()
    sigma[:sub] *= np.sqrt(frac)
    per = DAYS.get(main["candles"], 1) / sub
    counts = [max(1, round(per * (frac if i < sub else 1.0))) for i in range(n * sub)]
    rng = np.random.default_rng(BRIDGE_SEED)
    closes = np.log(walked)
    before = np.full(PATHS, start_price)
    points, scale = [], []
    for i, m in enumerate(counts):
        day = sigma[i] / math.sqrt(m)
        if m > 1:
            noise = np.cumsum(rng.standard_normal((PATHS, m)) * day, axis=1)
            share = np.arange(1, m + 1) / m
            points.append(before[:, None] + noise - share * noise[:, -1:]
                          + share * (closes[:, i] - before)[:, None])
        else:
            points.append(closes[:, i:i + 1])
        scale += [day] * m
        before = closes[:, i]
    end = np.concatenate(points, axis=1)
    start = np.concatenate([np.full((PATHS, 1), start_price), end[:, :-1]], axis=1)
    twice = 2 * (SPREAD * np.array(scale)) ** 2
    middle, gap = start + end, (end - start) ** 2
    high = (middle + np.sqrt(gap - twice * np.log(rng.random(end.shape)))) / 2
    low = (middle - np.sqrt(gap - twice * np.log(rng.random(end.shape)))) / 2
    return {"high": high, "low": low, "last": np.exp(end[:, -1]), "steps": end.shape[1], "method": method}


def first(mask, steps):
    """Index of the first True of each row, or `steps` when there is none."""
    return np.where(mask.any(axis=1), mask.argmax(axis=1), steps)


def odds(sim, side, order, entry, stop, target, ratio):
    """One setup played on every simulated path (paths()) by the rules of backtest_entries.walk()
    under its default, pessimistic ordering -> {"fill", "win", "loss", "open", "if_filled"}:
    the share of paths that fill, the shares of the filled ones that end at the target, at the
    stop or still open at valid_until, and the mean R of the filled ones.

    - A market order fills before the first day; a limit or stop order on the first day whose
      low (falling orders) or high (rising orders) reaches the entry.
    - Then the stop and the target, day by day. On the fill day the stop counts if the day
      reaches it (it may have come first: counted against the setup), the target only for a stop
      order, whose target lies beyond the entry in the direction of the fill. A day that reaches
      both counts as the stop.
    - R: -1 at the stop, +ratio at the target, the last close against the entry when still open,
      0 when never filled."""
    long, steps = side == "long", sim["steps"]
    high, low = sim["high"], sim["low"]
    days = np.arange(steps)[None, :]
    if order == "market":
        filled_on = np.full(len(high), -1)
    else:
        falling = (order == "limit") == long
        filled_on = first(low <= math.log(entry) if falling else high >= math.log(entry), steps)
    filled = filled_on < steps
    on = filled_on[:, None]
    stop_hit = (low <= math.log(stop)) if long else (high >= math.log(stop))
    target_hit = (high >= math.log(target)) if long else (low <= math.log(target))
    stopped = first(stop_hit & (days >= on), steps)
    reached = first(target_hit & ((days > on) | ((days == on) & (order == "stop"))), steps)
    lost = filled & (stopped < steps) & (stopped <= reached)
    won = filled & ~lost & (reached < steps)
    still = filled & ~lost & ~won
    held = ((sim["last"] - entry) if long else (entry - sim["last"])) / abs(entry - stop)
    r = np.where(lost, -1.0, np.where(won, ratio, np.where(still, held, 0.0)))
    count = int(filled.sum())
    share = lambda mask: float(mask.sum()) / count if count else 0.0
    return {"fill": float(filled.mean()), "win": share(won), "loss": share(lost), "open": share(still),
            "if_filled": float(r[filled].mean()) if count else 0.0}


def quality(if_filled):
    """favourable, marginal or unfavourable, from the edge per fill and EDGE_BAND."""
    return "favourable" if if_filled >= EDGE_BAND else "unfavourable" if if_filled <= -EDGE_BAND else "marginal"


def setups(source, proj, sim=None):
    """Every valid setup the template's rules allow, ranked by edge per order, then fill, then id.
    Prices come from the projection's zone edges, rounded to the input's rows (places()); the
    odds from the simulated paths of the input (paths(), computed here unless `sim` is given)."""
    digits = places(source)
    price, unit = proj["as_of"]["price"], units(proj, digits)
    buffer, found, played = unit["buffer"], [], {"sim": sim}

    def add(side, kind, zone_id, order, entry, stop, target, detail):
        ordered = stop < entry < target if side == "long" else target < entry < stop
        if not ordered:
            return
        risk, reward = half_up(abs(entry - stop), digits), half_up(abs(target - entry), digits)
        if risk <= 0:   # entry and stop round to the same price: no ratio, so no setup
            return
        ratio = half_up(reward / risk)
        if ratio < 0.5:
            return
        stop = half_up(stop, digits)
        if played["sim"] is None:
            played["sim"] = paths(source)
        sim_now = played["sim"]
        result = odds(sim_now, side, order, entry, stop, target, ratio)
        fill_p, win, if_filled = half_up(result["fill"]), half_up(result["win"]), half_up(result["if_filled"])
        if fill_p < MIN_FILL:
            return
        found.append({
            "id": f"{side}-{zone_id}-{kind}", "side": side, "setup": kind, "order": order,
            "entry": entry, "stop": stop, "target": target,
            "risk": risk, "reward": reward, "ratio": ratio, "fill": fill_p, "win_if_filled": win,
            "if_filled": if_filled, "per_order": half_up(fill_p * if_filled), "quality": quality(if_filled),
            "detail": dict(detail, zone=zone_id, buffer=buffer, loss=result["loss"], open=result["open"],
                           paths=PATHS, method=sim_now["method"])})

    for listed, opposite, support in ((proj["supports"], proj["resistances"], True),
                                      (proj["resistances"], proj["supports"], False)):
        zones, first_opposite = bounded(listed), bounded(opposite)[0]["zone"]
        for index, zone in enumerate(zones):
            low, high = zone["zone"]
            near, far = (high, low) if support else (low, high)

            # reversal: the zone holds; long at a support, short at a resistance
            inside = low <= price <= high
            entry, order = (price, "market") if inside else (near, "limit")
            if inside or (entry <= price if support else entry >= price):
                add("long" if support else "short", "reversal", zone["id"], order, entry,
                    far - buffer if support else far + buffer,
                    first_opposite[0] if support else first_opposite[1],
                    {"edge": far, "target_zone": bounded(opposite)[0]["id"],
                     "between": [[z["id"], *z["zone"]] for z in zones[:index]]})

            # continuation: the zone breaks; not for the last zone with two edges on a side
            if index + 1 < len(zones) and (far < price if support else far > price):
                following = zones[index + 1]
                add("short" if support else "long", "continuation", zone["id"], "stop", far,
                    near + buffer if support else near - buffer,
                    following["zone"][0] if support else following["zone"][1],
                    {"edge": near, "next": following["id"]})

    found.sort(key=lambda item: (-item["per_order"], -item["fill"], item["id"]))
    return found


def pick(found):
    """The three to keep: best first, with distinct entry prices, whatever their side. Rules 2
    swapped the third for the best of the other side when all three shared one; the setup
    backtest found that swapped-in setup the weakest of the three (README), so rules 3 lists
    the best three as they rank and says so in confidence.limits when they share a side."""
    chosen = []
    for item in found:
        if len(chosen) == 3:
            break
        if all(item["entry"] != kept["entry"] for kept in chosen):
            chosen.append(item)
    return chosen


# ---- the sentences around the numbers, for `build` ---------------------------------------

TEXT = {
    "en": {
        "near": "{zone} near edge", "far": "{zone} far edge", "price": "current price, inside {zone}",
        "plus": "{zone} {which} edge {edge} + buffer {buffer}", "minus": "{zone} {which} edge {edge} − buffer {buffer}",
        "which": {"near": "near", "far": "far"},
        "basis": "{paths} simulated paths: the order fills in {fill}; of the fills, {win} reach the target first, {loss} the stop, {open} are still open at valid_until",
        "basis_market": "{paths} simulated paths: a market order inside {zone} fills at once; {win} reach the target first, {loss} the stop, {open} are still open at valid_until",
        "why_reversal": "If {zone} holds and the price turns back to {target_zone}, the reward is {ratio} times the risk; on the simulated paths the target comes before the stop in {win} of the fills.",
        "why_continuation": "If {zone} breaks and the price carries on to {next}, the reward is {ratio} times the risk; on the simulated paths the target comes before the stop in {win} of the fills.",
        "between": "{zone} {low}–{high} lies between entry and target",
        "dividend": {"long": "{date} {what}: the drop works against a long and can jump the stop",
                     "short": "{date} {what}: a short pays the dividend, and the drop can fill the order on a gap"},
        "event": "{date} {what}: a price gap can jump the stop",
        "paths": "fill, win_if_filled and the edge are counted on random price paths with the projection's volatility and drift; the paths know nothing of the zones, so the odds describe the setup's distances, not an effect of a zone holding or breaking",
        "touch": "fills, stops and targets are judged on simulated daily highs and lows, and a day that reaches both the stop and the target counts as the stop; the projection's own reach and turn are for {basis} closes",
        "calibration": "a backtest of these rules on the mechanical engine compared the odds with outcomes on average over many past dates, before costs, not for this setup; the projection's own confidence is {level}",
        "no_edge": "no setup has a positive edge per order on the simulated paths: each of them loses on average, before costs",
        "fewer": "only {n} setup(s) pass the rules for this projection",
        "one_side": "every setup kept is on the {side} side, because these ranked highest by edge per order on the simulated paths; the list is not balanced by side, and a one-sided list is not a forecast of direction or a recommendation",
        "sides": {"long": "long", "short": "short"},
        "method": "Prices from zone edges only; fill, win and edge counted on {paths} simulated paths of the projection's simulation (sim method {sim}), refined to daily highs and lows; ranked by edge per order. {valid} setups were valid, {kept} kept. Written by trade_entries.py, without a language model.",
        "note": "Hypothetical setups for study, derived mechanically from a price simulation. Not a recommendation to buy or sell.",
    },
    "pt": {
        "near": "borda próxima de {zone}", "far": "borda distante de {zone}", "price": "preço atual, dentro de {zone}",
        "plus": "borda {which} de {zone} {edge} + margem {buffer}", "minus": "borda {which} de {zone} {edge} − margem {buffer}",
        "which": {"near": "próxima", "far": "distante"},
        "basis": "{paths} caminhos simulados: a ordem é executada em {fill}; das execuções, {win} chegam antes ao alvo, {loss} ao stop, {open} seguem abertas em valid_until",
        "basis_market": "{paths} caminhos simulados: uma ordem a mercado dentro de {zone} é executada na hora; {win} chegam antes ao alvo, {loss} ao stop, {open} seguem abertas em valid_until",
        "why_reversal": "Se {zone} segurar e o preço voltar até {target_zone}, o ganho é {ratio} vezes o risco; nos caminhos simulados o alvo vem antes do stop em {win} das execuções.",
        "why_continuation": "Se {zone} romper e o preço seguir até {next}, o ganho é {ratio} vezes o risco; nos caminhos simulados o alvo vem antes do stop em {win} das execuções.",
        "between": "{zone} {low}–{high} fica entre a entrada e o alvo",
        "dividend": {"long": "{date} {what}: a queda joga contra uma posição comprada e pode pular o stop",
                     "short": "{date} {what}: a posição vendida paga o dividendo, e a queda pode executar a ordem num gap"},
        "event": "{date} {what}: um gap de preço pode pular o stop",
        "paths": "fill, win_if_filled e o edge são contados em caminhos aleatórios de preço com a volatilidade e o drift da projeção; os caminhos não sabem nada das zonas, então as chances descrevem as distâncias do setup, não um efeito de a zona segurar ou romper",
        "touch": "execuções, stops e alvos são julgados nas máximas e mínimas diárias simuladas, e um dia que alcança o stop e o alvo conta como stop; o reach e o turn da própria projeção valem para fechamentos de {basis}",
        "calibration": "um backtest destas regras no motor mecânico comparou as chances com os resultados em média, em muitas datas passadas e antes de custos, não para este setup; a confiança da própria projeção é {level}",
        "no_edge": "nenhum setup tem edge por ordem positivo nos caminhos simulados: em média, todos perdem, antes de custos",
        "fewer": "apenas {n} setup(s) passam nas regras para esta projeção",
        "one_side": "todos os setups mantidos são do lado {side}, porque foram os de maior edge por ordem nos caminhos simulados; a lista não é equilibrada por lado, e uma lista de um lado só não é previsão de direção nem recomendação",
        "sides": {"long": "comprado", "short": "vendido"},
        "method": "Preços só de bordas de zonas; fill, win e edge contados em {paths} caminhos simulados da simulação da projeção (sim method {sim}), refinados para máximas e mínimas diárias; ordenados pelo edge por ordem. {valid} setups válidos, {kept} mantidos. Escrito por trade_entries.py, sem modelo de linguagem.",
        "note": "Setups hipotéticos para estudo, derivados mecanicamente de uma simulação de preços. Não é recomendação de compra ou venda.",
    },
}
FALLBACK_LANG = "en"


def language(source):
    """The TEXT entry for the input's reply.language (primary subtag), or English."""
    reply = source.get("reply") if isinstance(source, dict) else None
    asked = reply.get("language") if isinstance(reply, dict) else None
    code = asked.strip().lower().replace("_", "-").split("-")[0] if isinstance(asked, str) else ""
    return TEXT.get(code, TEXT[FALLBACK_LANG])


def money(value, digits=MIN_PLACES):
    return f"{value:.{digits}f}"


def share(value):
    return f"{round(value * 100):d}%"


def entry_of(rank, item, proj, t, digits=MIN_PLACES):
    """One setup in the reply's shape, with its sentences. Prices are written with `digits`
    decimals, probabilities and ratios with two."""
    d, long, zone = item["detail"], item["side"] == "long", item["detail"]["zone"]
    away = t["minus"] if long else t["plus"]          # the stop sits beyond an edge, away from the entry
    counted = {"paths": f"{d['paths']:,}", "fill": share(item["fill"]), "win": share(item["win_if_filled"]),
               "loss": share(d["loss"]), "open": share(d["open"]), "zone": zone}
    if item["setup"] == "reversal":
        market = item["order"] == "market"
        entry_anchor = t["price" if market else "near"].format(zone=zone)
        stop_anchor = away.format(zone=zone, which=t["which"]["far"], edge=money(d["edge"], digits),
                                  buffer=money(d["buffer"], digits))
        target_anchor = t["near"].format(zone=d["target_zone"])
        basis = t["basis_market" if market else "basis"].format(**counted)
        why = t["why_reversal"].format(zone=zone, target_zone=d["target_zone"], ratio=money(item["ratio"]),
                                       win=share(item["win_if_filled"]))
        watch = [t["between"].format(zone=name, low=money(low, digits), high=money(high, digits))
                 for name, low, high in d["between"]]
    else:
        entry_anchor = t["far"].format(zone=zone)
        stop_anchor = away.format(zone=zone, which=t["which"]["near"], edge=money(d["edge"], digits),
                                  buffer=money(d["buffer"], digits))
        target_anchor = t["far"].format(zone=d["next"])
        basis = t["basis"].format(**counted)
        why = t["why_continuation"].format(zone=zone, next=d["next"], ratio=money(item["ratio"]),
                                           win=share(item["win_if_filled"]))
        watch = []
    for event in proj.get("events") or []:
        date = event.get("date")
        if date and proj["as_of"]["time"][:10] <= date[:10] <= proj["horizon"]["last"]:
            what = str(event.get("what", "event"))
            line = t["dividend"][item["side"]] if "dividend" in what.lower() else t["event"]
            watch.append(line.format(date=date[:10], what=what))
    return {
        "rank": rank, "id": item["id"], "side": item["side"], "setup": item["setup"],
        "entry": {"order": item["order"], "price": item["entry"], "anchor": entry_anchor},
        "stop": {"price": item["stop"], "anchor": stop_anchor},
        "target": {"price": item["target"], "anchor": target_anchor},
        "payoff": {"risk": item["risk"], "reward": item["reward"], "ratio": item["ratio"]},
        "odds": {"fill": item["fill"], "win_if_filled": item["win_if_filled"], "basis": basis},
        "edge": {"if_filled": item["if_filled"], "per_order": item["per_order"]},
        "quality": item["quality"], "why": why, "watch": watch}


NO_SETUP = "no setup passes the rules for this projection"


def build(source, proj, found=None):
    """The whole reply, computed: what the prompt asks Claude for, with fixed wording. `found`
    is setups(source, proj) when the caller has it already."""
    reason = precondition(source, proj)
    if reason:
        return {"task": "trade_entries", "error": reason}
    t, digits = language(source), places(source)
    found = setups(source, proj) if found is None else found
    kept, measure = pick(found), source["want"]["probability"]
    if not kept:
        return {"task": "trade_entries", "error": NO_SETUP}
    level = proj["confidence"]["level"]
    limits = [t["paths"], t["touch"].format(basis=proj["horizon"]["close_basis"]),
              t["calibration"].format(level=level)]
    if all(item["per_order"] <= 0 for item in found):
        limits.append(t["no_edge"])
    if len(kept) < 3:
        limits.append(t["fewer"].format(n=len(kept)))
    if len({item["side"] for item in kept}) == 1:
        limits.append(t["one_side"].format(side=t["sides"][kept[0]["side"]]))
    return {
        "task": "trade_entries", "rules": RULES,
        "asset": {"ticker": proj["asset"]["ticker"], "currency": proj["asset"]["currency"]},
        "as_of": proj["as_of"], "valid_until": proj["horizon"]["last"], "probability": measure,
        "units": units(proj, digits),
        "entries": [entry_of(rank, item, proj, t, digits) for rank, item in enumerate(kept, 1)],
        "confidence": {"level": level, "limits": limits},
        "method": t["method"].format(paths=f"{PATHS:,}", sim=found[0]["detail"]["method"],
                                     valid=len(found), kept=len(kept)),
        "note": t["note"]}


def schema_problems(reply):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["the jsonschema package is missing, so the reply's shape was not checked (run `uv sync`)"]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return [f"{'.'.join(str(part) for part in error.path) or 'reply'}: {error.message}"
            for error in sorted(Draft202012Validator(schema).iter_errors(reply), key=lambda e: list(map(str, e.path)))]


EARLIER = {1: "its odds came from reach and turn by formulas, not from simulated paths",
           2: "its third setup may have been swapped for the best one of the other side"}
PRICES = {"entry", "stop", "target", "risk", "reward"}   # checked to the rows' precision, the rest to 0.011


def problems(source, proj, reply):
    """Everything wrong with a reply, as lines. Empty means it is consistent with the projection."""
    reason = precondition(source, proj)
    if isinstance(reply, dict) and set(reply) == {"task", "error"}:
        if reason or not setups(source, proj):   # unusable files, or no setup passes the rules
            return []
        return [f"the reply reports an error but the files are usable: {reply['error']}"]
    if reason:
        return [f"no setup can be built ({reason}), but the reply holds entries"]
    if isinstance(reply, dict) and "entries" in reply and reply.get("rules") != RULES:
        version = reply.get("rules", 1)
        why = EARLIER.get(version, "its rules are not this checkout's")
        return [f"written by an earlier version of the rules (rules {version}, now {RULES}): {why}; compute the "
                "setups again (`zones entries` or `trade_entries.py build`)"]
    bad = schema_problems(reply)
    if bad:
        return bad

    if reply["as_of"] != proj["as_of"]:
        bad.append("as_of must equal the projection's as_of")
    if reply["valid_until"] != proj["horizon"]["last"]:
        bad.append(f"valid_until must be {proj['horizon']['last']}, the projection's horizon.last")
    if reply["probability"] != source["want"]["probability"]:
        bad.append(f"probability must be {source['want']['probability']}, the input's want.probability")
    if reply["confidence"]["level"] == "medium" and proj["confidence"]["level"] != "medium":
        bad.append("confidence.level may not be higher than the projection's")
    digits = places(source)
    near = price_tolerance(digits)
    for name, value in units(proj, digits).items():
        if abs(reply["units"][name] - value) > near:
            bad.append(f"units.{name}: {reply['units'][name]}, expected {value}")

    found = setups(source, proj)
    by_id, wanted, got = {item["id"]: item for item in found}, pick(found), reply["entries"]
    numbers = (("entry.price", lambda e: e["entry"]["price"], "entry"), ("stop.price", lambda e: e["stop"]["price"], "stop"),
               ("target.price", lambda e: e["target"]["price"], "target"), ("payoff.risk", lambda e: e["payoff"]["risk"], "risk"),
               ("payoff.reward", lambda e: e["payoff"]["reward"], "reward"), ("payoff.ratio", lambda e: e["payoff"]["ratio"], "ratio"),
               ("odds.fill", lambda e: e["odds"]["fill"], "fill"), ("odds.win_if_filled", lambda e: e["odds"]["win_if_filled"], "win_if_filled"),
               ("edge.if_filled", lambda e: e["edge"]["if_filled"], "if_filled"), ("edge.per_order", lambda e: e["edge"]["per_order"], "per_order"))
    for position, entry in enumerate(got, 1):
        name = entry["id"]
        if entry["rank"] != position:
            bad.append(f"{name}: rank {entry['rank']} at position {position}")
        expected = by_id.get(name)
        if expected is None:
            bad.append(f"{name}: not a setup the rules allow for this projection")
            continue
        for label, read, key in numbers:
            if abs(read(entry) - expected[key]) > (near if key in PRICES else TOLERANCE):
                bad.append(f"{name}.{label}: {read(entry)}, expected {expected[key]}")
        for label, value, key in (("side", entry["side"], "side"), ("setup", entry["setup"], "setup"),
                                  ("entry.order", entry["entry"]["order"], "order"), ("quality", entry["quality"], "quality")):
            if value != expected[key]:
                bad.append(f"{name}.{label}: {value}, expected {expected[key]}")
    if len(got) != len(wanted):
        bad.append(f"{len(got)} entries, expected {len(wanted)}: {', '.join(item['id'] for item in wanted)}")
    elif [entry["id"] for entry in got] != [item["id"] for item in wanted]:
        # a different pick is accepted only when the edges are too close to tell apart
        close = all(entry["id"] in by_id and abs(by_id[entry["id"]]["per_order"] - item["per_order"]) <= RANK_TOLERANCE
                    for entry, item in zip(got, wanted))
        if not close or len({entry["entry"]["price"] for entry in got}) != len(got):
            bad.append("ranking: got " + ", ".join(entry["id"] for entry in got)
                       + "; expected " + ", ".join(item["id"] for item in wanted))
    return bad


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "build" and (len(argv) == 3 or (len(argv) == 5 and argv[3] == "-o")):
            text = json.dumps(build(load(argv[1]), load(argv[2])), indent=2, ensure_ascii=False) + "\n"
            if len(argv) == 5:
                Path(argv[4]).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            return 0
        if len(argv) == 3 and argv[0] == "prompt":
            sys.stdout.write(fill(Path(argv[1]).read_text(encoding="utf-8"), Path(argv[2]).read_text(encoding="utf-8")))
            return 0
        if len(argv) == 4 and argv[0] == "check":
            bad = problems(load(argv[1]), load(argv[2]), load(argv[3]))
            print("OK" if not bad else "\n".join(bad))
            return 1 if bad else 0
        if len(argv) == 3 and argv[0] == "expected":
            source, proj = load(argv[1]), load(argv[2])
            reason = precondition(source, proj)
            if reason:
                print(f"Error: {reason}", file=sys.stderr)
                return 1
            found = candidates(source, proj)
            keep = {item["id"] for item in pick(found)}
            print(json.dumps(units(proj, places(source))))
            for item in found:
                print("*" if item["id"] in keep else " ", json.dumps(item, ensure_ascii=False))
            return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
