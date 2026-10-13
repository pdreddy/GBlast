"""
GAMMA BLAST - BACKTEST
======================
Replays the bot's exact rules (momentum filter, gamma strike pick, 40% SL, +200% target,
trail after +50% at 25% below peak, daily limits, 15:15 square-off) over past trading days.

DATA
  * Index moves: REAL 1-minute candles of NIFTY / BANKNIFTY / SENSEX from FYERS.
  * Option premiums: MODELLED with Black-Scholes using real 1-minute India VIX as volatility.
    FYERS does not provide price history for expired option contracts, so real premiums
    are not available. The model captures delta, gamma and time decay, but NOT expiry-day
    IV spikes/crush, bid-ask spreads, or volatility skew. Treat results as a filter
    ("is there an edge at all?"), not a promise. Paper trading measures the real thing.

RUN
  python backtest.py                 # last 180 days, all enabled indices
  python backtest.py --days 90
  python backtest.py --index NIFTY   # one index only
  python backtest.py --demo          # synthetic data, no login needed (checks the program runs)

OUTPUT
  backtest_report.html   (open in browser: equity curve + statistics)
  backtest_trades.csv    (every simulated trade)
  data/                  (downloaded candles are cached here - re-runs are fast)
"""
import argparse
import csv
import datetime as dt
import math
import os
import random
import sys
from collections import defaultdict

import gamma_blast as g

IST = g.IST
C = g.CONFIG

BT = {
    "slippage_pct": 1.0,          # % of premium lost on EACH side (entry and exit)
    "vix_symbol": "NSE:INDIAVIX-INDEX",
    "default_vix": 14.0,
    # option IV relative to India VIX (Bank Nifty is usually more volatile)
    "iv_mult": {"NIFTY": 1.0, "BANKNIFTY": 1.2, "SENSEX": 1.0},
    "strike_step": {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100},
    "lot_fallback": {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20},  # replaced by live lot sizes if available
    "exchange_txn_pct": {"NIFTY": 0.03503, "BANKNIFTY": 0.03503, "SENSEX": 0.0325},
}


# =====================================================================================
# Expiry calendar (rules by date; holidays handled using the actual trading days in the data)
# Mon=0 Tue=1 Wed=2 Thu=3 Fri=4
# =====================================================================================
def expiry_rule(index, d):
    if index == "NIFTY":
        return ("weekly", 1) if d >= dt.date(2025, 9, 1) else ("weekly", 3)
    if index == "BANKNIFTY":
        return ("monthly", 1) if d >= dt.date(2025, 9, 1) else ("monthly", 3)
    if index == "SENSEX":
        if d >= dt.date(2025, 9, 1):
            return ("weekly", 3)
        return ("weekly", 1) if d >= dt.date(2025, 1, 1) else ("weekly", 4)
    raise ValueError(index)


class ExpiryCalendar:
    def __init__(self, trading_days):
        self.days = sorted(trading_days)
        self.dayset = set(trading_days)
        self.last = self.days[-1] if self.days else None

    def _adjust(self, d):
        """Exchange holiday on expiry -> previous trading day."""
        if self.last is None or d > self.last:
            return d
        for k in range(6):
            c = d - dt.timedelta(days=k)
            if c in self.dayset:
                return c
        return d

    def nearest(self, index, d):
        kind, wd = expiry_rule(index, d)
        if kind == "weekly":
            for k in range(0, 21):
                c = d + dt.timedelta(days=k)
                if c.weekday() == wd:
                    e = self._adjust(c)
                    if e >= d:
                        return e
        else:
            y, m = d.year, d.month
            for _ in range(3):
                nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
                c = nxt - dt.timedelta(days=1)
                while c.weekday() != wd:
                    c -= dt.timedelta(days=1)
                e = self._adjust(c)
                if e >= d:
                    return e
                y, m = nxt.year, nxt.month
        return d


