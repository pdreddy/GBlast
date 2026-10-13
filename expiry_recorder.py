"""
EXPIRY RECORDER - saves REAL expiry-day option premiums so future backtests need no modelling.

On every expiry day (NIFTY weekly Tue, SENSEX weekly Thu - taken from the FYERS symbol master, never
hard-coded) it records, once per minute from 09:15 to 15:30, the quote of ATM +/- 3 strikes (CE and PE)
of the expiring contract:   data/expiry_premiums/YYYY-MM-DD_<INDEX>.csv
Columns: time, epoch, index, spot, atm, strike, type, symbol, ltp, bid, ask, volume

It places NO orders. Start it before 09:15 on an expiry day (expiry_recorder.command), leave it running.
Several weeks of these files = a real-premium dataset (incl. real IV behaviour and bid/ask spreads).
"""
import csv
import datetime as dt
import os
import time

import expiry_blast as eb
import gamma_blast as g

OUT_DIR = os.path.join("data", "expiry_premiums")
STRIKES_EACH_SIDE = 3
HEADER = ["time", "epoch", "index", "spot", "atm", "strike", "type", "symbol", "ltp", "bid", "ask", "volume"]
START, END = dt.time(9, 15), dt.time(15, 30)


def snapshot_symbols(master, name, expiry, spot, step, n=STRIKES_EACH_SIDE):
    """ATM +/- n strikes, CE and PE, of the given expiry -> (atm, [OptionRow, ...])"""
    atm = round(spot / step) * step
    want = {atm + k * step for k in range(-n, n + 1)}
    rows = [o for o in master.chain(name, expiry) if o.strike in want]
    return atm, sorted(rows, key=lambda o: (o.strike, o.opt_type))


def fetch_quotes(broker, symbols):
    """symbol -> (ltp, bid, ask, volume).  One call per <=50 symbols."""
    out = {}
    for i in range(0, len(symbols), 50):
        resp = broker._call(broker.f.quotes, {"symbols": ",".join(symbols[i:i + 50])})
        for d in resp.get("d", []) or []:
            v = d.get("v") or {}
            if v.get("lp"):
                out[d["n"]] = (float(v["lp"]), v.get("bid", ""), v.get("ask", ""), v.get("volume", v.get("vol_traded_today", "")))
    return out


def append_rows(path, rows):
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(HEADER)
        w.writerows(rows)


def record_once(broker, master, cfg, name, now, out_dir=OUT_DIR):
    """Record one minute for one index. Returns number of rows written (0 if not an expiry day)."""
    expiry = master.nearest_expiry(name, now)
    if not expiry or expiry.date() != now.date():
        return 0
    ic = cfg["indices"][name]
    spot = broker.ltp([ic["spot"]]).get(ic["spot"])
    if not spot:
        return 0
    atm, rows = snapshot_symbols(master, name, expiry, spot, ic["strike_step"])
    quotes = fetch_quotes(broker, [r.ticker for r in rows])
    stamp = now.strftime("%H:%M")
    out = [[stamp, int(now.timestamp()), name, spot, atm, r.strike, r.opt_type, r.ticker, *quotes[r.ticker]]
           for r in rows if r.ticker in quotes]
    append_rows(os.path.join(out_dir, f"{now.date()}_{name}.csv"), out)
    return len(out)


def main():
    cfg = eb.CONFIG
    broker = g.Broker(cfg)
    master = g.SymbolMaster()
    master.load({k: v for k, v in cfg["indices"].items() if v["enabled"]})
    now = g.now_ist()
    today = [n for n, v in cfg["indices"].items() if v["enabled"]
             for e in [master.nearest_expiry(n, now)] if e and e.date() == now.date()]
    if not today:
        print("No NIFTY/SENSEX expiry today - nothing to record.")
        return
    print(f"Recording {today} every minute until {END}. Files go to {OUT_DIR}/. Ctrl+C to stop.")
    while True:
        now = g.now_ist()
        if now.weekday() >= 5 or now.time() >= END:
            print("Market closed - recording finished.")
            return
        if now.time() >= START and now.second >= 1:
            for n in today:
                try:
                    k = record_once(broker, master, cfg, n, now)
                    print(f"{now:%H:%M} {n}: {k} quotes saved")
                except Exception as e:
                    print(f"{now:%H:%M} {n}: error {e}")
            time.sleep(max(1, 60 - g.now_ist().second))       # next minute boundary
        else:
            time.sleep(5)


if __name__ == "__main__":
    main()
