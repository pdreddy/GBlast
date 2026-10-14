"""
EXPIRY BLAST - BACKTEST (tune on first half of the data, report the second half UNTOUCHED)
=========================================================================================
Data   : data/*.csv  = real 1-minute index candles (epoch, open, high, low, close, volume).
         File names must contain NIFTY / SENSEX / VIX (BANKNIFTY files are ignored).
Options: expired option prices do not exist, so premiums are MODELLED:
           Black-Scholes on the real index path, IV = India VIX x expiry-day multiplier that
           decays through the day (expiry IV adjustment), an extra IV fade after 13:00
           (theta acceleration; Black-Scholes itself already accelerates decay as T -> 0),
           slippage >= 2% per side, and full charges (brokerage, STT, exchange, SEBI, GST, stamp).
         If one bar touches both stop and target, the STOP is assumed to hit first.
Multi-timeframe: entries need the 5m/15m(/1h) EMA9-vs-EMA21 trend (built from 1-min data across days) to agree.
Strategies: time-anchored breakouts at 09:30 / 12:00 / 14:00 (alone and all three together).
Honesty: 16 simple variants are evaluated on the TUNE half only; the best by profit factor is frozen
         and run on the TEST half with nothing changed. All 20 are shown on both halves for transparency.
         The verdict says plainly if there is no edge. Nothing is tuned to look good.

RUN
  python expiry_backtest.py                 # reads data/*.csv
  python expiry_backtest.py --demo          # synthetic random-walk data: proves the machinery works
                                            # (a random walk must show NO edge)
OUTPUT  expiry_report.html (or expiry_report_DEMO.html), expiry_trades.csv
"""
import argparse
import csv
import datetime as dt
import glob
import html
import itertools
import math
import os
import sys
from collections import defaultdict

import expiry_blast as eb
import backtest as bt0                      # ExpiryCalendar + synthetic data generator (existing project code)

IST = eb.IST
C = eb.CONFIG
YEAR_MIN = 365 * 24 * 60
DAY_OPEN, DAY_CLOSE = 9 * 60 + 15, 15 * 60 + 30

# ----------------------------- modelling assumptions (all visible in the report) -----------------------------
BT = {
    "default_vix": 14.0,
    "iv_mult_open": 1.35,        # expiry-day ATM IV runs well above India VIX (VIX blends later expiries)...
    "iv_mult_close": 1.00,       # ...and fades through the day
    "iv_extra_fade": 0.15,       # additional IV fade between 13:00 and 15:30 (theta acceleration)
    "slippage_pct": 2.0,         # per side, applied to entries AND every exit (stop, target, square-off)
    "risk_free": 0.065,
    "brokerage_per_order": 20.0,
    "stt_before": 0.10, "stt_after": 0.15, "stt_change": dt.date(2026, 4, 1),   # % on sell premium - VERIFY on your contract note
    "exch_pct": {"NIFTY": 0.03503, "SENSEX": 0.0325},
    "lot_sizes": {"NIFTY": [(dt.date(2000, 1, 1), 75), (dt.date(2026, 1, 1), 65)],
                  "SENSEX": [(dt.date(2000, 1, 1), 20)]},                         # VERIFY: exchange circulars
}

MORNING, MID, AFTERNOON = "09:30 breakout", "12:00 breakout", "14:00 breakout"


def lot_size(index, day, bt):
    lot = bt["lot_sizes"][index][0][1]
    for since, v in bt["lot_sizes"][index]:
        if day >= since:
            lot = v
    return lot


def window_of(hhmm):
    m = eb._m(hhmm)
    return MORNING if m < 660 else MID if m < 810 else AFTERNOON


# =====================================================================================
# Data loading
# =====================================================================================
def _classify(path):
    n = os.path.basename(path).upper()
    if "VIX" in n:
        return "VIX"
    if "BANK" in n:
        return None
    if "SENSEX" in n:
        return "SENSEX"
    if "NIFTY" in n:
        return "NIFTY"
    return None


def load_dir(folder="data"):
    out = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        key = _classify(path)
        if not key:
            continue
        with open(path) as f:
            for r in csv.reader(f):
                try:
                    ts = int(float(r[0]))
                    row = [ts // 1000 if ts > 10 ** 11 else ts] + [float(x) for x in r[1:5]] + \
                          [float(r[5]) if len(r) > 5 and r[5] != "" else 0.0]
                except (ValueError, IndexError):
                    continue                                    # header / junk line
                out[key][row[0]] = row
    return {k: [v[t] for t in sorted(v)] for k, v in out.items()}


def fetch_range(broker, symbol, start, end):
    """1-minute candles from FYERS in 60-day chunks (FYERS limits intraday history per request)."""
    out, cur = [], start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=59), end)
        r = broker._call(broker.f.history, {
            "symbol": symbol, "resolution": "1", "date_format": "1",
            "range_from": cur.strftime("%Y-%m-%d"), "range_to": stop.strftime("%Y-%m-%d"), "cont_flag": "1"})
        if r.get("s") != "ok" and not r.get("candles"):
            if "authenticate" in str(r.get("message", "")).lower() or r.get("code") in (-16, -8, -15):
                raise SystemExit("FYERS rejected the login token (Could not authenticate). Delete access_token.txt and run again.")
            print(f"   warning: {symbol} {cur}..{stop}: {r.get('message', r)}")
        out += r.get("candles") or []
        cur = stop + dt.timedelta(days=1)
    return out


