"""
Offline checks for Expiry Blast. Needs NO FYERS account, NO internet, NO market hours.
Proves: -25% stop, +100% target, 15:15 square-off, 1% risk sizing, breakeven, 14:45 cut-off, trade limits,
broker-side SL-L placement + cancel-then-sell exits (live path with a fake broker), stop-before-target in the
backtest, no look-ahead in the signals, and the recorder's file output.

Run:  python test_expiry_offline.py
"""
import csv
import datetime as dt
import os
import shutil
import sys
import tempfile
import types

fake = types.ModuleType("fyers_apiv3")
fake.fyersModel = types.SimpleNamespace()
sys.modules["fyers_apiv3"] = fake

import expiry_blast as eb
import expiry_backtest as xb
import expiry_recorder as rec
import gamma_blast as g

TMP = tempfile.mkdtemp()
eb.TRADE_LOG, eb.STATE_FILE = os.path.join(TMP, "t.csv"), os.path.join(TMP, "s.json")
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


CFG = eb.CONFIG
check("paper mode is ON by default", CFG["paper_trading"] is True)
check("SL 25% / target 100% = 1:4", CFG["stop_loss_pct"] == 25 and CFG["target_pct"] == 100)
check("entry cut-off 14:45, square-off 15:15", CFG["entry_end"] == "14:45" and CFG["square_off"] == "15:15")

# ---------------------------------------------------------------------------
print("\n[1] 1% risk sizing")
cap, risk = 200000, 2000
for prem, lot in [(40, 65), (80, 65), (150, 65), (60, 20), (150, 20), (100, 20)]:
    lots = eb.size_lots(cap, 1.0, prem, 25.0, lot)
    loss = lots * lot * prem * 0.25
    one_lot = prem * 0.25 * lot
    ok = loss <= risk + 1e-6 and (lots == 0 or (lots + 1) * one_lot > risk or lots == 10)
    check(f"premium {prem}, lot {lot}", ok, f"-> {lots} lot(s), loss at -25% = Rs {loss:,.0f} (budget {risk:,})")
check("skip when 1 lot risks more than 1%", eb.size_lots(cap, 1.0, 150, 25.0, 65) == 0,
      f"1 lot @150 risks Rs {150*.25*65:,.0f} > Rs {risk:,}")
check("max_capital cap honoured", eb.size_lots(10_000_000, 1.0, 100, 25.0, 65, 100, 5.0) * 100 * 65 <= 500000)

# ---------------------------------------------------------------------------
print("\n[2] Paper bot exits")


class FakeBroker:
    def __init__(self):
        self.px = {}

    def ltp(self, symbols):
        return {s: self.px[s] for s in symbols if s in self.px}


def new_bot(be=False, **over):
    cfg = {**CFG, "breakeven_enabled": be, "capital": 200000, "paper_slippage_pct": 0.0,
           "trail_enabled": False, "trade_days": "expiry", **over}
    bot = eb.ExpiryBlast.__new__(eb.ExpiryBlast)
    bot.cfg, bot.paper, bot.broker = cfg, True, FakeBroker()
    bot.S = eb.compile_strategy(cfg["strategy"])
    bot.positions, bot.trades_today, bot.last_loss_time = {}, {"NIFTY": 0, "SENSEX": 0}, {}
    bot.realised, bot.last_scan_min = 0.0, {}
    return bot


SYM = "NSE:NIFTYTEST25000CE"
ROW = g.OptionRow(SYM, 25000, "CE", g.now_ist() + dt.timedelta(hours=3), 65, 0.05)


def run_path(path, entry=60.0, be=False, flatten_at_end=None):
    bot = new_bot(be)
    bot.broker.px[SYM] = entry
    bot.enter("NIFTY", "CE", ROW, entry, {})
    pos = bot.positions["NIFTY"]
    qty = pos.qty
    sl0, tgt0 = pos.sl, pos.target
    for price in path:
        bot.broker.px[SYM] = price
        bot.manage()
        if "NIFTY" not in bot.positions:
            break
    if flatten_at_end and "NIFTY" in bot.positions:
        bot.flatten(flatten_at_end)
    last = list(csv.reader(open(eb.TRADE_LOG)))[-1]
    return float(last[8]), last[11], qty, sl0, tgt0, bot


exit_px, reason, qty, sl0, tgt0, _ = run_path([55, 50, 46, 44.9])
check("stop-loss price is entry -25%", abs(sl0 - 45.0) < 0.06 and abs(tgt0 - 120.0) < 0.06, f"SL {sl0}, TGT {tgt0}")
check("stop hit at -25%", reason == "SL" and exit_px <= 45.0, f"exit {exit_px} reason {reason}")
exit_px, reason, qty, *_ = run_path([70, 90, 110, 121])
check("target hit at +100%", reason == "TARGET" and exit_px >= 120.0, f"exit {exit_px} reason {reason}")
exit_px, reason, qty, *_ = run_path([62, 58, 64], flatten_at_end="SQUARE_OFF")
check("square-off closes open position", reason == "SQUARE_OFF" and exit_px == 64, f"exit {exit_px} reason {reason}")

exit_px, reason, *_ = run_path([70, 97, 80, 60, 59.5], be=False)
check("without breakeven the -25% stop stays", reason != "BE", f"reason {reason}")
exit_px, reason, *_ = run_path([70, 97, 80, 70, 59.9], be=True)
check("breakeven: after +60% (96) stop moves to entry", reason == "BE" and abs(exit_px - 59.9) < 1,
      f"exit {exit_px} reason {reason}")
bot = new_bot(True)
bot.broker.px[SYM] = 60
bot.enter("NIFTY", "CE", ROW, 60.0, {})
bot.broker.px[SYM] = 95
bot.manage()
check("breakeven NOT armed below +60%", bot.positions["NIFTY"].sl < 60, f"SL {bot.positions['NIFTY'].sl}")
bot.broker.px[SYM] = 96.5
bot.manage()
check("breakeven armed at +60%", abs(bot.positions["NIFTY"].sl - 60.0) < 0.06, f"SL {bot.positions['NIFTY'].sl}")