# =====================================================================================
# Data
# =====================================================================================
def fetch_1min(broker, symbol, start, end):
    os.makedirs("data", exist_ok=True)
    fn = os.path.join("data", f"{symbol.replace(':', '_')}_{start}_{end}.csv")
    if os.path.exists(fn):
        with open(fn) as f:
            return [[int(r[0])] + [float(x) for x in r[1:6]] for r in csv.reader(f)]
    out, cur = [], start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=60), end)
        r = broker._call(broker.f.history, {
            "symbol": symbol, "resolution": "1", "date_format": "1",
            "range_from": cur.strftime("%Y-%m-%d"), "range_to": stop.strftime("%Y-%m-%d"),
            "cont_flag": "1"})
        if r.get("s") != "ok" and not r.get("candles"):
            print(f"   warning: {symbol} {cur}..{stop}: {r.get('message', r)}")
        out += r.get("candles") or []
        cur = stop + dt.timedelta(days=1)
    seen, clean = set(), []
    for c in sorted(out, key=lambda x: x[0]):
        if c[0] not in seen:
            seen.add(c[0])
            clean.append([int(c[0])] + [float(x) for x in c[1:5]] + [float(c[5]) if len(c) > 5 else 0.0])
    with open(fn, "w", newline="") as f:
        csv.writer(f).writerows(clean)
    return clean


def by_day(candles):
    days = defaultdict(list)
    for c in candles:
        t = dt.datetime.fromtimestamp(c[0], IST)
        if dt.time(9, 15) <= t.time() < dt.time(15, 30):
            days[t.date()].append(c)
    return days


def synthetic(indices, n_days, seed=7):
    """Random-walk market with trend bursts - only to check the program runs end to end."""
    rnd = random.Random(seed)
    base = {"NIFTY": 25000, "BANKNIFTY": 56000, "SENSEX": 82000}
    data, vix = {k: [] for k in indices}, []
    d = dt.date.today() - dt.timedelta(days=int(n_days * 1.45))
    made = 0
    while made < n_days:
        d += dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        made += 1
        start = dt.datetime.combine(d, dt.time(9, 15), IST)
        v = 12 + rnd.random() * 6
        drift = {k: 0.0 for k in indices}
        for i in range(375):
            ts = int((start + dt.timedelta(minutes=i)).timestamp())
            vix.append([ts, v, v, v, v, 0])
            for k in indices:
                if rnd.random() < 0.01:
                    drift[k] = rnd.choice([-1, 1]) * rnd.uniform(0.0004, 0.0012)
                drift[k] *= 0.97
                p = base[k]
                r = drift[k] + rnd.gauss(0, 0.00045)
                q = p * (1 + r)
                hi, lo = max(p, q) * (1 + abs(rnd.gauss(0, 0.0002))), min(p, q) * (1 - abs(rnd.gauss(0, 0.0002)))
                data[k].append([ts, p, hi, lo, q, 0])
                base[k] = q
    return data, vix


# =====================================================================================
# Simulator
# =====================================================================================
YEAR_MIN = 365 * 24 * 60


def premium(spot, K, minutes_left, iv, cp):
    T = max(minutes_left, 1.0) / YEAR_MIN
    return max(g.bs_price(spot, K, T, C["risk_free_rate"], iv, cp), 0.05)


def costs(index, entry, exit_, qty):
    buy, sell = entry * qty, exit_ * qty
    brokerage = 40.0
    stt = 0.001 * sell
    exch = BT["exchange_txn_pct"][index] / 100 * (buy + sell)
    sebi = 10 / 1e7 * (buy + sell)
    stamp = 0.00003 * buy
    gst = 0.18 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