def sync_data(broker, indices, folder="data", days=365, today=None):
    """Download whatever 1-minute history is missing (NIFTY, SENSEX, India VIX) into data/, covering the last
    `days` calendar days. Later runs fetch only new days; asking for more days than before back-fills the older part."""
    os.makedirs(folder, exist_ok=True)
    today = today or dt.datetime.now(IST).date()
    want_start = today - dt.timedelta(days=days)
    have = load_dir(folder)
    wanted = {k: C["indices"][k]["spot"] for k in indices}
    wanted["VIX"] = "NSE:INDIAVIX-INDEX"

    def save(key, symbol, a, b, existing_ts):
        print(f"Downloading {key} {a} .. {b}")
        new = [c for c in fetch_range(broker, symbol, a, b) if c[0] not in existing_ts]
        if not new:
            print(f"   no new candles for {key}")
            return
        fn = os.path.join(folder, f"{key}_1min_{a}_{b}.csv")
        with open(fn, "w", newline="") as f:
            csv.writer(f).writerows([[int(c[0])] + [float(x) for x in c[1:5]] + [float(c[5]) if len(c) > 5 else 0.0]
                                     for c in sorted(new, key=lambda c: c[0])])
        print(f"   {len(new):,} candles -> {fn}")

    for key, symbol in wanted.items():
        rows = have.get(key) or []
        ts = {r[0] for r in rows}
        if not rows:
            save(key, symbol, want_start, today, ts)
            continue
        first = dt.datetime.fromtimestamp(rows[0][0], IST).date()
        last = dt.datetime.fromtimestamp(rows[-1][0], IST).date()
        if first > want_start + dt.timedelta(days=5):              # older history requested than we have
            save(key, symbol, want_start, first, ts)
        if last < today:
            save(key, symbol, last, today, ts)
        if first <= want_start + dt.timedelta(days=5) and last >= today:
            print(f"{key}: up to date")


def token_valid(broker):
    """True if FYERS accepts the saved access token (a cheap profile call)."""
    try:
        resp = broker._call(broker.f.get_profile, retries=1)
    except Exception:
        return False
    return isinstance(resp, dict) and resp.get("s") == "ok"


def connect():
    """Return a Broker with a WORKING token. The token file is verified against FYERS (file date alone is not trusted);
    if it is missing, stale or invalid you get the login prompt (one auth_code paste)."""
    import fyers_login
    broker = None
    if fyers_login.token_is_from_today():
        try:
            broker = eb.g.Broker(C)
        except Exception:
            broker = None
    if broker is None or not token_valid(broker):
        print("Saved token is missing, expired or invalid - logging in to FYERS again.")
        fyers_login.login()
        broker = eb.g.Broker(C)
        if not token_valid(broker):
            raise SystemExit("FYERS still rejects the token after login. Check FYERS_CLIENT_ID / FYERS_SECRET_KEY in .env "
                             "(python3 config.py) and that the auth_code was pasted fresh and complete.")
    print("FYERS login OK.")
    return broker


def by_day(candles):
    days = defaultdict(list)
    for c in candles:
        t = dt.datetime.fromtimestamp(c[0], IST)
        if dt.time(9, 15) <= t.time() < dt.time(15, 30):
            days[t.date()].append(c)
    return days


# =====================================================================================
# Option model
# =====================================================================================
def iv_for(vix_pct, minute, bt):
    """minute = minute-of-day at which the price applies."""
    frac = min(max((minute - DAY_OPEN) / (DAY_CLOSE - DAY_OPEN), 0.0), 1.0)
    mult = bt["iv_mult_open"] + (bt["iv_mult_close"] - bt["iv_mult_open"]) * frac
    late = min(max((minute - 13 * 60) / 150.0, 0.0), 1.0)
    return vix_pct / 100 * mult * (1 - bt["iv_extra_fade"] * late)


def premium(spot, K, minute, vix_pct, cp, bt):
    T = max(DAY_CLOSE - minute, 0.5) / YEAR_MIN
    return max(eb.g.bs_price(spot, K, T, bt["risk_free"], iv_for(vix_pct, minute, bt), cp), 0.05)


def charges(index, entry, exit_, qty, day, bt):
    buy, sell = entry * qty, exit_ * qty
    brokerage = 2 * bt["brokerage_per_order"]
    stt = (bt["stt_after"] if day >= bt["stt_change"] else bt["stt_before"]) / 100 * sell
    exch = bt["exch_pct"][index] / 100 * (buy + sell)
    sebi = 10 / 1e7 * (buy + sell)
    stamp = 0.00003 * buy
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


# =====================================================================================
# Synthetic markets (for --demo only)
# =====================================================================================
def null_market(indices, n_days, seed=11):
    """Pure random walk with FAIRLY PRICED options: independent minute returns whose volatility equals the vol the
    premium model charges at that minute (so a buyer has no edge before costs - only theta, slippage and charges)."""
    import random
    rnd = random.Random(seed)
    base = {"NIFTY": 25000.0, "SENSEX": 82000.0}
    data, vix = {k: [] for k in indices}, []
    d = dt.date.today() - dt.timedelta(days=int(n_days * 1.45))
    made = 0
    while made < n_days:
        d += dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        made += 1
        start = int(dt.datetime.combine(d, dt.time(9, 15), IST).timestamp())
        v = 12 + rnd.random() * 6
        s_ = 0.25
        dayf = math.exp(rnd.gauss(0, s_) - s_ * s_)                          # E[dayf^2] = 1: fair on average
        for i in range(375):
            sig = iv_for(v, DAY_OPEN + i, BT) / math.sqrt(YEAR_MIN) * dayf   # same basis as premium()
            ts = start + 60 * i
            vix.append([ts, v, v, v, v, 0])
            for k in indices:
                p = q = hi = lo = base[k]
                for _ in range(4):                                           # 4 sub-steps give realistic high/low
                    q *= 1 + rnd.gauss(0, sig / 2)
                    hi, lo = max(hi, q), min(lo, q)
                data[k].append([ts, p, hi, lo, q, 0])
                base[k] = q
    return data, vix