# ---------------------------------------------------------------------------
print("\n[3] Entry guards")
bot = new_bot()
at = lambda h, m: g.now_ist().replace(hour=h, minute=m, second=5)
bot.S = eb.compile_strategy(dict(CFG["strategy"], win_start="09:30", win_end="14:45", mtf_tfs=[]))
check("entry allowed 14:45", bot.can_enter("NIFTY", at(14, 45)))
check("NO entry at 14:46", not bot.can_enter("NIFTY", at(14, 46)))
check("NO entry before window", not bot.can_enter("NIFTY", at(9, 20)))
bot.trades_today["NIFTY"] = 2
check("max 2 trades per index", not bot.can_enter("NIFTY", at(13, 30)))
check("other index unaffected", bot.can_enter("SENSEX", at(13, 30)))
bot.trades_today["NIFTY"] = 0
bot.broker.px[SYM] = 60
bot.enter("NIFTY", "CE", ROW, 60.0, {})
check("1 open position per index", not bot.can_enter("NIFTY", at(13, 30)))


class FakeMaster:
    def __init__(self, exp):
        self.exp = exp

    def nearest_expiry(self, name, now):
        return self.exp


bot = new_bot()
bot.master = FakeMaster(g.now_ist() + dt.timedelta(days=3))
bot.broker.candles_today = lambda s: (_ for _ in ()).throw(AssertionError("should not fetch candles"))
try:
    bot.scan("NIFTY", at(13, 30))
    check("scan ignores non-expiry days", True)
except AssertionError:
    check("scan ignores non-expiry days", False)

# ---------------------------------------------------------------------------
print("\n[4] LIVE path with a fake broker: broker-side SL-L and cancel-then-sell")


class LiveBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.calls, self.n = [], 0
        self.sl_status = 6

    def place(self, symbol, qty, side, otype=2, limit=0.0, stop=0.0):
        self.n += 1
        self.calls.append(("place", otype, side, qty, limit, stop))
        return f"O{self.n}"

    def wait_fill(self, oid, timeout=15):
        return self.px[SYM]

    def status(self, oid):
        return (self.sl_status, 0.0)

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        return True

    def modify_sl(self, oid, qty, stop, limit):
        self.calls.append(("modify", oid, stop, limit))
        return True


g.time.sleep = lambda s: None
bot = new_bot(True)
bot.paper = False
bot.broker = LiveBroker()
bot.broker.px[SYM] = 60
bot.enter("NIFTY", "CE", ROW, 60.0, {})
c = bot.broker.calls
check("entry = MARKET buy then SL-L sell placed immediately",
      c[0][:3] == ("place", 2, 1) and c[1][:3] == ("place", 4, -1), f"{c}")
check("broker SL trigger = entry -25%", abs(c[1][5] - 45.0) < 0.06, f"trigger {c[1][5]}, limit {c[1][4]}")
check("SL qty = entry qty", c[1][3] == c[0][3])
bot.broker.px[SYM] = 97
bot.manage()
check("breakeven modifies the BROKER stop to entry", any(x[0] == "modify" and abs(x[2] - 60.0) < 0.06 for x in bot.broker.calls))
bot.broker.px[SYM] = 125
bot.broker.calls.clear()
bot.manage()
kinds = [x[0] if x[0] == "cancel" else f"sell{x[1]}" for x in bot.broker.calls]
check("target exit cancels the SL order BEFORE selling", kinds[:2] == ["cancel", "sell2"], f"{kinds}")
check("position closed", "NIFTY" not in bot.positions)

# ---------------------------------------------------------------------------
print("\n[5] Backtest simulator rules (premiums from the same Black-Scholes model)")
BTX = dict(xb.BT, slippage_pct=0.0, iv_mult_open=1.0, iv_mult_close=1.0, iv_extra_fade=0.0)
S1 = eb.compile_strategy(dict(CFG["strategy"], min_premium=1, max_premium=1000, otm_steps=0, mtf_tfs=[]))
day = dt.date(2026, 6, 16)                                     # a Tuesday
start = int(dt.datetime.combine(day, dt.time(9, 15), eb.IST).timestamp())


def make_bars(overrides, n=376, spot=25000.0):
    bars = []
    for i in range(n):
        o, h, l, c_ = overrides.get(i, (spot, spot + 1, spot - 1, spot))
        bars.append([start + 60 * i, o, h, l, c_, 0])
    return bars


def sim(bars, fire_at, be=False, bt=BTX, S=S1):
    feat = eb.day_features(bars, S)
    real = eb.signal
    eb.signal = lambda f, i, S_: "CE" if i == fire_at else None
    try:
        return xb.simulate_day("NIFTY", day, bars, feat, lambda ts: 15.0, S, {"breakeven": be}, bt, CFG)
    finally:
        eb.signal = real


SIG = 300                                                       # bar 14:15 -> entry at 14:16 open
tr = sim(make_bars({302: (25000, 25001, 24960, 24962)}), SIG)
check("sim: stop-loss exits at -25%", tr and tr[0]["reason"] == "SL" and abs(tr[0]["ret_pct"] + 25) < 0.2,
      f"{tr[0]['reason']} {tr[0]['ret_pct']}%")
tr = sim(make_bars({302: (25000, 25090, 24999, 25080)}), SIG)
check("sim: target exits at +100%", tr and tr[0]["reason"] == "TARGET" and abs(tr[0]["ret_pct"] - 100) < 0.2,
      f"{tr[0]['reason']} {tr[0]['ret_pct']}%")
tr = sim(make_bars({302: (25000, 25090, 24960, 25000)}), SIG)
check("sim: bar touching BOTH stop and target -> STOP first", tr and tr[0]["reason"] == "SL", f"{tr[0]['reason']}")
itm = {i: (25015, 25016, 25014, 25015) for i in range(302, 376)}      # ITM by 15 pts: intrinsic value survives to the close
tr = sim(make_bars(itm), SIG)
check("sim: square-off at 15:15", tr and tr[0]["reason"] == "SQUARE_OFF" and tr[0]["exit_time"] == "15:15",
      f"{tr[0]['reason']} at {tr[0]['exit_time']}")