class Sim:
    def __init__(self, data, vix, lots, indices):
        self.data = {k: by_day(v) for k, v in data.items()}
        self.vix = {c[0]: c[4] for c in vix}
        self.lots = lots
        self.indices = indices
        all_days = set()
        for v in self.data.values():
            all_days |= set(v)
        self.days = sorted(all_days)
        self.cal = ExpiryCalendar(self.days)
        self.trades = []
        self.sizer = type("S", (), {"cfg": C})()

    def iv_at(self, index, ts, last):
        v = self.vix.get(ts)
        if v:
            last[0] = v
        return last[0] / 100 * BT["iv_mult"][index]

    def run(self):
        last_vix = [BT["default_vix"]]
        for day in self.days:
            bars = {k: self.data[k].get(day, []) for k in self.indices}
            idx = {k: {c[0]: i for i, c in enumerate(b)} for k, b in bars.items()}
            stamps = sorted({c[0] for b in bars.values() for c in b})
            open_pos, trades_today, last_loss, realised = {}, defaultdict(int), {}, 0.0
            pending = {}  # signal on bar i -> enter at open of bar i+1

            for ts in stamps:
                t = dt.datetime.fromtimestamp(ts, IST)
                for k in self.indices:
                    if ts not in idx[k]:
                        continue
                    i = idx[k][ts]
                    bar = bars[k][i]
                    iv = self.iv_at(k, ts, last_vix)
                    exp_dt = dt.datetime.combine(self.cal.nearest(k, day), dt.time(15, 30), IST)

                    # ---- 1. fill a pending entry at this bar's open ----
                    if k in pending:
                        sig = pending.pop(k)
                        mins = (exp_dt - t).total_seconds() / 60
                        px = premium(bar[1], sig["K"], mins, iv, sig["cp"])
                        prev = bars[k][max(0, i - 1 - C["option_confirm_bars"]):i - 1]
                        confirm = True
                        if C["option_confirm_bars"] > 0 and prev:
                            ext = max(c[2] for c in prev) if sig["cp"] == "CE" else min(c[3] for c in prev)
                            pm = (exp_dt - dt.datetime.fromtimestamp(prev[-1][0], IST)).total_seconds() / 60
                            confirm = px > premium(ext, sig["K"], pm, iv, sig["cp"])
                        lots = g.GammaBlast.size(self.sizer, k, px, self.lots[k])
                        if confirm and lots >= 1 and len(open_pos) < C["max_open_positions"]:
                            fill = px * (1 + BT["slippage_pct"] / 100)
                            open_pos[k] = {
                                "cp": sig["cp"], "K": sig["K"], "entry": fill, "qty": lots * self.lots[k],
                                "sl": fill * (1 - C["stop_loss_pct"] / 100),
                                "tgt": fill * (1 + C["target_pct"] / 100), "peak": fill, "trail": False,
                                "t_in": t, "exp": exp_dt, "score": sig["score"], "spot_in": bar[1],
                                "expiry_day": exp_dt.date() == day}
                            trades_today[k] += 1

                    # ---- 2. manage an open position on this bar ----
                    p = open_pos.get(k)
                    if p:
                        mins = (p["exp"] - t).total_seconds() / 60 - 0.5
                        o_s, h_s, l_s = bar[1], bar[2], bar[3]
                        fav, adv = (h_s, l_s) if p["cp"] == "CE" else (l_s, h_s)
                        pr_open = premium(o_s, p["K"], mins, iv, p["cp"])
                        pr_hi = premium(fav, p["K"], mins, iv, p["cp"])
                        pr_lo = premium(adv, p["K"], mins, iv, p["cp"])
                        exit_px = reason = None
                        if t.time() >= g.hhmm(C["square_off"]):
                            exit_px, reason = pr_open, "SQUARE_OFF"
                        elif pr_lo <= p["sl"]:                      # conservative: stop checked first
                            exit_px = min(p["sl"], pr_open)
                            reason = "TRAIL" if p["trail"] else "SL"
                        elif pr_hi >= p["tgt"]:
                            exit_px, reason = max(p["tgt"], pr_open), "TARGET"
                        elif C["no_progress_exit_min"] > 0 and not p["trail"] and \
                                (t - p["t_in"]).total_seconds() / 60 >= C["no_progress_exit_min"]:
                            pr_cl = premium(bar[4], p["K"], mins, iv, p["cp"])
                            if (pr_cl / p["entry"] - 1) * 100 < C["no_progress_min_gain_pct"]:
                                exit_px, reason = pr_cl, "NO_PROGRESS"
                        if exit_px is None:
                            p["peak"] = max(p["peak"], pr_hi)
                            if (p["peak"] / p["entry"] - 1) * 100 >= C["trail_trigger_pct"]:
                                new_sl = p["peak"] * (1 - C["trail_gap_pct"] / 100)
                                if new_sl > p["sl"]:
                                    p["sl"], p["trail"] = new_sl, True
                        else:
                            fill = exit_px * (1 - BT["slippage_pct"] / 100)
                            gross = (fill - p["entry"]) * p["qty"]
                            cst = costs(k, p["entry"], fill, p["qty"])
                            net = gross - cst
                            realised += net
                            if net < 0:
                                last_loss[k] = t
                            self.trades.append({
                                "date": day.isoformat(), "index": k, "type": p["cp"], "strike": p["K"],
                                "expiry": p["exp"].date().isoformat(), "expiry_day": p["expiry_day"],
                                "entry_time": p["t_in"].strftime("%H:%M"), "exit_time": t.strftime("%H:%M"),
                                "spot_in": round(p["spot_in"], 2), "qty": p["qty"],
                                "entry": round(p["entry"], 2), "exit": round(fill, 2),
                                "ret_pct": round((fill / p["entry"] - 1) * 100, 1),
                                "gross": round(gross, 0), "costs": round(cst, 0), "net": round(net, 0),
                                "reason": reason, "blast_score": p["score"]})
                            del open_pos[k]

                    # ---- 3. look for a new signal on this (closed) bar ----
                    decide = t + dt.timedelta(minutes=1)
                    end = C["expiry_day_entry_end"] if exp_dt.date() == day else C["entry_end"]
                    if k in open_pos or k in pending or trades_today[k] >= C["max_trades_per_index"]:
                        continue
                    if not (g.hhmm(C["entry_start"]) <= decide.time() <= g.hhmm(end)):
                        continue
                    if len(open_pos) >= C["max_open_positions"]:
                        continue
                    if realised <= -C["capital"] * C["max_daily_loss_pct"] / 100:
                        continue
                    if k in last_loss and decide - last_loss[k] < dt.timedelta(minutes=C["loss_cooldown_min"]):
                        continue
                    side, _ = g.GammaBlast.momentum_signal(self.sizer, bars[k][:i + 1])
                    if not side:
                        continue
                    pick = self.pick(k, side, bar[4], exp_dt, decide, iv)
                    if pick:
                        pending[k] = {"cp": side, **pick}

    def pick(self, k, cp, spot, exp_dt, now, iv):
        step = BT["strike_step"][k]
        atm = round(spot / step) * step
        n_otm = 2 if (exp_dt - now) < dt.timedelta(days=1) else 1
        sign = 1 if cp == "CE" else -1
        mins = max((exp_dt - now).total_seconds() / 60, 10)
        T = mins / YEAR_MIN
        dS = spot * C["blast_move_pct"] / 100
        hold_T = C["expected_hold_min"] / YEAR_MIN
        best = None
        for n in range(n_otm + 1):
            K = atm + sign * n * step
            px = premium(spot, K, mins, iv, cp)
            if px < C["indices"][k]["min_premium"]:
                continue
            delta, gamma, theta = g.greeks(spot, K, T, C["risk_free_rate"], iv, cp)
            if abs(delta) < C["min_abs_delta"]:
                continue
            gain = (abs(delta) * dS + 0.5 * gamma * dS * dS + theta * hold_T) / px * 100
            if best is None or gain > best["score"]:
                best = {"K": K, "score": round(gain, 1)}
        if best and best["score"] >= C["indices"][k]["min_blast_gain_pct"]:
            return best
        return None