# =====================================================================================
# Simulator (one index, one expiry day)
# =====================================================================================
def simulate_day(index, day, bars, feat, vix_at, S, ex, bt, cfg):
    """S = compiled strategy, ex = {'breakeven': bool}. Returns list of trade dicts."""
    trades, pos, pending, n_trades = [], None, None, 0
    lot = lot_size(index, day, bt)
    step = cfg["indices"][index]["strike_step"]
    slip = bt["slippage_pct"] / 100
    sq_min, entry_end = eb._m(cfg["square_off"]), eb._m(cfg["entry_end"])
    sl_pct, tg_pct = cfg["stop_loss_pct"], cfg["target_pct"]

    def close_out(minute, raw_px, reason):
        nonlocal pos
        fill = raw_px * (1 - slip)
        gross = (fill - pos["entry"]) * pos["qty"]
        cst = charges(index, pos["entry"], fill, pos["qty"], day, bt)
        trades.append({
            "date": day.isoformat(), "index": index, "type": pos["cp"], "strike": pos["K"],
            "entry_time": pos["t_in"], "exit_time": f"{minute // 60:02d}:{minute % 60:02d}",
            "window": window_of(pos["t_in"]), "spot_in": round(pos["spot_in"], 2), "lots": pos["lots"],
            "qty": pos["qty"], "entry": round(pos["entry"], 2), "exit": round(fill, 2),
            "ret_pct": round((fill / pos["entry"] - 1) * 100, 1), "gross": round(gross, 0),
            "costs": round(cst, 0), "net": round(gross - cst, 0), "reason": reason})
        pos = None

    for i, bar in enumerate(bars):
        t = (bar[0] + 19800) % 86400 // 60                       # bar start, minute of day
        vix = vix_at(bar[0])
        o_s, h_s, l_s, c_s = bar[1], bar[2], bar[3], bar[4]

        # 1. pending entry fills at this bar's open
        if pending and pos is None:
            p_open = premium(o_s, pending["K"], t, vix, pending["cp"], bt)
            pending_ = pending
            pending = None
            if S["min_premium"] <= p_open <= S["max_premium"]:
                fill = p_open * (1 + slip)
                lots = eb.size_lots(cfg["capital"], cfg["risk_per_trade_pct"], fill, sl_pct, lot,
                                    cfg["indices"][index]["max_lots"], cfg["max_capital_per_trade_pct"])
                if lots >= 1:
                    pos = {"cp": pending_["cp"], "K": pending_["K"], "entry": fill, "lots": lots,
                           "qty": lots * lot, "sl": fill * (1 - sl_pct / 100), "tgt": fill * (1 + tg_pct / 100),
                           "peak": fill, "be": False, "t_in": f"{t // 60:02d}:{t % 60:02d}", "spot_in": o_s}
                    n_trades += 1

        # 2. manage the open position through this bar
        if pos:
            tm = t + 0.5
            fav_s, adv_s = (h_s, l_s) if pos["cp"] == "CE" else (l_s, h_s)
            pr_open = premium(o_s, pos["K"], t, vix, pos["cp"], bt)
            pr_fav = premium(fav_s, pos["K"], tm, vix, pos["cp"], bt)
            pr_adv = premium(adv_s, pos["K"], tm, vix, pos["cp"], bt)
            if t >= sq_min:
                close_out(t, pr_open, "SQUARE_OFF")
            elif pr_adv <= pos["sl"]:                              # stop checked FIRST (worst case)
                close_out(t, min(pos["sl"], pr_open), "BE" if pos["be"] else "SL")
            elif pr_fav >= pos["tgt"]:
                close_out(t, max(pos["tgt"], pr_open), "TARGET")
            else:
                pos["peak"] = max(pos["peak"], pr_fav)
                if ex["breakeven"] and not pos["be"] and \
                        (pos["peak"] / pos["entry"] - 1) * 100 >= cfg["breakeven_trigger_pct"]:
                    pos["be"], pos["sl"] = True, pos["entry"]       # effective from the NEXT bar
                if i == len(bars) - 1:                             # data ended before square-off
                    close_out(t + 1, premium(c_s, pos["K"], t + 1, vix, pos["cp"], bt), "SQUARE_OFF")

        # 3. look for a signal on this closed bar -> enter at next bar's open
        if pos is None and pending is None and n_trades < cfg["max_trades_per_index"] \
                and t + 1 <= entry_end and i + 1 < len(bars):
            side = eb.signal(feat, i, S)
            if side:
                pick = eb.pick_strike(c_s, side, step,
                                      lambda K: premium(c_s, K, t + 1, vix, side, bt), S)
                if pick:
                    pending = {"cp": side, "K": pick[0]}
    return trades