tr = sim(make_bars({}), 329)
check("sim: signal closing 14:45 may enter (entry 14:45)", len(tr) == 1 and tr[0]["entry_time"] == "14:45")
tr = sim(make_bars({}), 330)
check("sim: NO entry after 14:45", len(tr) == 0)
tr = sim(make_bars({302: (25000, 25001, 24960, 24962)}), SIG)
risk_used = tr[0]["lots"] * 65 * tr[0]["entry"] * 0.25
check("sim: 1% risk sizing", risk_used <= 2000 and (tr[0]["lots"] + 1) * 65 * tr[0]["entry"] * 0.25 > 2000,
      f"{tr[0]['lots']} lots, risk Rs {risk_used:,.0f}")
tr = sim(make_bars({302: (25000, 25001, 24960, 24962)}), SIG, bt=dict(BTX, slippage_pct=2.0))
check("sim: slippage 2%/side applied (stop exits worse than -25%)", tr[0]["ret_pct"] < -26.0, f"{tr[0]['ret_pct']}%")
check("sim: charges are deducted", tr[0]["costs"] > 0 and tr[0]["net"] < tr[0]["gross"], f"costs Rs {tr[0]['costs']}")
# breakeven: rise to ~+70% (not +100%), fall back to entry -> BE exit instead of -25%
be_path = {302: (25000, 25020, 25000, 25012), 303: (25012, 25013, 24990, 24992)}   # peaks ~+65%, then falls back
tr_on = sim(make_bars(be_path), SIG, be=True)
tr_off = sim(make_bars(be_path), SIG, be=False)
check("sim: breakeven ON exits at ~entry, OFF takes the -25% stop",
      tr_on[0]["reason"] == "BE" and abs(tr_on[0]["ret_pct"]) < 0.5 and tr_off[0]["reason"] == "SL",
      f"ON {tr_on[0]['reason']} {tr_on[0]['ret_pct']}% | OFF {tr_off[0]['reason']} {tr_off[0]['ret_pct']}%")
feat = eb.day_features(make_bars({302: (25000, 25001, 24960, 24962)}), S1)
real = eb.signal
eb.signal = lambda f, i, S_: "CE" if i in (300, 310, 320, 330 - 20) else None
many = xb.simulate_day("NIFTY", day, make_bars({302: (25000, 25001, 24960, 24962), 312: (25000, 25001, 24960, 24962),
                                                322: (25000, 25001, 24960, 24962)}), feat, lambda ts: 15.0, S1, {"breakeven": False}, BTX, CFG)
eb.signal = real
check("sim: max 2 trades per index per day", len(many) == 2, f"{len(many)} trades")

# ---------------------------------------------------------------------------
print("\n[6] Signals: correct direction and no look-ahead")
S2 = eb.compile_strategy(dict(CFG["strategy"], mode="MOM", win_start="09:30", win_end="14:45", mtf_tfs=[]))
t0 = int(dt.datetime.combine(day, dt.time(9, 15), eb.IST).timestamp())
bars, px = [], 25000.0
for i in range(60):
    o = px
    c_ = px + (2 if i % 2 else -1.5)
    bars.append([t0 + 60 * i, o, max(o, c_) + 1, min(o, c_) - 1, c_, 0])
    px = c_
bull = bars + [[t0 + 3600, px, px + 41, px - 1, px + 40, 0]]
bear = bars + [[t0 + 3600, px, px + 1, px - 41, px - 40, 0]]
fb, fr = eb.day_features(bull, S2), eb.day_features(bear, S2)
check("momentum candle up -> CE", eb.signal(fb, 60, S2) == "CE")
check("momentum candle down -> PE", eb.signal(fr, 60, S2) == "PE")
check("quiet bars -> no trade", eb.signal(eb.day_features(bars, S2), 59, S2) is None)
for mode in ("MOM", "EXT", "ORB"):
    Sx = eb.compile_strategy(dict(CFG["strategy"], mode=mode, win_start="09:30", win_end="14:45", mtf_tfs=[]))
    full = eb.day_features(bull + [[t0 + 3660 + 60 * k, px + 40 - 5 * k, px + 41, px - 90, px - 80, 0] for k in range(5)], Sx)
    cut = eb.day_features(bull, Sx)
    same = all(eb.signal(full, i, Sx) == eb.signal(cut, i, Sx) for i in range(len(bull)))
    check(f"{mode}: signal on a bar does not depend on later bars", same)
pk = eb.pick_strike(25012, "CE", 50, lambda K: {25000: 80, 25050: 40, 25100: 15}.get(K), CFG["strategy"] | {"otm_steps": 2})
check("premium filter [20,150]: skips Rs15 strike, takes nearest allowed", pk == (25050, 40), f"{pk}")
check("no strike in range -> skip", eb.pick_strike(25000, "PE", 50, lambda K: 5, CFG["strategy"]) is None)

# ---------------------------------------------------------------------------
print("\n[7] Recorder")


class RecBroker:
    def ltp(self, s):
        return {"NSE:NIFTY50-INDEX": 24523.0}

    def _call(self, fn, data):
        return {"d": [{"n": s, "v": {"lp": 10.0 + i, "bid": 9.9, "ask": 10.1, "volume": 100}}
                      for i, s in enumerate(data["symbols"].split(","))]}

    class f:
        quotes = None


class RecMaster:
    def __init__(self, exp):
        rows = []
        for k in range(23500, 25600, 50):
            for t in ("CE", "PE"):
                rows.append(g.OptionRow(f"NSE:NIFTY{k}{t}", k, t, exp, 65, .05))
        self.rows, self.exp = rows, exp

    def nearest_expiry(self, name, now):
        return self.exp

    def chain(self, name, expiry):
        return self.rows


