"""
PATTERN LAB - do classic chart patterns predict anything, and what win rate do they REALLY give?
===============================================================================================
Works on 5-minute bars built from your real 1-minute NIFTY / SENSEX candles (no option-price modelling).
Every pattern needs a CONFIRMATION so one-bar flukes do not count (a closed bar beyond a level, a retest that
holds, or the 15-minute trend agreeing). One pattern (TRAP) trades the false breakout itself.

Outcome model (same for every pattern, nothing tuned):
  entry  = open of the 1-minute bar right after the pattern's 5-minute bar closes (09:45-14:30 only)
  stop   = 1 x ATR(5m, 14)            target = RR x stop, RR in {0.5, 1, 2}
  exit   = stop / target / 90 minutes / 15:15 (a time-out is marked to market and is NOT a win: win = target hit).  If a 1-minute bar touches BOTH, the STOP is assumed first.
  result in R (1R = the stop distance).  A cost of --cost-r (default 0.15R) is deducted from every trade:
  option buying pays ~4% round-trip slippage + charges, which is a large fraction of a ~25% premium stop.

WIN RATE IS NOT PROFIT: break-even win rate = 1 / (1 + RR)  ->  RR 0.5 needs 67%, RR 1 needs 50%, RR 2 needs 33%.
A 65-70% win rate is easy to manufacture with a small target and still lose money; it only matters together with
the payoff. The table shows win%, break-even win%, expectancy in R and a t-statistic.

Split: first half of the data = discovery, second half = unseen. 8 patterns x 3 payoffs = 24 tests; a result passes
only if net expectancy |t| >= 2.5 on discovery AND same sign |t| >= 1.65 on the unseen half, n >= 30 in each.

RUN   python pattern_lab.py [--days 365] [--no-download] [--cost-r 0.15]      python pattern_lab.py --demo null|trend
OUT   pattern_lab_report.html
"""
import argparse
import datetime as dt
import glob
import html
import math
import os
import sys
from collections import defaultdict

import backtest as bt0
import expiry_backtest as xb
import expiry_blast as eb

IST = eb.IST
RRS = (0.5, 1.0, 2.0)
T_DISC, T_TEST, MIN_N = 2.5, 1.65, 30
S0 = eb.compile_strategy(eb.CONFIG["strategy"])


