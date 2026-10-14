"""
SIGNAL LAB - does any entry signal actually PREDICT the index?  (no option-price modelling at all)
=================================================================================================
Why: backtesting on modelled option premiums mixes two questions - "does the signal predict?" and "are my assumed
option prices right?". The second depends on assumptions I make up. This lab answers ONLY the first, using your
real 1-minute NIFTY / SENSEX candles. If nothing predicts the index, no option strategy built on it can work.

Method (fixed in advance, nothing tuned):
  * 14 pre-declared signals (the 09:30 / 12:00 / 14:00 time-anchored breakouts, momentum candles in those windows, and 8 others), each fires at most ONCE per day per index (no overlapping samples).
  * After a signal, entry = next bar open; we measure the signed index move (basis points, in the signal's
    direction) after 15 / 30 / 60 minutes (or until 15:15, whichever is first).
  * Both indices are pooled (more samples), and the year is split: first half = discovery, second half = unseen test.
  * 14 signals x 3 horizons = 42 pooled tests. Testing many things produces false positives by luck, so a
    result only "passes" if |t| >= 2.5 on discovery AND the SAME sign with |t| >= 1.65 on the unseen half.
    The report prints how many passes chance alone would give.
  * A "fade" is just a negative result for a "follow" signal; the sign of the mean tells you which.

RUN   python signal_lab.py                (logs in, downloads data/ like expiry_backtest, default 365 days)
      python signal_lab.py --no-download  (use data/ as is)      python signal_lab.py --demo null|trend
OUT   signal_lab_report.html (or signal_lab_report_DEMO_<kind>.html)
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
HORIZONS = (15, 30, 60)
T_DISCOVERY, T_TEST = 2.5, 1.65
S0 = eb.compile_strategy(dict(eb.CONFIG["strategy"], mode="MOM"))


# =====================================================================================
# Pre-declared signals. Each: fn(f, i, ctx) -> +1 / -1 / 0 for the bar that just closed (index i).
# =====================================================================================
def sig_orb(f, i, ctx):
    if f["orb_hi"] is None or i < f["orb_n"] or not (570 <= f["mclose"][i] <= 660):
        return 0
    c, pc = f["c"][i], f["c"][i - 1]
    if c > f["orb_hi"] >= pc:
        return 1
    if c < f["orb_lo"] <= pc:
        return -1
    return 0


def sig_ext_pm(f, i, ctx):
    if not (780 <= f["mclose"][i] <= 885) or f["hi_prior"][i] is None:
        return 0
    c = f["c"][i]
    return 1 if c > f["hi_prior"][i] else -1 if c < f["lo_prior"][i] else 0


def sig_mom(f, i, ctx):
    atr = f["atr"][i]
    if not atr or not (570 <= f["mclose"][i] <= 885):
        return 0
    rng, body = f["h"][i] - f["l"][i], abs(f["c"][i] - f["o"][i])
    if rng >= 1.5 * atr and rng > 0 and body >= 0.6 * rng:
        return 1 if f["c"][i] > f["o"][i] else -1
    return 0


def sig_vwap_dev(f, i, ctx):
    """Far from VWAP -> sign is the FOLLOW direction (price above VWAP => +1); a fade shows as negative mean."""
    atr = f["atr"][i]
    if not atr or not (600 <= f["mclose"][i] <= 870):
        return 0
    dev = f["c"][i] - f["vwap"][i]
    return 1 if dev >= 5 * atr else -1 if dev <= -5 * atr else 0


def sig_gap(f, i, ctx):
    if f["mclose"][i] != 570 or not ctx.get("prev_close"):
        return 0
    gap = f["o"][0] / ctx["prev_close"] - 1
    return 1 if gap >= 0.003 else -1 if gap <= -0.003 else 0


def sig_open30(f, i, ctx):
    if f["mclose"][i] != 585:
        return 0
    r = f["c"][i] / f["o"][0] - 1
    return 1 if r >= 0.0025 else -1 if r <= -0.0025 else 0


def sig_mtf_pullback(f, i, ctx):
    m = (f.get("mtf") or [None] * f["n"])[i]
    if not m or i < 22 or not (600 <= f["mclose"][i] <= 870):
        return 0
    c, pc, es = f["c"][i], f["c"][i - 1], f["ema_s"][i]
    if m[15] == 1 and m[60] == 1 and pc < f["ema_s"][i - 1] and c >= es:
        return 1
    if m[15] == -1 and m[60] == -1 and pc > f["ema_s"][i - 1] and c <= es:
        return -1
    return 0


def sig_trend_all(f, i, ctx):
    """5m, 15m and 1h all agree AND price is on that side of VWAP - first bar of the day where that is true."""
    m = (f.get("mtf") or [None] * f["n"])[i]
    if not m or not (585 <= f["mclose"][i] <= 840):
        return 0
    s = {m[5], m[15], m[60]}
    if s == {1} and f["c"][i] > f["vwap"][i]:
        return 1
    if s == {-1} and f["c"][i] < f["vwap"][i]:
        return -1
    return 0


def make_anchor(A_str):
    """Raw (unfiltered) break of the range before an anchor time, within the span after it. Follow = +, fade = negative mean."""
    spec = [x for x in S0["anchors"] if x[0] == eb._m(A_str)][0]
    A, _w, span = spec

    def fn(f, i, ctx):
        t = f["mclose"][i]
        rng = f["anchor"].get(A)
        if not rng or not (A < t <= A + span):
            return 0
        c, pc = f["c"][i], f["c"][i - 1]
        if c > rng[0] >= pc:
            return 1
        if c < rng[1] <= pc:
            return -1
        return 0
    return fn


def make_window_mom(lo, hi):
    """Momentum candle (body>=60% of range, range>=1.5xATR) inside a clock window."""
    def fn(f, i, ctx):
        atr = f["atr"][i]
        if not atr or not (lo <= f["mclose"][i] <= hi):
            return 0
        rng, body = f["h"][i] - f["l"][i], abs(f["c"][i] - f["o"][i])
        if rng >= 1.5 * atr and rng > 0 and body >= 0.6 * rng:
            return 1 if f["c"][i] > f["o"][i] else -1
        return 0
    return fn


SIGNALS = [("09:30 break of 09:15-09:30 range", make_anchor("09:30")),
           ("12:00 break of 11:30-12:00 range", make_anchor("12:00")),
           ("14:00 break of 13:30-14:00 range", make_anchor("14:00")),
           ("Momentum candle 09:30-11:00", make_window_mom(570, 660)),
           ("Momentum candle 12:00-13:30", make_window_mom(720, 810)),
           ("Momentum candle 14:00-14:45", make_window_mom(840, 885)),
           ("ORB break (09:30-11:00)", sig_orb), ("Day-high/low break (13:00-14:45)", sig_ext_pm),
           ("Momentum candle (body>=60%, range>=1.5xATR)", sig_mom), ("Far from VWAP (>=5 ATR)", sig_vwap_dev),
           ("Opening gap >=0.3% (at 09:30)", sig_gap), ("First-30-min move >=0.25% (at 09:45)", sig_open30),
           ("5m/15m/1h trend pullback to EMA21", sig_mtf_pullback), ("5m+15m+1h all agree + VWAP side", sig_trend_all)]


# =====================================================================================
def build_events(data, indices):
    """-> list of dicts {signal, horizon, index, date, bps} for the first firing of every signal each day."""
    events = []
    for k in indices:
        days = xb.by_day(data[k])
        full = [c for d in sorted(days) for c in days[d]]
        trend = eb.htf_trend(full, (5, 15, 60))
        prev_close = None
        cal = bt0.ExpiryCalendar(sorted(days))
        for d in sorted(days):
            bars = days[d]
            is_exp = cal.nearest(k, d) == d
            if len(bars) < 100:
                prev_close = bars[-1][4] if bars else prev_close
                continue
            f = eb.day_features(bars, S0)
            f["mtf"] = [eb.mtf_signs_at(trend, b[0] + 60) for b in bars]
            ctx = {"prev_close": prev_close}
            for name, fn in SIGNALS:
                for i in range(S0["warmup_bars"], len(bars) - 2):
                    s = fn(f, i, ctx)
                    if not s:
                        continue
                    t_entry = f["mclose"][i]                               # next bar opens at this minute
                    if t_entry >= 915:
                        break
                    entry = bars[i + 1][1]
                    for H in HORIZONS:
                        j = min(i + 1 + H, len(bars) - 1)
                        j = min(j, max(x for x in range(len(bars)) if f["mclose"][x] <= 915))
                        events.append({"signal": name, "horizon": H, "index": k, "date": d.isoformat(),
                                       "bps": s * (bars[j][1] / entry - 1) * 1e4, "exp": is_exp})
                    break                                                  # one firing per day per signal
            prev_close = bars[-1][4]
    return events


def tstats(xs):
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": 0.0, "hit": 0.0, "t": 0.0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return {"n": n, "mean": m, "hit": sum(x > 0 for x in xs) / n * 100, "t": m / (sd / math.sqrt(n)) if sd else 0.0}


def analyse(events, split):
    cut = split.isoformat()
    rows = []
    for name, _ in SIGNALS:
        for H in HORIZONS:
            ev = [e for e in events if e["signal"] == name and e["horizon"] == H]
            a = tstats([e["bps"] for e in ev if e["date"] < cut])
            b = tstats([e["bps"] for e in ev if e["date"] >= cut])
            per = {k: tstats([e["bps"] for e in ev if e["index"] == k]) for k in sorted({e["index"] for e in ev})}
            ex = tstats([e["bps"] for e in ev if e.get("exp")])
            passed = (abs(a["t"]) >= T_DISCOVERY and a["n"] >= 25 and b["n"] >= 25 and
                      (a["mean"] > 0) == (b["mean"] > 0) and abs(b["t"]) >= T_TEST)
            rows.append({"signal": name, "H": H, "disc": a, "test": b, "per": per, "exp": ex, "pass": passed,
                         "side": "FOLLOW" if a["mean"] > 0 else "FADE"})
    return rows


def report(rows, label, split, days, path, n_events):
    n_tests = len(rows)
    passes = [r for r in rows if r["pass"]]
    p_chance = 2 * (1 - 0.5 * (1 + math.erf(T_DISCOVERY / math.sqrt(2)))) * (1 - 0.5 * (1 + math.erf(T_TEST / math.sqrt(2))))
    chance = n_tests * p_chance
    if passes:
        best = max(passes, key=lambda r: abs(r["test"]["t"]))
        kind, head = "good" if len(passes) > 2 * max(chance, 0.1) else "warn", f"{len(passes)} of {n_tests} tests passed (chance alone: ~{chance:.2f})."
        body = (f"Best: {best['signal']} ({best['side']}, {best['H']} min): unseen-half mean {best['test']['mean']:+.1f} bps/trade, "
                f"t = {best['test']['t']:.2f}, n = {best['test']['n']}. A pass means the INDEX moved predictably - it is not yet a "
                f"profitable option trade. Next step would be to build the option layer on this signal only.")
    else:
        kind, head = "bad", f"NO SIGNAL PREDICTS THE INDEX (0 of {n_tests} tests passed)."
        body = ("None of the pre-declared signals showed a repeatable move on the unseen half. No option strategy built on "
                "these entries can have an edge. Nothing was tuned.")

    def cell(s):
        c = "pos" if s["t"] >= 2 else "neg" if s["t"] <= -2 else ""
        return f"<td>{s['n']}</td><td class='{c}'>{s['mean']:+.1f}</td><td>{s['hit']:.0f}%</td><td class='{c}'>{s['t']:+.2f}</td>"

    trs = ""
    for r in rows:
        per = " · ".join(f"{k} {v['mean']:+.1f}" for k, v in r["per"].items()) + \
            f" · expiry days only: {r['exp']['mean']:+.1f} (n={r['exp']['n']})"
        mark = "<b class='pos'>PASS</b>" if r["pass"] else ""
        trs += (f"<tr{' class=sel' if r['pass'] else ''}><td>{html.escape(r['signal'])}</td><td>{r['H']}m</td>"
                f"{cell(r['disc'])}{cell(r['test'])}<td>{mark}</td><td class='tx'>{per}</td></tr>")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Signal Lab</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{xb.CSS} td.tx{{font-size:11px;color:var(--mut)}}</style></head><body>
<h1>Signal Lab {html.escape(label)}</h1>
<div class="sub">{days[0]} to {days[-1]} · discovery half: before {split} · unseen half: {split} onward · {n_events//len(HORIZONS):,} signal firings ·
NIFTY + SENSEX pooled · returns in basis points (1 bp = 0.01%) in the signal's direction, from next-bar open</div>
<div class="banner {kind}"><b>{html.escape(head)}</b><br>{html.escape(body)}</div>
{("<div class='note'><b>DEMO DATA - NOT YOUR MARKET.</b> " + ("Pure random walk: nothing should pass." if "null" in label else "Built-in momentum bursts: momentum signals should pass.") + "</div>") if "DEMO" in label else ""}
<h2>All {n_tests} pre-declared tests</h2>
<p class="sub">Pass rule: |t| ≥ {T_DISCOVERY} on the discovery half, same sign and |t| ≥ {T_TEST} on the unseen half, ≥ 25 samples each. t is the t-statistic of the mean (≈ how many standard errors from zero). Positive = FOLLOW the signal, negative = FADE it.
Last column: mean bps per index (discovery+unseen together) as a consistency check.</p>
<table><tr><th rowspan=2>Signal</th><th rowspan=2>Hold</th><th colspan=4>Discovery half</th><th colspan=4>Unseen half</th><th rowspan=2></th><th rowspan=2>Per index</th></tr>
<tr><th>n</th><th>mean bps</th><th>up%</th><th>t</th><th>n</th><th>mean bps</th><th>up%</th><th>t</th></tr>{trs}</table>
<div class="note"><b>How to use this:</b> an option BUYER also pays time decay, IV and ~4-5% round-trip slippage, so the index move must be
large enough to cover that - on a NIFTY expiry day an ATM option costs roughly 20-40 bps of the index per hour in decay alone.
Typical significant edges in intraday index data are a few bps, which is why a +25%/month target is not a realistic outcome of an honest edge.
{n_tests} tests were run; about {chance:.2f} passes are expected from pure luck.</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return passes


def main():
    ap = argparse.ArgumentParser(description="Signal Lab")
    ap.add_argument("--demo", nargs="?", const="null", choices=["null", "trend"])
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--data", default="data")
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()
    indices = [k for k, v in eb.CONFIG["indices"].items() if v["enabled"]]
    if a.demo:
        mk = xb.null_market if a.demo == "null" else bt0.synthetic
        data, vix = mk(indices, round(a.days * 0.69))
        label, out = f"(DEMO {a.demo} - synthetic data)", f"signal_lab_report_DEMO_{a.demo}.html"
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
        label, out = "", "signal_lab_report.html"
    days = sorted({d for k in indices for d in xb.by_day(data[k])})
    split = days[0] + (days[-1] - days[0]) / 2
    print(f"{len(days)} trading days {days[0]} .. {days[-1]} | discovery < {split} <= unseen")
    events = build_events(data, indices)
    rows = analyse(events, split)
    passes = report(rows, label, split, days, out, len(events))
    print(f"{len(rows)} tests, {len(passes)} passed (|t|>={T_DISCOVERY} discovery, same sign |t|>={T_TEST} unseen)")
    for r in passes:
        print(f"  PASS {r['signal']} {r['side']} {r['H']}m  discovery {r['disc']['mean']:+.1f}bps (t {r['disc']['t']:.2f}) "
              f"unseen {r['test']['mean']:+.1f}bps (t {r['test']['t']:.2f})")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