now = g.now_ist().replace(hour=11, minute=0)
n = rec.record_once(RecBroker(), RecMaster(now.replace(hour=15, minute=30)), CFG, "NIFTY", now, TMP)
rows = list(csv.reader(open(os.path.join(TMP, f"{now.date()}_NIFTY.csv"))))
check("recorder: ATM +/-3 strikes x CE/PE = 14 rows", n == 14 and len(rows) == 15, f"{n} rows")
check("recorder: ATM is 24500, strikes 24350..24650",
      {r[5] for r in rows[1:]} == {str(float(k)) if "." in rows[1][5] else str(k) for k in range(24350, 24651, 50)}, "")
n0 = rec.record_once(RecBroker(), RecMaster(now + dt.timedelta(days=2)), CFG, "NIFTY", now, TMP)
check("recorder: nothing recorded on a non-expiry day", n0 == 0)

# ---------------------------------------------------------------------------
print("\n[8] Data download from FYERS (fake broker) and optional use of the backtest result")


class HistBroker:
    class f:
        history = None

    def __init__(self):
        self.calls = []

    def _call(self, fn, data):
        self.calls.append(data)
        t0 = int(dt.datetime.strptime(data["range_from"], "%Y-%m-%d").replace(tzinfo=eb.IST, hour=9, minute=15).timestamp())
        return {"s": "ok", "candles": [[t0 + 60 * i, 100, 101, 99, 100, 0] for i in range(3)]}


hb, folder = HistBroker(), os.path.join(TMP, "data")
xb.sync_data(hb, ["NIFTY", "SENSEX"], folder, 365, today=dt.date(2026, 10, 13))
files = sorted(os.listdir(folder))
check("sync_data downloads NIFTY, SENSEX and VIX into data/",
      [f.split("_")[0] for f in files] == ["NIFTY", "SENSEX", "VIX"], f"{files}")
check("history is fetched in <=60-day chunks", all((dt.datetime.strptime(c["range_to"], "%Y-%m-%d") -
      dt.datetime.strptime(c["range_from"], "%Y-%m-%d")).days <= 59 for c in hb.calls), f"{len(hb.calls)//3} chunks per symbol")
n_before = len(os.listdir(folder))
xb.sync_data(hb, ["NIFTY", "SENSEX"], folder, 365, today=dt.date(2026, 10, 13))
check("re-run for the same window downloads nothing new", len(os.listdir(folder)) == n_before)
hb.calls.clear()
xb.sync_data(hb, ["NIFTY"], folder, 700, today=dt.date(2026, 10, 13))
check("asking for more days back-fills older history", any(c["range_from"] < "2025-10-13" for c in hb.calls), f"{len(hb.calls)} calls")
check("downloaded files are readable by the backtest loader", set(xb.load_dir(folder)) == {"NIFTY", "SENSEX", "VIX"})
import json
bp = os.path.join(TMP, "best.json")
json.dump({"generated": "2026-10-13", "name": "x", "strategy": {"mode": "ORB", "win_start": "09:30", "win_end": "11:00", "otm_steps": 0},
           "breakeven": True, "verdict": "warn", "headline": "h"}, open(bp, "w"))
on = eb.apply_backtest_result(dict(CFG, use_backtest_result=True), bp)
check("use_backtest_result adopts strategy + breakeven", on["strategy"]["mode"] == "ORB" and on["breakeven_enabled"] is True)
check("off by default", eb.apply_backtest_result(CFG, bp) is CFG)
json.dump({"verdict": "bad", "headline": "NO EDGE", "name": "x", "generated": "d", "strategy": {"mode": "ORB"}, "breakeven": True}, open(bp, "w"))
check("never adopts a 'NO EDGE' result", eb.apply_backtest_result(dict(CFG, use_backtest_result=True), bp)["strategy"]["mode"] == CFG["strategy"]["mode"])

# ---------------------------------------------------------------------------
print("\n[9] Multi-timeframe (5m / 15m / 1h) confirmation")
check("MTF filter is ON by default (5m, 15m, 1h)", CFG["strategy"]["mtf_tfs"] == [5, 15, 60])


def trending_days(n_days, slope):
    out, px = [], 25000.0
    d0 = dt.date(2026, 5, 4)
    for dd in range(n_days):
        t0 = int(dt.datetime.combine(d0 + dt.timedelta(days=dd), dt.time(9, 15), eb.IST).timestamp())
        for i in range(375):
            o = px
            px = px + slope + (0.5 if i % 2 else -0.5)
            out.append([t0 + 60 * i, o, max(o, px) + 0.3, min(o, px) - 0.3, px, 0])
    return out


up_c, dn_c = trending_days(8, 0.4), trending_days(8, -0.4)
tr_up, tr_dn = eb.htf_trend(up_c), eb.htf_trend(dn_c)
last_ts = up_c[-1][0] + 60
check("steady uptrend: 5m, 15m and 1h all +1", set(eb.mtf_signs_at(tr_up, last_ts).values()) == {1}, f"{eb.mtf_signs_at(tr_up, last_ts)}")
check("steady downtrend: 5m, 15m and 1h all -1", set(eb.mtf_signs_at(tr_dn, dn_c[-1][0] + 60).values()) == {-1})
check("too little history -> 0 (blocks trading)", set(eb.mtf_signs_at(eb.htf_trend(up_c[:60]), up_c[59][0] + 60).values()) == {0})
# no look-ahead: sign at time T identical whether or not later data exists
T = up_c[1500][0] + 60
a = eb.mtf_signs_at(eb.htf_trend(up_c), T)
b_ = eb.mtf_signs_at(eb.htf_trend(up_c[:1500]), T)
check("HTF trend at T does not depend on later bars", a == b_, f"{a} vs {b_}")
# live helper == backtest lookup (drops the still-forming bucket)
check("live_mtf equals backtest lookup", eb.live_mtf(up_c[:1500], T, [5, 15, 60]) == a)
# a 5-min bucket still forming at T must not be used
tr5 = eb.htf_trend(up_c[:1503], (5,))
ends = tr5[5][0]
check("incomplete 5-minute bucket is never used", all(e <= T for e in ends[:eb.bisect_right(ends, T) if hasattr(eb, "bisect_right") else sum(1 for e in ends if e <= T)]))
# signal needs agreement
Sm = eb.compile_strategy(dict(CFG["strategy"], mode="MOM", win_start="09:30", win_end="14:45", mtf_tfs=[5, 15, 60]))
f_ = eb.day_features(bull, Sm)
f_["mtf"] = [{5: 1, 15: 1, 60: 1}] * len(bull)
check("bullish momentum + all HTF up -> CE", eb.signal(f_, 60, Sm) == "CE")
f_["mtf"] = [{5: 1, 15: 1, 60: -1}] * len(bull)
check("one higher timeframe disagrees -> NO trade", eb.signal(f_, 60, Sm) is None)
f_["mtf"] = [{5: 1, 15: 0, 60: 1}] * len(bull)
check("higher timeframe undecided (0) -> NO trade", eb.signal(f_, 60, Sm) is None)
f_["mtf"] = [None] * len(bull)
check("HTF data missing -> NO trade", eb.signal(f_, 60, Sm) is None)
f_["mtf"] = [{5: -1, 15: -1, 60: -1}] * len(bear)
check("bearish momentum + all HTF down -> PE", eb.signal(eb.day_features(bear, Sm) | {"mtf": f_["mtf"]}, 60, Sm) == "PE")

