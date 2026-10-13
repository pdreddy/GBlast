"""
TEST 2 - Live connection test. Needs your FYERS login (run fyers_login.py first).
Places NO orders. Works any time (uses the last trading day's candles when the market is closed).

Checks: token works, symbol files download, lot sizes & expiries, live quotes for the 3 indices,
what the momentum filter sees right now, and the Greeks / gamma-blast score of near-ATM options.

Run:  python test_connection.py
"""
import datetime as dt
import sys

import gamma_blast as g

ok = True


def step(title):
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


step("1. Login token")
try:
    broker = g.Broker(g.CONFIG)
    prof = broker._call(broker.f.get_profile)
    if prof.get("s") != "ok":
        raise RuntimeError(prof)
    d = prof.get("data", {})
    print(f"OK  logged in as {d.get('name')} ({d.get('fy_id')})")
    funds = broker._call(broker.f.funds)
    for row in funds.get("fund_limit", []) or []:
        if row.get("title") in ("Available Balance", "Total Balance"):
            print(f"    {row['title']}: Rs {row.get('equityAmount', 0):,.2f}")
except FileNotFoundError:
    sys.exit("FAIL  access_token.txt not found - run fyers_login.py first.")
except Exception as e:
    sys.exit(f"FAIL  token not working ({e}). Run fyers_login.py again.")

step("2. Symbol master (expiries, strikes, lot sizes)")
master = g.SymbolMaster()
try:
    master.load(g.CONFIG["indices"])
except Exception as e:
    sys.exit(f"FAIL  {e}")
now = g.now_ist()
for name in master.options:
    exp = master.nearest_expiry(name, now)
    chain = master.chain(name, exp)
    print(f"OK  {name:10s} lot {chain[0].lot:4d} | nearest expiry {exp:%a %d-%b-%Y} | "
          f"{len(chain)} strikes | e.g. {chain[0].ticker}")

step("3. Live index quotes")
spots = broker.ltp([ic["spot"] for ic in g.CONFIG["indices"].values() if ic["enabled"]])
for name, ic in g.CONFIG["indices"].items():
    if not ic["enabled"]:
        continue
    v = spots.get(ic["spot"])
    print(("OK  " if v else "FAIL") + f"  {name:10s} {ic['spot']:22s} {v}")
    ok &= bool(v)

step("4. Momentum filter + gamma scan (no orders)")
bot = g.GammaBlast.__new__(g.GammaBlast)
bot.cfg, bot.paper, bot.broker, bot.master = g.CONFIG, True, broker, master
for name, ic in g.CONFIG["indices"].items():
    if not ic["enabled"]:
        continue
    # last trading day with data (today if market open)
    candles, day = [], now.date()
    for back in range(7):
        dd = (now - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        r = broker._call(broker.f.history, {"symbol": ic["spot"], "resolution": "1", "date_format": "1",
                                            "range_from": dd, "range_to": dd, "cont_flag": "1"})
        candles = r.get("candles") or []
        if candles:
            day = dd
            break
    side, info = bot.momentum_signal(candles)
    print(f"\n{name}: {len(candles)} one-minute candles from {day}")
    print(f"   signal now: {side or 'NONE'}   {info}")

    spot = spots.get(ic["spot"])
    exp = master.nearest_expiry(name, now)
    if not spot or not exp:
        continue
    for opt in ("CE", "PE"):
        chain = [o for o in master.chain(name, exp) if o.opt_type == opt]
        strikes = sorted({o.strike for o in chain})
        atm = min(strikes, key=lambda k: abs(k - spot))
        row = next(o for o in chain if o.strike == atm)
        ltp = broker.ltp([row.ticker]).get(row.ticker)
        if not ltp:
            print(f"   {opt} ATM {atm}: no quote")
            continue
        T = max((exp - now).total_seconds(), 600) / (365 * 24 * 3600)
        iv = g.implied_vol(ltp, spot, atm, T, g.CONFIG["risk_free_rate"], opt)
        if not iv:
            print(f"   {opt} ATM {atm}: premium {ltp} (IV not solvable)")
            continue
        dl, gm, th = g.greeks(spot, atm, T, g.CONFIG["risk_free_rate"], iv, opt)
        dS = spot * g.CONFIG["blast_move_pct"] / 100
        gain = (abs(dl) * dS + 0.5 * gm * dS * dS + th * g.CONFIG["expected_hold_min"] / (365 * 24 * 60)) / ltp * 100
        lots = bot.size(name, ltp, row.lot)
        print(f"   {opt} ATM {atm:.0f}: premium {ltp:7.2f} | IV {iv*100:5.1f}% | delta {dl:+.2f} | "
              f"blast score {gain:5.1f}% (needs {ic['min_blast_gain_pct']}%) | lots allowed {lots}")

print("\n" + ("ALL CONNECTION CHECKS PASSED" if ok else "SOME CHECKS FAILED - see above"))