# =====================================================================================
# Report
# =====================================================================================
def stats(tr):
    if not tr:
        return {}
    wins = [t for t in tr if t["net"] > 0]
    losses = [t for t in tr if t["net"] <= 0]
    eq, peak, mdd, cur, worst = 0.0, 0.0, 0.0, 0, 0
    for t in tr:
        eq += t["net"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        cur = cur + 1 if t["net"] <= 0 else 0
        worst = max(worst, cur)
    gp, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in losses)
    return {
        "trades": len(tr), "win_rate": len(wins) / len(tr) * 100,
        "avg_win_pct": sum(t["ret_pct"] for t in wins) / len(wins) if wins else 0,
        "avg_loss_pct": sum(t["ret_pct"] for t in losses) / len(losses) if losses else 0,
        "profit_factor": gp / gl if gl else float("inf"),
        "net": sum(t["net"] for t in tr), "costs": sum(t["costs"] for t in tr),
        "expectancy": sum(t["net"] for t in tr) / len(tr), "max_dd": mdd, "loss_streak": worst,
    }


def fmt_stats(s):
    if not s:
        return "no trades"
    return (f"trades {s['trades']} | win rate {s['win_rate']:.0f}% | avg win {s['avg_win_pct']:+.0f}% | "
            f"avg loss {s['avg_loss_pct']:+.0f}% | profit factor {s['profit_factor']:.2f} | "
            f"net Rs {s['net']:,.0f} | max drawdown Rs {s['max_dd']:,.0f} | worst losing streak {s['loss_streak']}")


