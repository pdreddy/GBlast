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


def new_bot(be=False):
    cfg = dict(CFG, breakeven_enabled=be, capital=200000, paper_slippage_pct=0.0)
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
bot.S = eb.compile_strategy(dict(CFG["strategy"], win_start="09:30", win_end="14:45"))
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
S1 = eb.compile_strategy(dict(CFG["strategy"], min_premium=1, max_premium=1000, otm_steps=0))
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
S2 = eb.compile_strategy(dict(CFG["strategy"], mode="MOM", win_start="09:30", win_end="14:45"))
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
    Sx = eb.compile_strategy(dict(CFG["strategy"], mode=mode, win_start="09:30", win_end="14:45"))
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
xb.sync_data(hb, ["NIFTY", "SENSEX"], folder, today=dt.date(2026, 10, 13))
files = sorted(os.listdir(folder))
check("sync_data downloads NIFTY, SENSEX and VIX into data/",
      [f.split("_")[0] for f in files] == ["NIFTY", "SENSEX", "VIX"], f"{files}")
check("history is fetched in <=60-day chunks", all((dt.datetime.strptime(c["range_to"], "%Y-%m-%d") -
      dt.datetime.strptime(c["range_from"], "%Y-%m-%d")).days <= 59 for c in hb.calls), f"{len(hb.calls)//3} chunks per symbol")
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

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