# ---------------------------------------------------------------------------
print("\n[10] Discipline guards")
check("guards configured", CFG["max_daily_loss_pct"] == 2.0 and CFG["max_consecutive_losses"] == 3 and CFG["max_monthly_loss_pct"] == 6.0)
bot = new_bot()
bot.S = eb.compile_strategy(dict(CFG["strategy"], win_start="09:30", win_end="14:45", mtf_tfs=[]))
check("fresh day: entries allowed", bot.can_enter("NIFTY", at(13, 30)))
bot.realised = -4001                                           # 2% of 2,00,000 = 4,000
check("daily loss limit blocks new entries", not bot.can_enter("NIFTY", at(13, 30)) and "daily" in bot.halted())
bot = new_bot()
bot.S = eb.compile_strategy(dict(CFG["strategy"], win_start="09:30", win_end="14:45", mtf_tfs=[]))
for price in (50, 50, 50):                                     # three stop-outs in a row
    bot.broker.px[SYM] = 60
    bot.enter("NIFTY", "CE", ROW, 60.0, {})
    bot.broker.px[SYM] = price - 10
    bot.manage()
    bot.trades_today["NIFTY"] = 0
check("3 consecutive losses halt the day", bot.loss_streak == 3 and not bot.can_enter("NIFTY", at(13, 30)), f"streak {bot.loss_streak}, {bot.halted()}")
bot2 = new_bot()
bot2.S = bot.S
if os.path.exists(eb.TRADE_LOG):
    os.remove(eb.TRADE_LOG)                                      # isolate the monthly sum
check("monthly guard: empty log -> not halted", bot2.month_pnl() == 0 and bot2.halted() is None)
open(eb.TRADE_LOG, "w").write("date,index,symbol,type,qty,entry_time,entry,exit_time,exit,pnl\n")
open(eb.TRADE_LOG, "a").write(f"{g.now_ist().date()},NIFTY,x,CE,1,t,1,t,1,-5000,-1,SL,PAPER,{{}}\n")
check("monthly guard: -5,000 (< 6% = 12,000) -> not halted", bot2.halted() is None, f"month pnl {bot2.month_pnl():,.0f}")
open(eb.TRADE_LOG, "a").write(f"{g.now_ist().date()},NIFTY,x,CE,1,t,1,t,1,-8000,-1,SL,PAPER,{{}}\n")
check("monthly guard: -13,000 (> 6% of capital) -> halted, no entries",
      "monthly" in (bot2.halted() or "") and not bot2.can_enter("NIFTY", at(13, 30)), f"{bot2.halted()}")

# ---------------------------------------------------------------------------
print("\n[11] Time-anchored breakouts (09:30 / 12:00 / 14:00)")
SA = eb.compile_strategy(dict(CFG["strategy"], mode="ANCHOR", mtf_tfs=[]))
check("three anchors configured: 09:30, 12:00, 14:00", [a for a, _, _ in SA["anchors"]] == [570, 720, 840], f"{SA['anchors']}")


def anchor_day(break_minute, direction, flat=100.0):
    """Quiet, slowly drifting day; a 25-point range is formed BEFORE break_minute, then a break of the range."""
    bars, px = [], 25000.0
    for i in range(375):
        m = 555 + i + 1                                          # close minute of bar i
        o = px
        if break_minute - 30 < m <= break_minute:                 # the range window: oscillate +-12
            c_ = 25000 + (12 if i % 2 else -12)
        elif m == break_minute + 1:                               # break bar closes outside the range
            c_ = 25000 + direction * 40
        elif m > break_minute + 1:
            c_ = 25000 + direction * (40 + (m - break_minute) * 0.3)
        else:
            c_ = 25000 + direction * (m - 555) * 0.02             # gentle drift with the trade direction
        px = c_
        bars.append([start + 60 * i, o, max(o, c_) + 0.5, min(o, c_) - 0.5, c_, 0])
    return bars


for anchor, label in ((720, "12:00"), (840, "14:00")):
    for direction, want in ((1, "CE"), (-1, "PE")):
        bars_ = anchor_day(anchor, direction)
        S_one = eb.compile_strategy(dict(CFG["strategy"], mode="ANCHOR", mtf_tfs=[],
                                         anchors=[x for x in CFG["strategy"]["anchors"] if eb._m(x[0]) == anchor]))
        fa = eb.day_features(bars_, S_one)
        sigs = [(i, eb.signal(fa, i, S_one)) for i in range(len(bars_)) if eb.signal(fa, i, S_one)]
        first_close = fa["mclose"][sigs[0][0]] if sigs else None
        check(f"{label} range break {'up' if direction > 0 else 'down'} -> {want} on the first bar after the anchor",
              bool(sigs) and sigs[0][1] == want and first_close == anchor + 1, f"{sigs[:2]} closes {first_close}")