def report(trades, days, label):
    with open("backtest_trades.csv", "w", newline="") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)

    s = stats(trades)
    groups = {
        "By index": defaultdict(list), "By exit reason": defaultdict(list),
        "Expiry day vs other days": defaultdict(list), "By month": defaultdict(list),
    }
    for t in trades:
        groups["By index"][t["index"]].append(t)
        groups["By exit reason"][t["reason"]].append(t)
        groups["Expiry day vs other days"]["Expiry day" if t["expiry_day"] else "Other days"].append(t)
        groups["By month"][t["date"][:7]].append(t)

    print("\n" + "=" * 90)
    print(f"BACKTEST {label}: {days[0]} to {days[-1]}  ({len(days)} trading days)")
    print(f"Capital Rs {C['capital']:,} | risk {C['risk_per_trade_pct']}%/trade | SL {C['stop_loss_pct']}% | "
          f"target +{C['target_pct']}% | trail +{C['trail_trigger_pct']}%/{C['trail_gap_pct']}% | "
          f"slippage {BT['slippage_pct']}%/side")
    print("=" * 90)
    print("OVERALL:", fmt_stats(s))
    for gname, gd in groups.items():
        print(f"\n{gname}:")
        for key in sorted(gd):
            print(f"  {key:14s} {fmt_stats(stats(gd[key]))}")

    # ---- HTML ----
    eq, pts = 0.0, [0.0]
    for t in trades:
        eq += t["net"]
        pts.append(eq)
    W, H, pad = 760, 260, 40
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    xy = " ".join(f"{pad + i * (W - 2 * pad) / max(len(pts) - 1, 1):.1f},"
                  f"{H - pad - (v - lo) / span * (H - 2 * pad):.1f}" for i, v in enumerate(pts))
    zero_y = H - pad - (0 - lo) / span * (H - 2 * pad)
    color = "#1a7f37" if pts[-1] >= 0 else "#cf222e"

    def table(gd):
        rows = ""
        for key in sorted(gd):
            x = stats(gd[key])
            rows += (f"<tr><td>{key}</td><td>{x['trades']}</td><td>{x['win_rate']:.0f}%</td>"
                     f"<td>{x['avg_win_pct']:+.0f}%</td><td>{x['avg_loss_pct']:+.0f}%</td>"
                     f"<td>{x['profit_factor']:.2f}</td><td class='{'pos' if x['net'] >= 0 else 'neg'}'>"
                     f"₹{x['net']:,.0f}</td></tr>")
        return ("<table><tr><th></th><th>Trades</th><th>Win</th><th>Avg win</th><th>Avg loss</th>"
                "<th>Profit factor</th><th>Net</th></tr>" + rows + "</table>")

    cards = ""
    if s:
        for lab, val in [("Net P&L", f"₹{s['net']:,.0f}"), ("Trades", s["trades"]),
                         ("Win rate", f"{s['win_rate']:.0f}%"), ("Profit factor", f"{s['profit_factor']:.2f}"),
                         ("Max drawdown", f"₹{s['max_dd']:,.0f}"), ("Worst losing streak", s["loss_streak"]),
                         ("Avg win / loss", f"{s['avg_win_pct']:+.0f}% / {s['avg_loss_pct']:+.0f}%"),
                         ("Costs paid", f"₹{s['costs']:,.0f}")]:
            cards += f"<div class='card'><div class='l'>{lab}</div><div class='v'>{val}</div></div>"
    sections = "".join(f"<h2>{n}</h2>{table(gd)}" for n, gd in groups.items())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Gamma Blast Backtest</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1f2328}}