# =====================================================================================
# Building 5-minute bars (with context) from 1-minute candles
# =====================================================================================
def build_days(full):
    """-> list of days; each day = {'date', 'm': 1-min candles, 'b': 5-min bars, 'prev_hi', 'prev_lo'}.
    A 5-min bar: o,h,l,c, mclose (minute of day when it closes), i1 (index of its last 1-min bar in the day list)."""
    by = defaultdict(list)
    for c in full:
        by[(c[0] + 19800) // 86400].append(c)
    days, prev = [], None
    for k in sorted(by):
        m = by[k]
        if len(m) < 100:
            continue
        bars, cur = [], None
        for i, c in enumerate(m):
            mm = ((c[0] + 19800) % 86400) // 60
            bucket = (mm - 555) // 5
            if cur is None or cur["bucket"] != bucket:
                cur = {"bucket": bucket, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "i1": i, "ts": c[0]}
                bars.append(cur)
            else:
                cur["h"], cur["l"], cur["c"], cur["i1"] = max(cur["h"], c[2]), min(cur["l"], c[3]), c[4], i
        for b in bars:
            b["mclose"] = 555 + (b["bucket"] + 1) * 5
        # day VWAP (TWAP of typical price - index volume is zero) at each 1-min bar
        cum, vw = 0.0, []
        for i, c in enumerate(m):
            cum += (c[2] + c[3] + c[4]) / 3
            vw.append(cum / (i + 1))
        for b in bars:
            b["vwap"] = vw[b["i1"]]
        days.append({"date": dt.date(1970, 1, 1) + dt.timedelta(days=k), "m": m, "b": bars,
                     "prev_hi": prev[0] if prev else None, "prev_lo": prev[1] if prev else None})
        prev = (max(c[2] for c in m), min(c[3] for c in m))
    # ATR(5m,14) from the PREVIOUS 14 bars, continuous across days
    tr, last_c = [], None
    flat = [b for d in days for b in d["b"]]
    for b in flat:
        tr.append(max(b["h"] - b["l"], abs(b["h"] - last_c), abs(b["l"] - last_c)) if last_c else b["h"] - b["l"])
        last_c = b["c"]
    for n, b in enumerate(flat):
        b["atr"] = sum(tr[n - 14:n]) / 14 if n >= 14 else None
    # higher-timeframe trend signs at each 5-min bar close
    trend = eb.htf_trend(full, (15, 60))
    for d in days:
        for b in d["b"]:
            sg = eb.mtf_signs_at(trend, d["m"][b["i1"]][0] + 60)
            b["t15"], b["t60"] = sg[15], sg[60]
    return days


# =====================================================================================
# Patterns. fn(B, j, d) -> +1 (long) / -1 (short) / 0, evaluated when 5-min bar j has just CLOSED.
# Only information from bars <= j is used.
# =====================================================================================
def _swings(B, j, kind, lo=0):
    """Confirmed pivots (2 bars each side) that were complete before bar j. Returns [(index, price)]."""
    out = []
    for p in range(max(lo + 2, 2), j - 2):
        if kind == "H" and B[p]["h"] > max(B[p - 2]["h"], B[p - 1]["h"], B[p + 1]["h"], B[p + 2]["h"]):
            out.append((p, B[p]["h"]))
        if kind == "L" and B[p]["l"] < min(B[p - 2]["l"], B[p - 1]["l"], B[p + 1]["l"], B[p + 2]["l"]):
            out.append((p, B[p]["l"]))
    return out


def p_inside(B, j, d):
    """Inside bar then a CLOSE beyond the mother bar, with the 15-min trend agreeing."""
    if j < 3:
        return 0
    m, i_, c = B[j - 2], B[j - 1], B[j]
    if not (i_["h"] <= m["h"] and i_["l"] >= m["l"]):
        return 0
    if c["c"] > m["h"] and B[j]["t15"] == 1:
        return 1
    if c["c"] < m["l"] and B[j]["t15"] == -1:
        return -1
    return 0


def p_flag(B, j, d):
    """Impulse (3 bars >= 1.5 ATR) -> tight 4-bar pause (<= 50% of the impulse) -> close beyond the pause, same direction."""
    if j < 8 or not B[j]["atr"]:
        return 0
    imp = B[j - 5]["c"] - B[j - 7]["o"]
    if abs(imp) < 1.5 * B[j]["atr"]:
        return 0
    pause = B[j - 4:j]
    hi, lo = max(x["h"] for x in pause), min(x["l"] for x in pause)
    if hi - lo > 0.5 * abs(imp):
        return 0
    if imp > 0 and B[j]["c"] > hi:
        return 1
    if imp < 0 and B[j]["c"] < lo:
        return -1
    return 0


def p_double(B, j, d):
    """Double bottom/top: two pivots within 0.15% of each other, 6-40 bars apart; CLOSE through the neckline."""
    lows, highs = _swings(B, j, "L"), _swings(B, j, "H")
    if len(lows) >= 2:
        (p1, l1), (p2, l2) = lows[-2], lows[-1]
        if 6 <= p2 - p1 <= 40 and abs(l1 - l2) / l2 <= 0.0015:
            neck = max(x["h"] for x in B[p1:p2 + 1])
            if B[j]["c"] > neck and max(x["c"] for x in B[p2 + 1:j]) <= neck if j > p2 + 1 else B[j]["c"] > neck:
                return 1
    if len(highs) >= 2:
        (p1, h1), (p2, h2) = highs[-2], highs[-1]
        if 6 <= p2 - p1 <= 40 and abs(h1 - h2) / h2 <= 0.0015:
            neck = min(x["l"] for x in B[p1:p2 + 1])
            if B[j]["c"] < neck and min(x["c"] for x in B[p2 + 1:j]) >= neck if j > p2 + 1 else B[j]["c"] < neck:
                return -1
    return 0


def p_hhhl(B, j, d):
    """Trend structure: higher highs AND higher lows (or the mirror) on 5m, 15m trend agrees, buy the first up-close after a pullback bar."""
    hs, ls = _swings(B, j, "H"), _swings(B, j, "L")
    if len(hs) < 2 or len(ls) < 2 or j < 2:
        return 0
    up = hs[-1][1] > hs[-2][1] and ls[-1][1] > ls[-2][1] and B[j]["t15"] == 1
    dn = hs[-1][1] < hs[-2][1] and ls[-1][1] < ls[-2][1] and B[j]["t15"] == -1
    if up and B[j - 1]["c"] < B[j - 1]["o"] and B[j]["c"] > B[j - 1]["h"] and B[j - 1]["l"] > ls[-1][1]:
        return 1
    if dn and B[j - 1]["c"] > B[j - 1]["o"] and B[j]["c"] < B[j - 1]["l"] and B[j - 1]["h"] < hs[-1][1]:
        return -1
    return 0


def p_engulf(B, j, d):
    """Engulfing candle at a real level (previous-day low/high or VWAP), not against a strong 15-min trend."""
    if j < 2:
        return 0
    a, b = B[j - 1], B[j]
    tol = 0.001
    near = lambda x, y: y is not None and abs(x - y) / y <= tol
    if a["c"] < a["o"] and b["c"] > b["o"] and b["o"] <= a["c"] and b["c"] >= a["o"] and B[j]["t15"] != -1:
        if near(min(a["l"], b["l"]), d["prev_lo"]) or near(min(a["l"], b["l"]), b["vwap"]):
            return 1
    if a["c"] > a["o"] and b["c"] < b["o"] and b["o"] >= a["c"] and b["c"] <= a["o"] and B[j]["t15"] != 1:
        if near(max(a["h"], b["h"]), d["prev_hi"]) or near(max(a["h"], b["h"]), b["vwap"]):
            return -1
    return 0


def p_trap(B, j, d):
    """FALSE BREAKOUT fade: a 30-min range is broken by a close, then bar j closes back INSIDE the range -> trade the other way."""
    if j < 10:
        return 0
    rng = B[j - 9:j - 3]
    hi, lo = max(x["h"] for x in rng), min(x["l"] for x in rng)
    broke_up = any(x["c"] > hi for x in B[j - 3:j])
    broke_dn = any(x["c"] < lo for x in B[j - 3:j])
    if broke_up and not broke_dn and lo < B[j]["c"] <= hi:
        return -1
    if broke_dn and not broke_up and lo <= B[j]["c"] < hi:
        return 1
    return 0


def p_retest(B, j, d):
    """09:15-09:30 range break, then a RETEST of the edge that HOLDS (close back beyond it) within 8 bars."""
    orb = [x for x in B if x["mclose"] <= 570]
    if len(orb) < 3 or B[j]["mclose"] <= 570 + 10:
        return 0
    hi, lo = max(x["h"] for x in orb), min(x["l"] for x in orb)
    first = None
    for k in range(len(orb), j):
        if B[k]["c"] > hi:
            first = (k, 1)
            break
        if B[k]["c"] < lo:
            first = (k, -1)
            break
    if not first or not (first[0] < j <= first[0] + 8):
        return 0
    k, s = first
    tol = 0.0005
    if s == 1 and B[j]["l"] <= hi * (1 + tol) and B[j]["c"] > hi and B[j]["c"] > B[j]["o"]:
        return 1
    if s == -1 and B[j]["h"] >= lo * (1 - tol) and B[j]["c"] < lo and B[j]["c"] < B[j]["o"]:
        return -1
    return 0


def p_nr7(B, j, d):
    """NR7: the previous bar had the narrowest range of the last 7; a CLOSE beyond it with the 15-min trend agreeing."""
    if j < 8:
        return 0
    r = [x["h"] - x["l"] for x in B[j - 7:j]]
    if r[-1] != min(r) or r[-1] <= 0:
        return 0
    p = B[j - 1]
    if B[j]["c"] > p["h"] and B[j]["t15"] == 1:
        return 1
    if B[j]["c"] < p["l"] and B[j]["t15"] == -1:
        return -1
    return 0


PATTERNS = [("Inside-bar break (with 15m trend)", p_inside), ("Flag / pause after impulse", p_flag),
            ("Double bottom / top (neckline close)", p_double), ("Higher-high/higher-low pullback", p_hhhl),
            ("Engulfing at prev-day level / VWAP", p_engulf), ("TRAP: failed breakout (fade)", p_trap),
            ("Opening-range break + retest hold", p_retest), ("NR7 squeeze break (with 15m trend)", p_nr7)]


# =====================================================================================
# Outcome simulation on 1-minute bars
# =====================================================================================
def outcome(m, i0, side, entry, risk, rr, last_i, max_hold=90):
    """(R-multiple, target_hit) of a trade entered at 1-min index i0. Stop is checked FIRST when a bar touches both.
    A trade that times out is marked to market and is NOT counted as a win (win = target hit)."""
    stop, tgt = entry - side * risk, entry + side * risk * rr
    end = min(last_i, i0 + max_hold)
    for k in range(i0, end + 1):
        h, l = m[k][2], m[k][3]
        if (side == 1 and l <= stop) or (side == -1 and h >= stop):
            return -1.0, False
        if (side == 1 and h >= tgt) or (side == -1 and l <= tgt):
            return rr, True
    return side * (m[end][4] - entry) / risk, False


def build_trades(days, index, rrs=RRS):
    out = []
    for d in days:
        B, m = d["b"], d["m"]
        last_i = max(i for i, c in enumerate(m) if ((c[0] + 19800) % 86400) // 60 + 1 <= 915)
        for name, fn in PATTERNS:
            for j in range(10, len(B) - 1):
                if not (585 <= B[j]["mclose"] <= 870) or not B[j]["atr"]:
                    continue
                s = fn(B, j, d)
                if not s:
                    continue
                i0 = B[j]["i1"] + 1
                if i0 >= last_i:
                    break
                entry, risk = m[i0][1], B[j]["atr"]
                for rr in rrs:
                    r, hit = outcome(m, i0, s, entry, risk, rr, last_i)
                    out.append({"pattern": name, "rr": rr, "index": index, "date": d["date"].isoformat(), "r": r, "hit": hit})
                break                                                   # one trade per pattern per day
    return out


# =====================================================================================
def tstat(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    mu = sum(xs) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    return mu / (sd / math.sqrt(n)) if sd else 0.0


def summarize(ts, cost):
    """ts = list of trade dicts. Win rate = share of trades that HIT THE TARGET."""
    rs = [t["r"] for t in ts]
    n = len(rs)
    net = [r - cost for r in rs]
    return {"n": n, "win": sum(t["hit"] for t in ts) / n * 100 if n else 0, "gross": sum(rs) / n if n else 0,
            "net": sum(net) / n if n else 0, "t": tstat(net)}


def analyse(trades, split, cost):
    cut, rows = split.isoformat(), []
    for name, _ in PATTERNS:
        for rr in RRS:
            ev = [t for t in trades if t["pattern"] == name and t["rr"] == rr]
            a = summarize([t for t in ev if t["date"] < cut], cost)
            b = summarize([t for t in ev if t["date"] >= cut], cost)
            per = {k: summarize([t for t in ev if t["index"] == k], cost) for k in sorted({t["index"] for t in ev})}
            ok = (a["n"] >= MIN_N and b["n"] >= MIN_N and abs(a["t"]) >= T_DISC and
                  (a["net"] > 0) == (b["net"] > 0) and abs(b["t"]) >= T_TEST and b["net"] > 0 and a["net"] > 0)
            rows.append({"pattern": name, "rr": rr, "be": 100 / (1 + rr), "disc": a, "test": b, "per": per, "pass": ok})
    return rows


def report(rows, label, split, days, path, cost, n_trades):
    n_tests = len(rows)
    passes = [r for r in rows if r["pass"]]
    p1 = 2 * (1 - 0.5 * (1 + math.erf(T_DISC / math.sqrt(2)))) * (1 - 0.5 * (1 + math.erf(T_TEST / math.sqrt(2))))
    chance = n_tests * p1 / 2                                         # both halves positive only
    hi_win = [r for r in rows if r["test"]["win"] >= 65 and r["test"]["n"] >= MIN_N]
    hi_win_profit = [r for r in hi_win if r["test"]["net"] > 0 and r["disc"]["net"] > 0]
    if passes:
        best = max(passes, key=lambda r: r["test"]["t"])
        kind = "good" if len(passes) > 2 * max(chance, 0.1) else "warn"
        head = f"{len(passes)} of {n_tests} pattern tests passed (chance alone: ~{chance:.2f})."
        body = (f"Best: {best['pattern']} at {best['rr']}R target - unseen half: win {best['test']['win']:.0f}% "
                f"(break-even {best['be']:.0f}%), net {best['test']['net']:+.2f}R/trade, t = {best['test']['t']:.2f}, n = {best['test']['n']}. "
                f"This is a pattern on the INDEX after a {cost:.2f}R cost - the next step is an option-level test of just this one.")
    else:
        kind, head = "bad", f"NO PATTERN WORKS (0 of {n_tests} tests passed)."
        body = ("After costs, no pattern had a positive, repeatable expectancy on the unseen half. A high win rate "
                "was possible only with small targets, where it did not cover the break-even level. Nothing was tuned.")
    wr = (f"{len(hi_win)} test rows reached a win rate of 65%+ on the unseen half; "
          f"{len(hi_win_profit)} of them were also profitable on BOTH halves." if hi_win else
          "No pattern reached a 65% win rate on the unseen half (with n >= 30).")

    def cell(s):
        c = "pos" if s["t"] >= 2 else "neg" if s["t"] <= -2 else ""
        return f"<td>{s['n']}</td><td>{s['win']:.0f}%</td><td class='{c}'>{s['net']:+.2f}</td><td class='{c}'>{s['t']:+.1f}</td>"

    trs = ""
    for r in rows:
        per = " · ".join(f"{k}: {v['net']:+.2f}R (n={v['n']})" for k, v in r["per"].items())
        trs += (f"<tr{' class=sel' if r['pass'] else ''}><td>{html.escape(r['pattern'])}</td><td>{r['rr']:g}R</td><td>{r['be']:.0f}%</td>"
                f"{cell(r['disc'])}{cell(r['test'])}<td>{'<b class=pos>PASS</b>' if r['pass'] else ''}</td><td class='tx'>{per}</td></tr>")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Pattern Lab</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{xb.CSS} td.tx{{font-size:11px;color:var(--mut)}}</style></head><body>
<h1>Pattern Lab {html.escape(label)}</h1>
<div class="sub">{days[0]} to {days[-1]} · discovery half: before {split} · unseen half: {split} onward · {n_trades // len(RRS):,} pattern trades ·
NIFTY + SENSEX pooled · stop = 1 ATR(5m), target = RR × stop · cost {cost:.2f}R per trade</div>
<div class="banner {kind}"><b>{html.escape(head)}</b><br>{html.escape(body)}</div>
<div class="note"><b>Win rate check:</b> {html.escape(wr)} Break-even win rate = 1/(1+RR): 0.5R needs 67%, 1R needs 50%, 2R needs 33%.
A 65-70% win rate with a tiny target is not an edge - look at the Net R columns.</div>
{("<div class='note'><b>DEMO DATA - NOT YOUR MARKET.</b> " + ("Pure random walk: nothing should pass, and win rates should sit near break-even." if "null" in label else "Synthetic momentum bursts: continuation patterns may pass.") + "</div>") if "DEMO" in label else ""}
<h2>All {n_tests} pre-declared tests</h2>
<p class="sub">Net R = average R per trade after the cost. t = t-statistic of net R. Pass: n ≥ {MIN_N} per half, |t| ≥ {T_DISC} on discovery, same sign (and positive) with t ≥ {T_TEST} on the unseen half.</p>
<table><tr><th rowspan=2>Pattern</th><th rowspan=2>Target</th><th rowspan=2>Break-even win</th><th colspan=4>Discovery half</th><th colspan=4>Unseen half</th><th rowspan=2></th><th rowspan=2>Per index (all data, net R)</th></tr>
<tr><th>n</th><th>Win</th><th>Net R</th><th>t</th><th>n</th><th>Win</th><th>Net R</th><th>t</th></tr>{trs}</table>
<div class="note"><b>Context:</b> SEBI's studies (FY22-FY25) found that over 90% of individual F&amp;O traders lose money, and that loss-making rises with experience.
I found no study showing chart patterns profitable after costs; guides quoting 70-88% "success rates" do not say how they were measured or
whether costs were included. {n_tests} tests were run; about {chance:.2f} passes are expected by luck.</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return passes


def main():
    ap = argparse.ArgumentParser(description="Pattern Lab")
    ap.add_argument("--demo", nargs="?", const="null", choices=["null", "trend"])
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--data", default="data")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--cost-r", type=float, default=0.15, help="cost per trade in R (default 0.15)")
    a = ap.parse_args()
    indices = [k for k, v in eb.CONFIG["indices"].items() if v["enabled"]]
    if a.demo:
        mk = xb.null_market if a.demo == "null" else bt0.synthetic
        data, _ = mk(indices, round(a.days * 0.69))
        label, out = f"(DEMO {a.demo} - synthetic data)", f"pattern_lab_report_DEMO_{a.demo}.html"
    else:
        if not a.no_download:
            try:
                xb.sync_data(xb.connect(), indices, a.data, a.days)
            except SystemExit as e:
                print(f"Could not log in / download: {e}")
                if not glob.glob(os.path.join(a.data, "*.csv")):
                    sys.exit("No data available. Fill in .env (see README) or use --demo.")
        data = xb.load_dir(a.data)
        cutoff = int(dt.datetime.combine(dt.datetime.now(IST).date() - dt.timedelta(days=a.days), dt.time(0), IST).timestamp())
        data = {k: [c for c in v if c[0] >= cutoff] for k, v in data.items()}
        if any(not data.get(k) for k in indices):
            sys.exit("Missing NIFTY/SENSEX candle files in data/.")
        label, out = "", "pattern_lab_report.html"
    trades, all_days = [], set()
    for k in indices:
        days = build_days([c for d, cs in sorted(xb.by_day(data[k]).items()) for c in cs])
        all_days |= {d["date"] for d in days}
        trades += build_trades(days, k)
    all_days = sorted(all_days)
    split = all_days[0] + (all_days[-1] - all_days[0]) / 2
    print(f"{len(all_days)} trading days {all_days[0]} .. {all_days[-1]} | discovery < {split} <= unseen | cost {a.cost_r}R")
    rows = analyse(trades, split, a.cost_r)
    passes = report(rows, label, split, all_days, out, a.cost_r, len(trades))
    import json
    best = max(passes, key=lambda r: r["test"]["t"], default=None)
    json.dump({"tests": len(rows), "passes": len(passes), "generated": dt.date.today().isoformat(), "demo": bool(a.demo),
               "best": best and {"pattern": best["pattern"], "rr": best["rr"], "win": best["test"]["win"],
                                 "net_r": best["test"]["net"], "t": best["test"]["t"]}}, open("pattern_lab_summary.json", "w"))
    print(f"{len(rows)} tests, {len(passes)} passed")
    for r in passes:
        print(f"  PASS {r['pattern']} {r['rr']}R  discovery {r['disc']['net']:+.2f}R (t {r['disc']['t']:.2f}) "
              f"unseen {r['test']['net']:+.2f}R (t {r['test']['t']:.2f}) win {r['test']['win']:.0f}%")
    best_win = max(rows, key=lambda r: r["test"]["win"] if r["test"]["n"] >= MIN_N else -1)
    print(f"Highest unseen win rate: {best_win['pattern']} {best_win['rr']}R -> {best_win['test']['win']:.0f}% "
          f"(break-even {best_win['be']:.0f}%, net {best_win['test']['net']:+.2f}R)")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