SA14 = eb.compile_strategy(dict(CFG["strategy"], mode="ANCHOR", mtf_tfs=[], anchors=[("14:00", 30, 45)]))
fb = eb.day_features(anchor_day(840, 1), SA14)
check("no signal before the 14:00 anchor completes", all(eb.signal(fb, i, SA14) is None for i in range(len(fb["c"])) if fb["mclose"][i] <= 840))
fb = eb.day_features(anchor_day(720, 1), SA14)
check("with only the 14:00 anchor on, the 12:00 breakout bars give no signal",
      all(eb.signal(fb, i, SA14) is None for i in range(len(fb["c"])) if 720 < fb["mclose"][i] <= 810))
Sx = eb.compile_strategy(dict(CFG["strategy"], mode="ANCHOR", mtf_tfs=[]))
b_full, b_cut = anchor_day(720, 1), anchor_day(720, 1)[:340]
ff, fc = eb.day_features(b_full, Sx), eb.day_features(b_cut, Sx)
check("ANCHOR: signal on a bar does not depend on later bars", all(eb.signal(ff, i, Sx) == eb.signal(fc, i, Sx) for i in range(340)))
fz = eb.day_features(anchor_day(840, 1)[:100], Sx)
check("incomplete range (data missing) -> no anchor, no trade", fz["anchor"].get(840) is None)

# ---------------------------------------------------------------------------
print("\n[12] Token check")


class TokB:
    class f:
        get_profile = None

    def __init__(self, resp):
        self.resp = resp

    def _call(self, fn, data=None, retries=3):
        return self.resp


check("valid token accepted", xb.token_valid(TokB({"s": "ok", "data": {}})))
check("'Could not authenticate' token rejected", not xb.token_valid(TokB({"s": "error", "message": "Could not authenticate the user"})))
class AuthFail(HistBroker):
    def _call(self, fn, data):
        return {"s": "error", "message": "Could not authenticate the user"}
try:
    xb.fetch_range(AuthFail(), "NSE:NIFTY50-INDEX", dt.date(2026, 10, 1), dt.date(2026, 10, 13))
    check("download aborts on auth error instead of silently continuing", False)
except SystemExit:
    check("download aborts on auth error instead of silently continuing", True)

# ---------------------------------------------------------------------------
print("\n[13] +10% auto trailing stop and non-expiry days")
check("trailing stop configured: trigger +10%", CFG["trail_enabled"] and CFG["trail_trigger_pct"] == 10.0 and CFG["trade_days"] == "all")
bot = new_bot(trail_enabled=True, trail_trigger_pct=10.0, trail_gap_pct=15.0)
bot.broker.px[SYM] = 100
bot.enter("NIFTY", "CE", ROW, 100.0, {})
bot.broker.px[SYM] = 109
bot.manage()
check("below +10%: stop still -25%", abs(bot.positions["NIFTY"].sl - 75.0) < 0.06, f"SL {bot.positions['NIFTY'].sl}")
bot.broker.px[SYM] = 112
bot.manage()
check("at +12%: trail starts, SL = peak x 0.85 = 95.2", abs(bot.positions["NIFTY"].sl - 95.2) < 0.06, f"SL {bot.positions['NIFTY'].sl}")
bot.broker.px[SYM] = 130
bot.manage()
check("peak 130 -> SL 110.5", abs(bot.positions["NIFTY"].sl - 110.5) < 0.06, f"SL {bot.positions['NIFTY'].sl}")
bot.broker.px[SYM] = 120
bot.manage()
check("price falls back: stop never moves down", abs(bot.positions["NIFTY"].sl - 110.5) < 0.06, f"SL {bot.positions['NIFTY'].sl}")
bot.broker.px[SYM] = 110
bot.manage()
last = list(csv.reader(open(eb.TRADE_LOG)))[-1]
check("exit when price hits the trailed stop, labelled TRAIL, locked in a profit", last[11] == "TRAIL" and float(last[8]) >= 100, f"{last[11]} exit {last[8]}")
bot = new_bot(trail_enabled=True)
bot.paper = False
bot.broker = LiveBroker()
bot.broker.px[SYM] = 100
bot.enter("NIFTY", "CE", ROW, 100.0, {})
bot.broker.px[SYM] = 120
bot.manage()
check("live: trailing modifies the BROKER stop order", any(x[0] == "modify" and abs(x[2] - 102.0) < 0.06 for x in bot.broker.calls),
      f"{[x for x in bot.broker.calls if x[0] == 'modify']}")

# day scope
bot = new_bot(trade_days="expiry")
bot.master = FakeMaster(g.now_ist() + dt.timedelta(days=3))
bot.broker.candles_range = lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))
try:
    bot.scan("NIFTY", at(13, 30))
    check("trade_days=expiry ignores a non-expiry day", True)
except AssertionError:
    check("trade_days=expiry ignores a non-expiry day", False)
bot = new_bot(trade_days="non_expiry")
bot.master = FakeMaster(g.now_ist() + dt.timedelta(hours=2))      # expires today
bot.broker.candles_range = lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched"))
try:
    bot.scan("NIFTY", at(13, 30))
    check("trade_days=non_expiry ignores the expiry day", True)
except AssertionError:
    check("trade_days=non_expiry ignores the expiry day", False)
if os.path.exists(eb.TRADE_LOG):
    os.remove(eb.TRADE_LOG)                                          # earlier tests left losses that trip the monthly guard
bot = new_bot(trade_days="all")
bot.master = FakeMaster(g.now_ist() + dt.timedelta(days=3))
fetched = []
bot.broker.candles_range = lambda *a, **k: fetched.append(1) or []
bot.scan("NIFTY", at(13, 30))
check("trade_days=all scans a non-expiry day", fetched == [1])