class Market:
    """Pre-computed per-day features so that many variants can be run cheaply."""

    def __init__(self, data, indices, cfg=C):
        self.cfg = cfg
        self.S0 = eb.compile_strategy(cfg["strategy"])
        vix = data.get("VIX", [])
        self.vix_series = {c[0]: c[4] for c in vix}
        self.vix_sorted = sorted(self.vix_series)
        self.vix_day = {}
        for c in vix:
            self.vix_day.setdefault(dt.datetime.fromtimestamp(c[0], IST).date(), c[4])
        self.indices = indices
        self.days_by_index = {k: by_day(data[k]) for k in indices}
        all_days = sorted(set().union(*[set(v) for v in self.days_by_index.values()]))
        self.days = all_days
        self.expiry_days = {}
        for k in indices:
            cal = bt0.ExpiryCalendar(sorted(self.days_by_index[k]))
            self.expiry_days[k] = [d for d in sorted(self.days_by_index[k]) if cal.nearest(k, d) == d]
        self.feat = {}
        for k in indices:
            full = [c for d in sorted(self.days_by_index[k]) for c in self.days_by_index[k][d]]
            trend = eb.htf_trend(full, (5, 15, 60))                 # 5m / 15m / 1h trend from ALL days (EMA warm-up)
            for d in self.expiry_days[k]:
                bars = self.days_by_index[k][d]
                if len(bars) >= 60:
                    f = eb.day_features(bars, self.S0)
                    f["mtf"] = [eb.mtf_signs_at(trend, b_[0] + 60) for b_ in bars]
                    self.feat[(k, d)] = (bars, f)

    def vix_fn(self, day):
        last = [self.vix_day.get(day, BT["default_vix"])]

        def at(ts):
            v = self.vix_series.get(ts)
            if v:
                last[0] = v
            return last[0]
        return at

    def run(self, strategy_overrides, breakeven, bt=BT, start=None, end=None):
        S = eb.compile_strategy({**self.cfg["strategy"], **strategy_overrides})
        ex = {"breakeven": breakeven}
        out = []
        for (k, d), (bars, feat) in sorted(self.feat.items(), key=lambda x: (x[0][1], x[0][0])):
            if (start and d < start) or (end and d >= end):
                continue
            out += simulate_day(k, d, bars, feat, self.vix_fn(d), S, ex, bt, self.cfg)
        return out


# =====================================================================================
# Variants and statistics
# =====================================================================================
ANCH = {"09:30": ("09:30", 15, 90), "12:00": ("12:00", 30, 90), "14:00": ("14:00", 30, 45)}
VARIANTS = []
for (label, keys), mtf, otm in itertools.product([
        ("09:30 breakout", ["09:30"]), ("12:00 breakout", ["12:00"]), ("14:00 breakout", ["14:00"]),
        ("All three (09:30+12:00+14:00)", ["09:30", "12:00", "14:00"])],
        [[5, 15], [5, 15, 60]], [0, 1]):
    # multi-timeframe confirmation is ALWAYS on (5m+15m, or 5m+15m+1h); breakeven is compared separately afterwards
    VARIANTS.append({"name": f"{label} | {'ATM' if otm == 0 else '1-OTM'} | MTF {'+'.join(str(x) if x < 60 else '1h' for x in mtf)}",
                     "ov": {"mode": "ANCHOR", "anchors": [ANCH[k] for k in keys], "win_start": "09:30", "win_end": "14:45",
                            "otm_steps": otm, "mtf_tfs": mtf}, "be": False})


def stats(tr, capital=None):
    if not tr:
        return {"trades": 0, "win_rate": 0, "profit_factor": 0, "net": 0, "max_dd": 0, "loss_streak": 0,
                "avg_win_pct": 0, "avg_loss_pct": 0, "expectancy": 0, "tstat": 0, "costs": 0}
    wins = [t for t in tr if t["net"] > 0]
    losses = [t for t in tr if t["net"] <= 0]
    eq = peak = mdd = 0.0
    cur = worst = 0
    for t in tr:
        eq += t["net"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        cur = cur + 1 if t["net"] <= 0 else 0
        worst = max(worst, cur)
    gp, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in losses)
    nets = [t["net"] for t in tr]
    mean = sum(nets) / len(nets)
    sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (len(nets) - 1)) if len(nets) > 1 else 0
    return {"trades": len(tr), "win_rate": len(wins) / len(tr) * 100,
            "avg_win_pct": sum(t["ret_pct"] for t in wins) / len(wins) if wins else 0,
            "avg_loss_pct": sum(t["ret_pct"] for t in losses) / len(losses) if losses else 0,
            "profit_factor": gp / gl if gl else (99.0 if gp else 0), "net": sum(nets),
            "costs": sum(t["costs"] for t in tr), "expectancy": mean, "max_dd": mdd, "loss_streak": worst,
            "tstat": mean / (sd / math.sqrt(len(nets))) if sd else 0}


def verdict(tune, test):
    """Plain-language verdict from FIXED rules (decided before looking at any result)."""
    ok_tune = tune["trades"] >= 30 and tune["profit_factor"] >= 1.2 and tune["net"] > 0
    ok_test = test["trades"] >= 30 and test["profit_factor"] >= 1.2 and test["net"] > 0
    if ok_tune and ok_test and test["tstat"] >= 1.65:
        return ("good", "POSSIBLE EDGE (not proven).",
                "Profitable on both halves and the unseen half has t-stat >= 1.65. Still modelled premiums - "
                "confirm with the recorder's real expiry-day data and weeks of paper trading before real money.")
    if test["trades"] < 30:
        return ("warn", "INCONCLUSIVE - too few trades.",
                f"Only {test['trades']} trades in the test half; nothing can be concluded.")
    if ok_test:
        return ("warn", "WEAK / NOT CONVINCING.",
                "The unseen half is profitable but the evidence is statistically weak"
                + (" and the tuning half was not." if not ok_tune else "."))
    return ("bad", "NO EDGE FOUND.",
            "The frozen rules do not make money on the unseen half after realistic costs. "
            "Do not trade this live. Nothing was re-tuned to make it look better.")


# =====================================================================================
# HTML report
# =====================================================================================
def money(x):
    return f"₹{x:,.0f}"


def cls(x):
    return "pos" if x > 0 else "neg" if x < 0 else ""