h1{{font-size:22px;margin-bottom:4px}} h2{{font-size:16px;margin-top:28px}} .sub{{color:#656d76;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:18px 0}}
.card{{border:1px solid #d0d7de;border-radius:8px;padding:10px 12px}} .l{{font-size:12px;color:#656d76}} .v{{font-size:20px;font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{padding:6px 8px;border-bottom:1px solid #eaeef2;text-align:right}}
td:first-child,th:first-child{{text-align:left}} .pos{{color:#1a7f37}} .neg{{color:#cf222e}}
.warn{{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:10px 12px;font-size:13px;margin-top:16px}}
svg{{width:100%;height:auto;border:1px solid #d0d7de;border-radius:8px}}</style></head><body>
<h1>Gamma Blast backtest {label}</h1>
<div class="sub">{days[0]} to {days[-1]} · {len(days)} trading days · capital ₹{C['capital']:,} · risk {C['risk_per_trade_pct']}%/trade ·
SL {C['stop_loss_pct']}% · target +{C['target_pct']}% · trail from +{C['trail_trigger_pct']}% at {C['trail_gap_pct']}% below peak · slippage {BT['slippage_pct']}%/side + charges</div>
<div class="cards">{cards or '<div class="card">No trades were taken.</div>'}</div>
<h2>Equity curve (cumulative net P&L, trade by trade)</h2>
<svg viewBox="0 0 {W} {H}"><line x1="{pad}" x2="{W - pad}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" stroke="#8c959f" stroke-dasharray="4 4"/>
<polyline fill="none" stroke="{color}" stroke-width="2" points="{xy}"/>
<text x="{pad}" y="20" font-size="12" fill="#656d76">max ₹{hi:,.0f}</text><text x="{pad}" y="{H - 10}" font-size="12" fill="#656d76">min ₹{lo:,.0f}</text></svg>
{sections}
<div class="warn"><b>How to read this:</b> index prices are real; option premiums are modelled from India VIX because expired option prices are not available from FYERS.
Real expiry-day premiums can move very differently (IV spikes and crush, wide spreads). Use this to judge whether the rules have an edge at all,
then confirm with several weeks of paper trading before risking money.</div>
</body></html>"""
    with open("backtest_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\nSaved backtest_report.html and backtest_trades.csv")


# =====================================================================================
def main():
    ap = argparse.ArgumentParser(description="Gamma Blast backtest")
    ap.add_argument("--days", type=int, default=180, help="calendar days back (default 180)")
    ap.add_argument("--index", choices=list(C["indices"]), help="test one index only")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no FYERS login needed")
    a = ap.parse_args()

    indices = [a.index] if a.index else [k for k, v in C["indices"].items() if v["enabled"]]
    lots = dict(BT["lot_fallback"])

    if a.demo:
        print("DEMO MODE: synthetic random data - results mean nothing, this only checks the program runs.")
        data, vix = synthetic(indices, min(a.days, 60))
        label = "(DEMO - synthetic data)"
    else:
        try:
            broker = g.Broker(C)
        except FileNotFoundError:
            sys.exit("access_token.txt not found - run fyers_login.py first (or use --demo).")
        try:
            m = g.SymbolMaster()
            m.load({k: C["indices"][k] for k in indices})
            for k in indices:
                lots[k] = m.options[k][0].lot
        except Exception as e:
            print(f"   could not load live lot sizes ({e}); using fallback {lots}")
        end = dt.datetime.now(IST).date()
        start = end - dt.timedelta(days=a.days)
        data = {}
        for k in indices:
            print(f"Downloading {k} 1-minute candles {start} .. {end}")
            data[k] = fetch_1min(broker, C["indices"][k]["spot"], start, end)
            print(f"   {len(data[k]):,} candles")
            if not data[k]:
                sys.exit(f"No data for {k}. Is your login token valid? Run fyers_login.py again.")
        print(f"Downloading India VIX {start} .. {end}")
        vix = fetch_1min(broker, BT["vix_symbol"], start, end)
        if not vix:
            print(f"   no VIX data - using constant {BT['default_vix']}")
        label = ""

    print(f"Lot sizes used: { {k: lots[k] for k in indices} }")
    sim = Sim(data, vix, lots, indices)
    print(f"Simulating {len(sim.days)} trading days ...")
    sim.run()
    report(sim.trades, sim.days, label)


if __name__ == "__main__":
    main()