# backtest: dte pricing, trail, skips
BTN = dict(xb.BT, slippage_pct=0.0)
p0, p3 = xb.premium(25000, 25000, 800, 15.0, "CE", BTN, 0), xb.premium(25000, 25000, 800, 15.0, "CE", BTN, 3)
check("sim: a 3-days-to-expiry ATM option costs much more than on expiry day", p3 > 4 * p0, f"{p0:.1f} vs {p3:.1f}")
S_ne = eb.compile_strategy(dict(CFG["strategy"], min_premium=1, max_premium=1000, otm_steps=0, mtf_tfs=[], min_premium_ne=1, max_premium_ne=2000))
cap_big = dict(CFG, capital=2_000_000, stop_loss_pct_ne=25.0, target_pct_ne=100.0)   # old 25% semantics for these sim tests


def sim_ne(bars, fire_at, trail, dte=3, cfg_=cap_big, ex_extra=None):
    feat = eb.day_features(bars, S_ne)
    real = eb.signal
    eb.signal = lambda f, i, S_: "CE" if i == fire_at else None
    ex_ = {"breakeven": False, "trail": trail, "skips": 0}
    try:
        tr_ = xb.simulate_day("NIFTY", day, bars, feat, lambda ts: 15.0, S_ne, ex_, BTN, cfg_, dte)
    finally:
        eb.signal = real
    return tr_, ex_


up_then_down = {302: (25000, 25040, 25000, 25035), 303: (25035, 25036, 24900, 24905)}   # +10% then a deep reversal
tr_t, _ = sim_ne(make_bars(up_then_down), SIG, (10, 15))
tr_n, _ = sim_ne(make_bars(up_then_down), SIG, None)
check("sim: +10% then reversal -> TRAIL exit keeps more than the plain -25% stop",
      tr_t[0]["reason"] == "TRAIL" and tr_t[0]["ret_pct"] > tr_n[0]["ret_pct"] and tr_n[0]["reason"] == "SL",
      f"trail {tr_t[0]['reason']} {tr_t[0]['ret_pct']}% vs none {tr_n[0]['reason']} {tr_n[0]['ret_pct']}%")
check("sim: trade records days-to-expiry", tr_t[0]["dte"] == 3)
tr_s, ex_s = sim_ne(make_bars({}), SIG, None, 3, dict(CFG, capital=200000, stop_loss_pct_ne=25.0, target_pct_ne=100.0))
check("sim: non-expiry trade skipped when 1 lot risks > 1% (and counted)", len(tr_s) == 0 and ex_s["skips"] == 1, f"skips {ex_s['skips']}")

# ---------------------------------------------------------------------------
print("\n[14] Non-expiry days: tight 5-7.5% stop (hero-or-zero keeps 25%)")
check("config: non-expiry stop 7.5%, target 4x = 30%, expiry-day stop still 25%",
      CFG["stop_loss_pct_ne"] == 7.5 and CFG["target_pct_ne"] == 4 * CFG["stop_loss_pct_ne"] and CFG["stop_loss_pct"] == 25.0)
for v, ok in ((5.0, True), (7.5, True), (7.6, False), (10.0, False), (25.0, False), (4.0, False)):
    try:
        eb.validate_config({**CFG, "stop_loss_pct_ne": v})
        good = True
    except ValueError:
        good = False
    check(f"validate_config: non-expiry stop {v}% {'accepted' if ok else 'REFUSED'}", good == ok)


class ScanMaster:
    def __init__(self, exp):
        self.exp = exp
        self.rows = [g.OptionRow(f"NSE:NIFTY{k}CE", k, "CE", exp, 65, 0.05) for k in range(24800, 25300, 50)] + \
                    [g.OptionRow(f"NSE:NIFTY{k}PE", k, "PE", exp, 65, 0.05) for k in range(24800, 25300, 50)]

    def nearest_expiry(self, name, now):
        return self.exp

    def chain(self, name, expiry):
        return self.rows


def scan_once(exp_delta):
    if os.path.exists(eb.TRADE_LOG):
        os.remove(eb.TRADE_LOG)
    bot = new_bot(trade_days="all")
    bot.S = eb.compile_strategy(dict(CFG["strategy"], mtf_tfs=[], win_start="09:30", win_end="14:45", min_premium=1, max_premium=1000,
                                     min_premium_ne=1, max_premium_ne=1000))
    now_ = at(13, 30)
    bot.master = ScanMaster(now_ + exp_delta)
    base = int(now_.timestamp())
    bot.broker.candles_range = lambda *a, **k: [[base - 60 * (60 - i), 25000, 25001, 24999, 25000, 0] for i in range(60)]
    bot._closed = lambda c: c
    bot.broker.ltp = lambda syms: {s: (25000.0 if "INDEX" in s else 150.0) for s in syms}
    real = eb.signal
    eb.signal = lambda f, i, S_: "CE"
    try:
        bot.scan("NIFTY", now_)
    finally:
        eb.signal = real
    return bot


bot = scan_once(dt.timedelta(days=3))                              # non-expiry day
p = bot.positions.get("NIFTY")
check("non-expiry scan: entered with a 7.5% stop (SL 138.75) and +30% target (195)",
      p is not None and abs(p.sl - 138.75) < 0.06 and abs(p.target - 195.0) < 0.06, f"{p and (p.sl, p.target)}")
check("non-expiry: 1% risk buys 2 lots (130 qty) instead of 0 with a 25% stop", p is not None and p.qty == 130, f"qty {p and p.qty}")
check("non-expiry: trailing gap stored as 7.5%", p is not None and p.meta.get("trail_gap") == 7.5)
bot.broker.px = {p.symbol: 150.0}
bot.cfg = {**bot.cfg, "trail_enabled": True}
bot.broker.px[p.symbol] = 170.0                                    # +13% -> trail starts, SL = 170 x 0.925
bot.broker.ltp = lambda syms: {s: 170.0 for s in syms}
bot.manage()
check("non-expiry trail: SL = peak x (1 - 7.5%) = 157.25", abs(bot.positions["NIFTY"].sl - 157.25) < 0.06, f"SL {bot.positions['NIFTY'].sl}")
bot = scan_once(dt.timedelta(hours=2))                             # expiry day, same premium
check("expiry day keeps the 25% stop: 1 lot would risk Rs 2,437 > Rs 2,000 -> skipped", "NIFTY" not in bot.positions)