def stat_row(label, s):
    pf = "∞" if s["profit_factor"] >= 99 else f"{s['profit_factor']:.2f}"
    return (f"<tr><td>{html.escape(str(label))}</td><td>{s['trades']}</td><td>{s['win_rate']:.0f}%</td>"
            f"<td>{s['avg_win_pct']:+.0f}%</td><td>{s['avg_loss_pct']:+.0f}%</td><td>{pf}</td>"
            f"<td class='{cls(s['net'])}'>{money(s['net'])}</td><td>{money(s['max_dd'])}</td>"
            f"<td>{s['loss_streak']}</td></tr>")


TH = ("<tr><th></th><th>Trades</th><th>Win</th><th>Avg win</th><th>Avg loss</th><th>Profit factor</th>"
      "<th>Net</th><th>Max DD</th><th>Worst streak</th></tr>")


def group_table(trades, keyfn, order=None):
    g_ = defaultdict(list)
    for t in trades:
        g_[keyfn(t)].append(t)
    keys = order or sorted(g_)
    return "<table>" + TH + "".join(stat_row(k, stats(g_[k])) for k in keys if k in g_) + "</table>"


def equity_svg(trades, split_idx):
    pts, eq = [0.0], 0.0
    for t in trades:
        eq += t["net"]
        pts.append(eq)
    W, H, pad = 780, 280, 46
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    X = lambda i: pad + i * (W - 2 * pad) / max(len(pts) - 1, 1)
    Y = lambda v: H - pad - (v - lo) / span * (H - 2 * pad)
    tune_pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(pts[:split_idx + 1]))
    test_pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(pts) if i >= split_idx)
    sx = X(split_idx)
    return (f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Equity curve'>"
            f"<line class='axis' x1='{pad}' x2='{W - pad}' y1='{Y(0):.1f}' y2='{Y(0):.1f}' stroke-dasharray='4 4'/>"
            f"<line class='axis' x1='{sx:.1f}' x2='{sx:.1f}' y1='{pad - 10}' y2='{H - pad}'/>"
            f"<polyline fill='none' class='l-tune' stroke-width='2' points='{tune_pts}'/>"
            f"<polyline fill='none' class='l-test' stroke-width='2.4' points='{test_pts}'/>"
            f"<text class='tx' x='{pad}' y='18'>max {money(hi)}</text><text class='tx' x='{pad}' y='{H - 12}'>min {money(lo)}</text>"
            f"<text class='tx' x='{sx + 6:.1f}' y='{pad - 14}'>TEST half starts (rules frozen) →</text>"
            f"<text class='tx' x='{sx - 6:.1f}' y='{pad - 14}' text-anchor='end'>← tuning half</text></svg>")


def monthly_table(trades, capital, target_pct):
    m = defaultdict(list)
    for t in trades:
        m[t["date"][:7]].append(t)
    rows, hit = "", 0
    for k in sorted(m):
        s = stats(m[k])
        pct = s["net"] / capital * 100
        hit += pct >= target_pct
        rows += (f"<tr><td>{k}</td><td>{s['trades']}</td><td>{s['win_rate']:.0f}%</td>"
                 f"<td class='{cls(s['net'])}'>{money(s['net'])}</td><td class='{cls(pct)}'>{pct:+.1f}%</td></tr>")
    return ("<table><tr><th>Month</th><th>Trades</th><th>Win</th><th>Net P&L</th><th>% of capital</th></tr>"
            + rows + "</table>"), hit, len(m)


def month_bars_svg(trades):
    m = defaultdict(float)
    for t in trades:
        m[t["date"][:7]] += t["net"]
    if not m:
        return ""
    keys = sorted(m)
    W, H, pad = 780, 200, 40
    mx = max(abs(v) for v in m.values()) or 1
    bw = (W - 2 * pad) / len(keys)
    zero = H / 2
    out = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='Monthly net P&L'>"
           f"<line class='axis' x1='{pad}' x2='{W - pad}' y1='{zero}' y2='{zero}'/>"]
    for i, k in enumerate(keys):
        v = m[k]
        h = abs(v) / mx * (H / 2 - 28)
        y = zero - h if v >= 0 else zero
        out.append(f"<rect class='{'b-pos' if v >= 0 else 'b-neg'}' x='{pad + i * bw + 4:.1f}' y='{y:.1f}' "
                   f"width='{bw - 8:.1f}' height='{max(h, 1):.1f}' rx='2'/>"
                   f"<text class='tx' x='{pad + i * bw + bw / 2:.1f}' y='{H - 6}' text-anchor='middle'>{k[2:]}</text>"
                   f"<text class='tx' x='{pad + i * bw + bw / 2:.1f}' y='{(y - 4) if v >= 0 else (y + h + 12):.1f}' "
                   f"text-anchor='middle'>{v / 1000:+.1f}k</text>")
    return "".join(out) + "</svg>"


CSS = """
:root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--line:#d0d7de;--soft:#f6f8fa;--pos:#1a7f37;--neg:#cf222e;--blue:#0969da;--amber:#9a6700;--amberbg:#fff8c5}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#30363d;--soft:#161b22;--pos:#3fb950;--neg:#f85149;--blue:#58a6ff;--amber:#d29922;--amberbg:#3a2f00}}
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:var(--fg);background:var(--bg)}
h1{font-size:22px;margin-bottom:4px}h2{font-size:16px;margin-top:30px}.sub{color:var(--mut);font-size:13px}
.banner{border-radius:10px;padding:14px 16px;margin:16px 0;border:1px solid var(--line);background:var(--soft)}
.banner b{font-size:18px}.banner.good{border-color:var(--pos)}.banner.bad{border-color:var(--neg)}.banner.warn{border-color:var(--amber)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.card{border:1px solid var(--line);border-radius:8px;padding:9px 12px;background:var(--soft)}.l{font-size:12px;color:var(--mut)}.v{font-size:19px;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right}
td:first-child,th:first-child{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}tr.sel td{background:var(--soft);font-weight:600}
.note{background:var(--amberbg);border:1px solid var(--amber);border-radius:8px;padding:10px 12px;font-size:13px;margin:14px 0}
svg{width:100%;height:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.axis{stroke:var(--mut);stroke-width:1}.tx{fill:var(--mut);font-size:11px}.l-tune{stroke:var(--mut)}.l-test{stroke:var(--blue)}
.b-pos{fill:var(--pos)}.b-neg{fill:var(--neg)}.twocol{display:block}
code{background:var(--soft);padding:1px 5px;border-radius:4px}
"""


