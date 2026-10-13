"""
TEST 1 - Offline logic test. Needs NO FYERS account, NO internet, NO market hours.
Checks: option maths, momentum signals, position sizing, and the 40% SL / +200% target /
trailing-stop rules on simulated premium paths.

Run:  python test_offline.py      (or .venv\\Scripts\\python test_offline.py)
"""
import datetime as dt
import os
import sys
import types

# fake the FYERS library so this runs without an account
fake = types.ModuleType("fyers_apiv3")
fake.fyersModel = types.SimpleNamespace()
sys.modules["fyers_apiv3"] = fake

import gamma_blast as g

TEST_TRADE_LOG = "test_trades.csv"
TEST_STATE = "test_state.json"
g.TRADE_LOG, g.STATE_FILE = TEST_TRADE_LOG, TEST_STATE

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}  {detail}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
print("\n[1] Option maths (Black-Scholes, IV, Greeks)")
S, K, T = 25000, 25000, 1 / 365
price = g.bs_price(S, K, T, 0.065, 0.12, "CE")
iv = g.implied_vol(price, S, K, T, 0.065, "CE")
d, gm, th = g.greeks(S, K, T, 0.065, iv, "CE")
check("IV recovered", abs(iv - 0.12) < 1e-4, f"iv={iv:.4f}")
check("ATM call delta ~0.5", 0.45 < d < 0.58, f"delta={d:.3f}")
pd_, _, _ = g.greeks(S, K, T, 0.065, iv, "PE")
check("ATM put delta ~-0.5", -0.55 < pd_ < -0.42, f"delta={pd_:.3f}")
check("theta negative", th < 0)

# ---------------------------------------------------------------------------
print("\n[2] Momentum signals")
bot_cfg = types.SimpleNamespace(cfg=g.CONFIG)
base = dt.datetime(2026, 10, 13, 9, 15, tzinfo=g.IST)
c, px = [], 25000
for i in range(40):
    o = px
    cl = px + (3 if i % 2 else -3)
    c.append([int((base + dt.timedelta(minutes=i)).timestamp()), o, max(o, cl) + 2, min(o, cl) - 2, cl, 0])
    px = cl
bull = c + [[int((base + dt.timedelta(minutes=40)).timestamp()), px, px + 42, px - 1, px + 40, 0]]
bear = c + [[int((base + dt.timedelta(minutes=40)).timestamp()), px, px + 1, px - 42, px - 40, 0]]
check("bullish breakout -> CALL", g.GammaBlast.momentum_signal(bot_cfg, bull)[0] == "CE")
check("bearish breakdown -> PUT", g.GammaBlast.momentum_signal(bot_cfg, bear)[0] == "PE")
check("sideways -> no trade", g.GammaBlast.momentum_signal(bot_cfg, c)[0] is None)

# ---------------------------------------------------------------------------
print("\n[3] Position sizing (capital and risk from CONFIG)")
cap = g.CONFIG["capital"]
risk = cap * g.CONFIG["risk_per_trade_pct"] / 100
for name, prem, lot in [("NIFTY", 60, 75), ("NIFTY", 120, 75), ("SENSEX", 150, 20), ("BANKNIFTY", 300, 35)]:
    lots = g.GammaBlast.size(bot_cfg, name, prem, lot)
    loss_at_sl = lots * lot * prem * g.CONFIG["stop_loss_pct"] / 100
    ok = lots == 0 or loss_at_sl <= risk + 1
    check(f"{name} premium {prem}, lot {lot}", ok,
          f"-> {lots} lot(s), loss if SL hit = Rs {loss_at_sl:,.0f} (budget Rs {risk:,.0f})")
    if lots == 0:
        print(f"        note: skipped - 1 lot risks Rs {prem*lot*0.4:,.0f}, more than your Rs {risk:,.0f} budget")


# ---------------------------------------------------------------------------
print("\n[4] Exit rules on simulated premium paths (paper mode)")


class FakeBroker:
    def __init__(self):
        self.px = {}

    def ltp(self, symbols):
        return {s: self.px[s] for s in symbols if s in self.px}


def new_bot():
    bot = g.GammaBlast.__new__(g.GammaBlast)
    bot.cfg, bot.paper, bot.broker = g.CONFIG, True, FakeBroker()
    bot.positions, bot.trades_today, bot.last_loss_time = {}, {"NIFTY": 0}, {}
    bot.realised, bot.last_scan_min = 0.0, {}
    return bot


def run_path(path, entry=100.0):
    bot = new_bot()
    sym = "NSE:NIFTYTEST25000CE"
    row = g.OptionRow(sym, 25000, "CE", g.now_ist() + dt.timedelta(hours=3), 75, 0.05)
    bot.broker.px[sym] = entry
    bot.enter("NIFTY", "CE", row, entry, {"test": True})
    p = bot.positions.get("NIFTY")
    sl_hist = []
    for price in path:
        bot.broker.px[sym] = price
        bot.manage()
        if "NIFTY" not in bot.positions:
            break
        sl_hist.append(bot.positions["NIFTY"].sl)
    import csv
    last = list(csv.reader(open(TEST_TRADE_LOG)))[-1]
    return float(last[8]), last[11], sl_hist


g.CONFIG["capital"] = max(g.CONFIG["capital"], 500000)  # make sure 1 lot is allowed in the simulation
if os.path.exists(TEST_TRADE_LOG):
    os.remove(TEST_TRADE_LOG)

exit_px, reason, _ = run_path([95, 80, 70, 61, 59])
check("Stop loss at -40%", reason == "SL" and exit_px <= 60, f"exit {exit_px} reason {reason}")

exit_px, reason, sl = run_path([120, 150, 180, 200, 250, 300, 301])
check("Target at +200%", reason == "TARGET" and exit_px >= 300, f"exit {exit_px} reason {reason}")

exit_px, reason, sl = run_path([130, 151, 170, 190, 160, 142])
check("Trail starts at +50%", any(s > 60 for s in sl), f"SL path {sl}")
check("Trail = 25% below peak (190 -> 142.5)", reason == "TRAIL" and abs(exit_px - 142) < 1,
      f"exit {exit_px} reason {reason}")
check("Trailing stop never moves down", all(b >= a for a, b in zip(sl, sl[1:])))

exit_px, reason, sl = run_path([130, 140, 145, 120, 100, 80, 59])
check("No trail below +50% (normal SL used)", reason == "SL", f"exit {exit_px} reason {reason}")

for f in (TEST_TRADE_LOG, TEST_STATE):
    if os.path.exists(f):
        os.remove(f)

print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