# backtest uses the tight stop on non-expiry days only
BTX0 = dict(xb.BT, slippage_pct=0.0)
cfg75 = dict(CFG, capital=2_000_000)
drop = {302: (25000, 25001, 24975, 24978)}
feat_ = eb.day_features(make_bars(drop), S_ne)
real = eb.signal
eb.signal = lambda f, i, S_: "CE" if i == SIG else None
t_ne = xb.simulate_day("NIFTY", day, make_bars(drop), feat_, lambda ts: 15.0, S_ne, {"breakeven": False, "trail": None}, BTX0, cfg75, 3)
t_ex = xb.simulate_day("NIFTY", day, make_bars(drop), feat_, lambda ts: 15.0, S1, {"breakeven": False, "trail": None}, BTX0, cfg75, 0)
eb.signal = real
check("sim: non-expiry trade stops out at -7.5%", t_ne and t_ne[0]["reason"] == "SL" and abs(t_ne[0]["ret_pct"] + 7.5) < 0.2, f"{t_ne[0]['reason']} {t_ne[0]['ret_pct']}%")
check("sim: expiry-day trade keeps the -25% stop", t_ex and abs(t_ex[0]["ret_pct"] + 25) < 0.3 or (t_ex and t_ex[0]["reason"] != "SL"), f"{t_ex and (t_ex[0]['reason'], t_ex[0]['ret_pct'])}")
eb.signal = lambda f, i, S_: "CE" if i == SIG else None
rise = {302: (25000, 25095, 25000, 25092)}
t_up = xb.simulate_day("NIFTY", day, make_bars(rise), feat_, lambda ts: 15.0, S_ne, {"breakeven": False, "trail": None}, BTX0, cfg75, 3)
eb.signal = real
check("sim: non-expiry target at +30%", t_up and t_up[0]["reason"] == "TARGET" and abs(t_up[0]["ret_pct"] - 30) < 0.5, f"{t_up and (t_up[0]['reason'], t_up[0]['ret_pct'])}")
eb.signal = lambda f, i, S_: "CE" if i == SIG else None
t_sz = xb.simulate_day("NIFTY", day, make_bars(drop), feat_, lambda ts: 15.0, S_ne, {"breakeven": False, "trail": None}, BTX0, dict(CFG, capital=200000), 3)
eb.signal = real
risk_ne = t_sz[0]["lots"] * 65 * t_sz[0]["entry"] * 0.075
check("sim: 1% risk sizing uses the 7.5% stop (Rs 2,000 budget)", risk_ne <= 2000 and (t_sz[0]["lots"] + 1) * 65 * t_sz[0]["entry"] * 0.075 > 2000,
      f"{t_sz[0]['lots']} lots risk Rs {risk_ne:,.0f}")

# ---------------------------------------------------------------------------
print("\n[15] Live-trading gate")
import gate
import json as _json
gdir = tempfile.mkdtemp()
best_p, log_p = os.path.join(gdir, "best.json"), os.path.join(gdir, "trades.csv")


def write_log(pnls, mode="PAPER"):
    with open(log_p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "index", "symbol", "type", "qty", "entry_time", "entry", "exit_time", "exit", "pnl", "pnl_pct", "reason", "mode", "meta"])
        for i, p in enumerate(pnls):
            w.writerow(["2026-10-01", "NIFTY", "x", "CE", 1, "t", 1, "t", 1, p, 0, "SL", mode, "{}"])


ok, why = gate.live_allowed(200000, best_p, log_p)
check("no backtest + no paper trades -> live refused", not ok and len(why) == 2, f"{why}")
_json.dump({"verdict": "bad", "headline": "NO EDGE FOUND."}, open(best_p, "w"))
write_log([500] * 45 + [-300] * 5)
ok, why = gate.live_allowed(200000, best_p, log_p)
check("backtest says NO EDGE -> refused even with profitable paper trades", not ok and any("NO EDGE" in w for w in why), f"{why}")
_json.dump({"verdict": "warn", "headline": "WEAK"}, open(best_p, "w"))
write_log([500] * 10)
ok, why = gate.live_allowed(200000, best_p, log_p)
check("too few paper trades -> refused", not ok and any("only 10 paper" in w for w in why))
write_log([-200, 150] * 25)
ok, why = gate.live_allowed(200000, best_p, log_p)
check("50 paper trades but net negative -> refused", not ok and any("net P&L" in w for w in why), f"{why}")
write_log([300, 300, -150] * 17)
ok, why = gate.live_allowed(200000, best_p, log_p)
check("profitable paper run (51 trades, PF 4) + backtest not 'bad' -> gate PASSES", ok and not why, f"{why}")
write_log([400] * 30 + [-14000] + [400] * 20)
ok, why = gate.live_allowed(200000, best_p, log_p)
check("a 14,000 drawdown (> 6% of 2L) -> refused", not ok and any("drawdown" in w for w in why), f"{why}")
write_log([300] * 60, mode="LIVE")
check("LIVE rows are not counted as paper evidence", gate.paper_stats(log_p)["n"] == 0)
real_gate = gate.live_allowed
gate.live_allowed = lambda capital: (False, ["test reason"])
try:
    eb.check_live_gate({**CFG, "paper_trading": False, "live_gate": True})
    check("bot refuses live mode when the gate is closed", False)
except SystemExit as e:
    check("bot refuses live mode when the gate is closed", "test reason" in str(e))
eb.check_live_gate({**CFG, "paper_trading": False, "live_gate": False})
check("live_gate=False overrides (with a warning) - your explicit choice", True)
gate.live_allowed = real_gate
check("live_gate is ON by default", CFG["live_gate"] is True and CFG["paper_trading"] is True)
shutil.rmtree(gdir, ignore_errors=True)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