def cards(s):
    pf = "∞" if s["profit_factor"] >= 99 else f"{s['profit_factor']:.2f}"
    items = [("Net P&L (after costs)", money(s["net"])), ("Trades", s["trades"]), ("Win rate", f"{s['win_rate']:.0f}%"),
             ("Profit factor", pf), ("Max drawdown", money(s["max_dd"])), ("Worst losing streak", s["loss_streak"]),
             ("Avg win / avg loss", f"{s['avg_win_pct']:+.0f}% / {s['avg_loss_pct']:+.0f}%"),
             ("Expectancy / trade", money(s["expectancy"])), ("t-stat of expectancy", f"{s['tstat']:.2f}"),
             ("Costs paid", money(s["costs"]))]
    return "<div class='cards'>" + "".join(f"<div class='card'><div class='l'>{a}</div><div class='v'>{b}</div></div>"
                                          for a, b in items) + "</div>"


def build_report(mkt, label, split, chosen, tune_tr, test_tr, grid, sens, be_cmp, path, days, mtf_cmp=()):
    cap = C["capital"]
    tune, test = stats(tune_tr), stats(test_tr)
    kind, headline, detail = verdict(tune, test)
    all_tr = tune_tr + test_tr
    mt_tbl, mt_hit, mt_n = monthly_table(test_tr, cap, 200)
    mt_tbl_tune, _, _ = monthly_table(tune_tr, cap, 200)
    win_order = [MORNING, MID, AFTERNOON]
    grid_rows = ""
    for gr in sorted(grid, key=lambda r: -r["tune"]["profit_factor"]):
        sel = " class='sel'" if gr["name"] == chosen["name"] else ""
        a, b = gr["tune"], gr["test"]
        grid_rows += (f"<tr{sel}><td>{html.escape(gr['name'])}</td><td>{a['trades']}</td><td>{a['profit_factor']:.2f}</td>"
                      f"<td class='{cls(a['net'])}'>{money(a['net'])}</td><td>{b['trades']}</td>"
                      f"<td>{b['profit_factor']:.2f}</td><td class='{cls(b['net'])}'>{money(b['net'])}</td></tr>")
    sens_rows = "".join(f"<tr><td>{html.escape(n)}</td><td>{s['trades']}</td><td>{s['win_rate']:.0f}%</td>"
                        f"<td>{s['profit_factor']:.2f}</td><td class='{cls(s['net'])}'>{money(s['net'])}</td></tr>"
                        for n, s in sens)
    be_rows = "".join(f"<tr><td>{n}</td><td>{a['trades']}</td><td>{a['win_rate']:.0f}%</td><td>{a['profit_factor']:.2f}</td>"
                      f"<td class='{cls(a['net'])}'>{money(a['net'])}</td><td>{b['trades']}</td>"
                      f"<td>{b['profit_factor']:.2f}</td><td class='{cls(b['net'])}'>{money(b['net'])}</td></tr>"
                      for n, a, b in be_cmp)
    mtf_rows = "".join(f"<tr><td>{n}</td><td>{a['trades']}</td><td>{a['win_rate']:.0f}%</td><td>{a['profit_factor']:.2f}</td>"
                       f"<td class='{cls(a['net'])}'>{money(a['net'])}</td><td>{b['trades']}</td><td>{b['win_rate']:.0f}%</td>"
                       f"<td>{b['profit_factor']:.2f}</td><td class='{cls(b['net'])}'>{money(b['net'])}</td></tr>"
                       for n, a, b in mtf_cmp)
    ov = chosen["ov"]
    snippet = (f'"strategy": {{"mode": "{ov["mode"]}", "anchors": {ov.get("anchors")}, "win_start": "{ov["win_start"]}", "win_end": "{ov["win_end"]}", '
               f'"otm_steps": {ov["otm_steps"]}, "mtf_tfs": {ov["mtf_tfs"]}, ...}},   "breakeven_enabled": {chosen["be"]}')
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Expiry Blast Backtest</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>
<h1>Expiry Blast backtest {html.escape(label)}</h1>
<div class="sub">{days[0]} to {days[-1]} · tune half: before {split} · test half: {split} onward (rules frozen) ·
capital {money(cap)} · risk {C['risk_per_trade_pct']}%/trade · SL {C['stop_loss_pct']:.0f}% · target +{C['target_pct']:.0f}% ·
slippage {BT['slippage_pct']}%/side + full charges · square-off {C['square_off']}</div>

<div class="banner {kind}"><b>{html.escape(headline)}</b><br>{html.escape(detail)}</div>
{("<div class='note'><b>DEMO DATA - NOT YOUR MARKET.</b> " + ("Pure random walk: no edge exists, so a losing result is the CORRECT outcome and shows the engine does not invent profits." if "null" in label else "Synthetic momentum bursts: an edge exists by construction, so a profitable result only shows the engine CAN detect one.") + " Put your real CSVs in <code>data/</code> and run again.</div>") if "DEMO" in label else ""}

<h2>Strategy chosen on the tuning half (then frozen)</h2>
<p><b>{html.escape(chosen['name'])}</b> — picked by highest profit factor on the tuning half (min 30 trades) out of {len(grid)} variants. Every variant also requires the 5m/15m(/1h) charts to trend the same way as the trade.
Expect some selection luck on the tuning half; the test half below is the honest number.<br>
<code>{html.escape(snippet)}</code></p>

<div class="twocol"><div><h2>TEST half (unseen) — the number that matters</h2>{cards(test)}</div>
<div><h2>Tuning half (in-sample)</h2>{cards(tune)}</div></div>

<h2>Equity curve (cumulative net P&L, trade by trade)</h2>{equity_svg(all_tr, len(tune_tr))}
<h2>Monthly net P&L (both halves)</h2>{month_bars_svg(all_tr)}

<h2>Monthly P&L — test half</h2>{mt_tbl}
<div class="note"><b>About a "+200% per month" goal:</b> months in the test half with ≥ +200% of capital: <b>{mt_hit} of {mt_n}</b>.
With 1% risk per trade and at most 4 trades per day, one expiry day can lose at most ~4% of capital and win at most ~16% (4 × 4R) —
so +200% in a month is arithmetically out of reach at this risk setting, and chasing it means raising risk until one bad week ends the account.
The rules were not tuned toward any monthly target.</div>
<h2>Monthly P&L — tuning half</h2>{mt_tbl_tune}

<div class="twocol"><div><h2>Per index — test half</h2>{group_table(test_tr, lambda t: t['index'])}</div>
<div><h2>Per index — tuning half</h2>{group_table(tune_tr, lambda t: t['index'])}</div></div>
<div class="twocol"><div><h2>Per entry time window — test half</h2>{group_table(test_tr, lambda t: t['window'], win_order)}</div>
<div><h2>Per entry time window — tuning half</h2>{group_table(tune_tr, lambda t: t['window'], win_order)}</div></div>
<div class="twocol"><div><h2>Exit reasons — test half</h2>{group_table(test_tr, lambda t: t['reason'])}</div>
<div><h2>Call vs put — test half</h2>{group_table(test_tr, lambda t: t['type'])}</div></div>

<h2>Higher-timeframe confirmation (5m / 15m / 1h EMA9 vs EMA21 must agree): what it does to the chosen entry</h2>
<table><tr><th></th><th>Tune trades</th><th>Win</th><th>PF</th><th>Net</th><th>Test trades</th><th>Win</th><th>PF</th><th>Net</th></tr>{mtf_rows}</table>

<h2>Breakeven stop at +60%: with vs without (same entries as chosen variant)</h2>
<table><tr><th></th><th>Tune trades</th><th>Win</th><th>PF</th><th>Net</th><th>Test trades</th><th>PF</th><th>Net</th></tr>{be_rows}</table>

<h2>Robustness of the frozen rules on the test half</h2>
<p class="sub">Premiums are modelled, so check the result survives worse assumptions. This is NOT used for tuning.</p>
<table><tr><th>Assumption</th><th>Trades</th><th>Win</th><th>PF</th><th>Net</th></tr>{sens_rows}</table>

<h2>All {len(grid)} variants (sorted by tuning profit factor; chosen one highlighted)</h2>
<p class="sub">Test columns are shown for transparency only — selection used the tuning columns.</p>
<table><tr><th>Variant</th><th>Tune trades</th><th>PF</th><th>Net</th><th>Test trades</th><th>PF</th><th>Net</th></tr>{grid_rows}</table>

<div class="note"><b>How to read this:</b> index prices are real (when you use your data); option premiums are Black-Scholes
with India VIX × an expiry-day IV profile ({BT['iv_mult_open']}→{BT['iv_mult_close']}, extra {BT['iv_extra_fade']:.0%} fade after 13:00),
{BT['slippage_pct']}% slippage per side on entry and every exit, brokerage ₹{BT['brokerage_per_order']:.0f}/order, STT {BT['stt_before']}%→{BT['stt_after']}% (from {BT['stt_change']}) on sells,
exchange fees, SEBI, 18% GST and stamp duty. Stop-before-target on any bar touching both. Real expiry-day option prices can
behave very differently (IV spikes, wide spreads, jumpy ticks) — run <code>expiry_recorder</code> on every expiry day and re-test on real premiums.
Not investment advice; most hero-or-zero buyers lose money.</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


# =====================================================================================
def main():
    ap = argparse.ArgumentParser(description="Expiry Blast backtest")
    ap.add_argument("--demo", nargs="?", const="null", choices=["null", "trend"],
                    help="synthetic data. 'null' = pure random walk (must show NO edge); "
                         "'trend' = random walk with momentum bursts (edge by construction, checks the engine can find one)")
    ap.add_argument("--data", default="data", help="folder with the 1-minute CSVs")
    ap.add_argument("--no-download", action="store_true", help="use the CSVs already in data/, do not contact FYERS")
    ap.add_argument("--days", type=int, default=365, help="calendar days of history to download and test (default 365)")
    a = ap.parse_args()
    indices = [k for k, v in C["indices"].items() if v["enabled"]]

    if a.demo:
        if a.demo == "null":
            print("DEMO MODE (null): pure random walk - there is NO edge, a correct engine must not find one.")
            data, vix = null_market(indices, round(a.days * 0.69))
        else:
            print("DEMO MODE (trend): synthetic data WITH momentum bursts - an edge exists by construction.")
            data, vix = bt0.synthetic(indices, round(a.days * 0.69))
        data["VIX"] = vix
        label, out_html = f"(DEMO {a.demo} - synthetic data)", f"expiry_report_DEMO_{a.demo}.html"
    else:
        if not a.no_download:
            try:
                sync_data(connect(), indices, a.data, a.days)
            except SystemExit as e:
                print(f"Could not log in / download: {e}")
                if not glob.glob(os.path.join(a.data, "*.csv")):
                    sys.exit("No data available. Fill in .env (see README) or put CSVs in data/. Or try --demo.")
                print("Using the CSVs already in data/.")
        data = load_dir(a.data)
        cutoff = int(dt.datetime.combine(dt.datetime.now(IST).date() - dt.timedelta(days=a.days), dt.time(0), IST).timestamp())
        data = {k: [c for c in v if c[0] >= cutoff] for k, v in data.items()}
        missing = [k for k in indices if not data.get(k)]
        if missing:
            sys.exit(f"No candle files for {missing} in '{a.data}/'. File names must contain NIFTY / SENSEX / VIX. "
                     f"Or run with --demo.")
        if not data.get("VIX"):
            print(f"WARNING: no VIX file found - using constant VIX {BT['default_vix']}. Results will be less realistic.")
        label, out_html = "", "expiry_report.html"
        firsts = [dt.datetime.fromtimestamp(v[0][0], IST).strftime("%H:%M") for v in (data[k] for k in indices)]
        print("First candle time (IST) per index:", dict(zip(indices, firsts)), "(expected 09:15)")

    mkt = Market(data, indices)
    days = mkt.days
    split = days[0] + (days[-1] - days[0]) / 2
    print(f"{len(days)} trading days {days[0]} .. {days[-1]} | tune < {split} <= test | "
          f"expiry days: { {k: len(v) for k, v in mkt.expiry_days.items()} }")

    grid = []
    for v in VARIANTS:
        tr = mkt.run(v["ov"], v["be"])
        tu = [t for t in tr if t["date"] < split.isoformat()]
        te = [t for t in tr if t["date"] >= split.isoformat()]
        grid.append({**v, "tune": stats(tu), "test": stats(te)})
    eligible = [r for r in grid if r["tune"]["trades"] >= 30 and r["tune"]["net"] > 0] or \
               [r for r in grid if r["tune"]["trades"] >= 30] or grid
    chosen = max(eligible, key=lambda r: r["tune"]["profit_factor"])
    print(f"Chosen on TUNE half only: {chosen['name']}  (tune PF {chosen['tune']['profit_factor']:.2f})")

    tr = mkt.run(chosen["ov"], chosen["be"])
    tune_tr = [t for t in tr if t["date"] < split.isoformat()]
    test_tr = [t for t in tr if t["date"] >= split.isoformat()]

    be_cmp = []
    for name, be in (("Breakeven OFF", False), ("Breakeven ON", True)):
        r = mkt.run(chosen["ov"], be)
        be_cmp.append((name, stats([t for t in r if t["date"] < split.isoformat()]),
                       stats([t for t in r if t["date"] >= split.isoformat()])))

    mtf_cmp = []
    for name, tfs in (("No higher-timeframe filter (1-min only)", []), ("5m + 15m", [5, 15]), ("5m + 15m + 1h", [5, 15, 60])):
        r = mkt.run({**chosen["ov"], "mtf_tfs": tfs}, chosen["be"])
        mtf_cmp.append((name, stats([t for t in r if t["date"] < split.isoformat()]),
                        stats([t for t in r if t["date"] >= split.isoformat()])))

    sens = []
    for name, ch in (("Baseline (as reported)", {}), ("Slippage 3% per side", {"slippage_pct": 3.0}),
                     ("Slippage 4% per side", {"slippage_pct": 4.0}),
                     ("Expiry IV 15% lower", {"iv_mult_open": BT["iv_mult_open"] * .85, "iv_mult_close": BT["iv_mult_close"] * .85}),
                     ("Expiry IV 15% higher", {"iv_mult_open": BT["iv_mult_open"] * 1.15, "iv_mult_close": BT["iv_mult_close"] * 1.15}),
                     ("No expiry IV adj. (IV = VIX)", {"iv_mult_open": 1.0, "iv_mult_close": 1.0, "iv_extra_fade": 0.0})):
        r = mkt.run(chosen["ov"], chosen["be"], bt={**BT, **ch}, start=split)
        sens.append((name, stats(r)))

    build_report(mkt, label, split, chosen, tune_tr, test_tr, grid, sens, be_cmp, out_html, days, mtf_cmp)
    all_tr = tune_tr + test_tr
    if all_tr:
        with open("expiry_trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_tr[0].keys()))
            w.writeheader()
            w.writerows(all_tr)
    s_tu, s_te = stats(tune_tr), stats(test_tr)
    if not a.demo:
        import json
        with open("expiry_best.json", "w") as f:
            json.dump({"generated": dt.date.today().isoformat(), "name": chosen["name"], "strategy": chosen["ov"],
                       "breakeven": chosen["be"], "verdict": verdict(s_tu, s_te)[0],
                       "headline": verdict(s_tu, s_te)[1]}, f, indent=2)
        print("Saved expiry_best.json (the bot can use it: set use_backtest_result to True in expiry_blast.py)")
    for nm, s in (("TUNE", s_tu), ("TEST", s_te)):
        print(f"{nm}: trades {s['trades']} | win {s['win_rate']:.0f}% | PF {s['profit_factor']:.2f} | "
              f"net Rs {s['net']:,.0f} | maxDD Rs {s['max_dd']:,.0f} | worst streak {s['loss_streak']}")
    print("VERDICT:", verdict(s_tu, s_te)[1])
    print(f"Saved {out_html} and expiry_trades.csv")


if __name__ == "__main__":
    main()
