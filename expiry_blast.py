"""
EXPIRY BLAST - expiry-day "hero or zero" index option BUYER for FYERS API v3
NIFTY (weekly, Tuesday)  +  SENSEX (weekly, Thursday)         PAPER MODE IS ON BY DEFAULT

Rules (strict 1:4)
  * Trades ONLY on the expiry day of each index, ONLY the expiring contract.
  * Buys ATM or 1-2 strikes OTM (premium must be inside [min_premium, max_premium]).
  * Stop loss  -25% of entry premium  -> real broker-side SL-L order placed right after entry.
  * Target     +100% (premium doubles).
  * Optional breakeven: once +60% is reached the stop moves up to entry (cost).   [config flag]
  * Max 2 trades per index per day, 1 open position per index at a time.
  * No entries after 14:45 IST, everything squared off at 15:15 IST.
  * Risk 1% of capital per trade (lots = risk / (premium x 25% x lot size)); if 1 lot risks more
    than that, the trade is skipped.

Entry modes (strategy.mode) - one is picked, they are NOT combined:
  ORB  opening-range breakout   : close crosses the 09:15-09:30 high/low
  EXT  day-extreme breakout     : close makes a new high/low of the day (use with an afternoon window)
  MOM  momentum candle          : body >= 60% of range AND range >= 1.5 x ATR14
  All modes also need: price on the right side of VWAP and EMA9 vs EMA21 in the trade direction.

The signal code (day_features / signal / pick_strike / size_lots) is shared with
expiry_backtest.py, so what you backtest is what the bot trades.

IMPORTANT: the default strategy settings below are a PRIOR CHOICE, not a validated edge.
Run expiry_backtest on your real data and read the verdict before trusting any of it,
then paper trade for several weeks.

Order safety (reused from gamma_blast): entry MARKET buy -> immediate broker-side SL-L ->
exits cancel the SL order FIRST, confirm, then sell -> KILL file / stop.command squares off ->
state file lets the bot resume after a restart.
"""
import csv
import datetime as dt
import json
import logging
import os
import time
from dataclasses import asdict

import gamma_blast as g
from config import CLIENT_ID

IST = g.IST

# =====================================================================================
# CONFIG - edit here
# =====================================================================================
CONFIG = {
    "client_id": CLIENT_ID,
    "access_token_file": "access_token.txt",
    "paper_trading": True,                  # <-- keep True until you have weeks of paper results

    # ---- money management ----
    "capital": 200000,
    "risk_per_trade_pct": 1.0,              # INR lost if the 25% stop is hit = 1% of capital
    "max_capital_per_trade_pct": 40.0,      # premium outlay cap
    "max_open_positions": 2,                # 1 per index x 2 indices
    "max_trades_per_index": 2,              # per expiry day

    # ---- exits (strict 1:4) ----
    "stop_loss_pct": 25.0,
    "target_pct": 100.0,
    "breakeven_enabled": False,             # test with and without (the backtest does both)
    "breakeven_trigger_pct": 60.0,          # at +60% move the stop to entry
    "sl_limit_buffer_pct": 5.0,             # SL-L limit price this % below the trigger
    "gap_fallback_pct": 8.0,                # LTP this % below SL while SL still pending -> force exit
    "paper_slippage_pct": 2.0,              # paper fills are made worse by this % per side

    # ---- timing (IST) ----
    "entry_end": "14:45",
    "square_off": "15:15",
    "manage_interval_sec": 2,

    # ---- entry rules (see module docstring) ----
    "strategy": {
        "mode": "EXT",                      # ORB | EXT | MOM
        "win_start": "13:00",               # afternoon gamma window; use 09:30-11:00 for morning
        "win_end": "14:45",
        "otm_steps": 1,                     # preferred strike: 0 = ATM, 1 or 2 = OTM
        "max_otm_steps": 2,
        "min_premium": 20,
        "max_premium": 150,
        "ema_fast": 9, "ema_slow": 21,
        "atr_period": 14, "atr_mult": 1.5,  # MOM: range >= 1.5 x ATR14
        "body_ratio": 0.6,                  # MOM: body >= 60% of range
        "orb_end": "09:30",
        "warmup_bars": 22,
    },

    # ---- instruments (NIFTY Tue / SENSEX Thu weekly expiries; the symbol master decides the date) ----
    "indices": {
        "NIFTY":  {"enabled": True, "spot": "NSE:NIFTY50-INDEX", "master": "NSE_FO",
                   "underlying": "NIFTY", "strike_step": 50, "max_lots": 10},
        "SENSEX": {"enabled": True, "spot": "BSE:SENSEX-INDEX", "master": "BSE_FO",
                   "underlying": "SENSEX", "strike_step": 100, "max_lots": 10},
    },
}

STATE_FILE = "expiry_blast_state.json"
TRADE_LOG = "expiry_blast_trades.csv"
KILL_FILE = "KILL"

# gamma_blast configures root logging into gamma_blast.log on import; give this bot its own log
for _h in list(logging.root.handlers):
    logging.root.removeHandler(_h)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("expiry_blast.log"), logging.StreamHandler()])
log = logging.getLogger("expiry_blast")


# =====================================================================================
# Shared strategy code (used by the live bot AND the backtest)
# =====================================================================================
def _m(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def compile_strategy(s: dict) -> dict:
    """Turn the human-readable strategy block into a flat dict of numbers (fast to evaluate)."""
    out = dict(s)
    for k in ("win_start", "win_end", "orb_end"):
        out[k] = _m(s[k])
    return out


def day_features(candles, S):
    """Indicators for ONE trading day. candles = [[epoch, o, h, l, c, v], ...] closed bars in order.
    Everything at index i uses only bars <= i (ATR and day high/low use bars < i), so there is no look-ahead."""
    n = len(candles)
    ts = [c[0] for c in candles]
    o = [c[1] for c in candles]; h = [c[2] for c in candles]
    l = [c[3] for c in candles]; c_ = [c[4] for c in candles]
    mclose = [((t + 19800) % 86400) // 60 + 1 for t in ts]          # minute-of-day when the bar CLOSES (IST)

    def ema(vals, p):
        k, e, out = 2 / (p + 1), vals[0], []
        for v in vals:
            e = v * k + e * (1 - k)
            out.append(e)
        return out

    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c_[i - 1]), abs(l[i] - c_[i - 1])) for i in range(1, n)]
    ap = S["atr_period"]
    atr = [None] * n
    for i in range(ap + 1, n):
        atr[i] = sum(tr[i - ap:i]) / ap                                # previous `ap` bars, excludes bar i

    vols = [c[5] if len(c) > 5 else 0 for c in candles]
    use_vol = sum(vols) > 0                                           # FYERS index volume is usually 0 -> TWAP
    pv = vv = 0.0
    vwap = []
    for i in range(n):
        w = vols[i] if use_vol else 1.0
        pv += (h[i] + l[i] + c_[i]) / 3 * w
        vv += w
        vwap.append(pv / vv if vv else c_[i])

    hi_prior, lo_prior = [None] * n, [None] * n
    hh, ll = float("-inf"), float("inf")
    for i in range(n):
        if i:
            hi_prior[i], lo_prior[i] = hh, ll
        hh, ll = max(hh, h[i]), min(ll, l[i])

    orb_idx = [i for i in range(n) if mclose[i] <= S["orb_end"]]
    orb_hi = max(h[i] for i in orb_idx) if orb_idx else None
    orb_lo = min(l[i] for i in orb_idx) if orb_idx else None
    return {"n": n, "ts": ts, "o": o, "h": h, "l": l, "c": c_, "mclose": mclose, "atr": atr,
            "ema_f": ema(c_, S["ema_fast"]), "ema_s": ema(c_, S["ema_slow"]), "vwap": vwap,
            "hi_prior": hi_prior, "lo_prior": lo_prior, "orb_hi": orb_hi, "orb_lo": orb_lo,
            "orb_n": len(orb_idx)}


def signal(f, i, S):
    """'CE', 'PE' or None for the bar that just closed (index i)."""
    if i < S["warmup_bars"] or i >= f["n"]:
        return None
    t = f["mclose"][i]
    if not (S["win_start"] <= t <= S["win_end"]):
        return None
    c, o_, h, l = f["c"][i], f["o"][i], f["h"][i], f["l"][i]
    up = c > f["vwap"][i] and f["ema_f"][i] > f["ema_s"][i]
    dn = c < f["vwap"][i] and f["ema_f"][i] < f["ema_s"][i]
    if not (up or dn):
        return None
    mode = S["mode"]
    if mode == "ORB":
        if f["orb_hi"] is None or i < f["orb_n"]:
            return None
        pc = f["c"][i - 1]
        if up and c > f["orb_hi"] and pc <= f["orb_hi"]:
            return "CE"
        if dn and c < f["orb_lo"] and pc >= f["orb_lo"]:
            return "PE"
    elif mode == "EXT":
        if up and c > f["hi_prior"][i]:
            return "CE"
        if dn and c < f["lo_prior"][i]:
            return "PE"
    elif mode == "MOM":
        rng, body, atr = h - l, abs(c - o_), f["atr"][i]
        if atr and rng >= S["atr_mult"] * atr and rng > 0 and body >= S["body_ratio"] * rng:
            if up and c > o_:
                return "CE"
            if dn and c < o_:
                return "PE"
    return None


def pick_strike(spot, cp, step, price_fn, S):
    """ATM or 1-2 strikes OTM. Candidates whose premium is inside [min,max] are kept and the one closest to
    the preferred OTM distance wins. price_fn(strike) -> premium or None. Returns (strike, premium) or None."""
    atm = round(spot / step) * step
    sign = 1 if cp == "CE" else -1
    best = None
    for n in range(0, S["max_otm_steps"] + 1):
        K = atm + sign * n * step
        px = price_fn(K)
        if px is None or not (S["min_premium"] <= px <= S["max_premium"]):
            continue
        rank = (abs(n - S["otm_steps"]), n)
        if best is None or rank < best[0]:
            best = (rank, K, px)
    return (best[1], best[2]) if best else None


def size_lots(capital, risk_pct, price, sl_pct, lot, max_lots=10, max_capital_pct=40.0):
    """Lots such that a full stop-loss costs <= risk_pct of capital. 0 means 'skip the trade'."""
    risk_amt = capital * risk_pct / 100
    per_lot_risk = price * sl_pct / 100 * lot
    if per_lot_risk <= 0:
        return 0
    lots = int(risk_amt // per_lot_risk)
    lots = min(lots, max_lots, int((capital * max_capital_pct / 100) // (price * lot)))
    return max(lots, 0)


# =====================================================================================
# Live / paper bot (reuses gamma_blast's Broker, SymbolMaster, enter, SL placement and exits)
# =====================================================================================
class ExpiryBlast(g.GammaBlast):
    def __init__(self, cfg):                                   # deliberately not calling the parent __init__
        self.cfg = cfg
        self.paper = cfg["paper_trading"]
        self.S = compile_strategy(cfg["strategy"])
        self.broker = g.Broker(cfg)
        self.master = g.SymbolMaster()
        self.master.load({k: v for k, v in cfg["indices"].items() if v["enabled"]})
        self.positions = {}
        self.trades_today = {k: 0 for k in cfg["indices"]}
        self.last_loss_time = {}
        self.realised = 0.0
        self.last_scan_min = {}
        self._load_state()

    # ---------------- persistence (own files) ----------------
    def _save_state(self):
        data = {"date": g.now_ist().strftime("%Y-%m-%d"), "realised": self.realised,
                "trades_today": self.trades_today,
                "positions": {k: asdict(v) for k, v in self.positions.items()}}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        data = json.load(open(STATE_FILE))
        if data.get("date") != g.now_ist().strftime("%Y-%m-%d"):
            return
        self.realised = data.get("realised", 0.0)
        self.trades_today.update(data.get("trades_today", {}))
        self.positions = {k: g.Position(**v) for k, v in data.get("positions", {}).items()}
        log.info("Restored state: %d open position(s), realised %.0f", len(self.positions), self.realised)

    def _log_trade(self, p, exit_px, reason):
        new = not os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "index", "symbol", "type", "qty", "entry_time", "entry",
                            "exit_time", "exit", "pnl", "pnl_pct", "reason", "mode", "meta"])
            w.writerow([g.now_ist().date(), p.index, p.symbol, p.opt_type, p.qty, p.entry_time, p.entry,
                        g.now_ist().strftime("%H:%M:%S"), exit_px, round((exit_px - p.entry) * p.qty, 2),
                        round((exit_px / p.entry - 1) * 100, 1), reason,
                        "PAPER" if self.paper else "LIVE", json.dumps(p.meta)])

    # ---------------- sizing / guards ----------------
    def size(self, name, price, lot):
        c = self.cfg
        return size_lots(c["capital"], c["risk_per_trade_pct"], price, c["stop_loss_pct"], lot,
                         c["indices"][name]["max_lots"], c["max_capital_per_trade_pct"])

    def can_enter(self, name, now, expiry=None):
        c = self.cfg
        if not c["indices"][name]["enabled"] or name in self.positions:
            return False
        if len(self.positions) >= c["max_open_positions"]:
            return False
        if self.trades_today.get(name, 0) >= c["max_trades_per_index"]:
            return False
        m = now.hour * 60 + now.minute
        return self.S["win_start"] <= m <= min(self.S["win_end"], _m(c["entry_end"]))

    # ---------------- scan & enter ----------------
    def scan(self, name, now):
        expiry = self.master.nearest_expiry(name, now)
        if not expiry or expiry.date() != now.date():          # EXPIRY DAY ONLY
            return
        if not self.can_enter(name, now, expiry):
            return
        ic = self.cfg["indices"][name]
        candles = self._closed(self.broker.candles_today(ic["spot"]))
        if not candles or now.timestamp() - candles[-1][0] > 150:
            return                                              # stale data - never trade on it
        f = day_features(candles, self.S)
        side = signal(f, len(candles) - 1, self.S)
        if not side:
            return
        spot = self.broker.ltp([ic["spot"]]).get(ic["spot"], candles[-1][4])
        chain = {o.strike: o for o in self.master.chain(name, expiry) if o.opt_type == side}
        atm = round(spot / ic["strike_step"]) * ic["strike_step"]
        sign = 1 if side == "CE" else -1
        want = [atm + sign * n * ic["strike_step"] for n in range(self.S["max_otm_steps"] + 1)]
        rows = {k: chain[k] for k in want if k in chain}
        prices = self.broker.ltp([r.ticker for r in rows.values()])
        pick = pick_strike(spot, side, ic["strike_step"],
                           lambda K: prices.get(rows[K].ticker) if K in rows else None, self.S)
        if not pick:
            log.info("%s %s: no strike with premium in [%s, %s] - skip", name, side,
                     self.S["min_premium"], self.S["max_premium"])
            return
        K, ltp = pick
        row = rows[K]
        fill_px = ltp * (1 + self.cfg["paper_slippage_pct"] / 100) if self.paper else ltp
        self.enter(name, side, row, g.round_tick(fill_px, row.tick),
                   {"mode": self.S["mode"], "spot": spot, "ltp_seen": ltp, "expiry": expiry.strftime("%Y-%m-%d")})

    # ---------------- manage (-25% SL, +100% target, optional breakeven) ----------------
    def manage(self):
        if not self.positions:
            return
        c = self.cfg
        slip = c["paper_slippage_pct"] / 100 if self.paper else 0.0
        prices = self.broker.ltp([p.symbol for p in self.positions.values()])
        for name, p in list(self.positions.items()):
            ltp = prices.get(p.symbol)
            if ltp is None:
                continue
            if not self.paper and p.sl_order_id:
                st, px = self.broker.status(p.sl_order_id)
                if st == 2:
                    self._close(name, px or p.sl, "BE" if p.trail_on else "SL", broker_done=True)
                    continue
                if st in (1, 5, 7):
                    log.warning("%s broker SL inactive (status %s) - re-placing", p.symbol, st)
                    p.sl_order_id = self._place_sl(p)

            p.peak = max(p.peak, ltp)

            # optional breakeven: +60% reached -> stop moves to entry (cost)
            if c["breakeven_enabled"] and not p.trail_on and \
                    (p.peak / p.entry - 1) * 100 >= c["breakeven_trigger_pct"]:
                p.trail_on = True
                p.sl = g.round_tick(p.entry, p.tick)
                if not self.paper and p.sl_order_id:
                    lim = g.round_tick(p.sl * (1 - c["sl_limit_buffer_pct"] / 100), p.tick)
                    if not self.broker.modify_sl(p.sl_order_id, p.qty, p.sl, lim):
                        log.warning("SL modify failed for %s", p.symbol)
                log.info("BREAKEVEN %s peak %.2f -> SL %.2f", p.symbol, p.peak, p.sl)
                self._save_state()

            if ltp >= p.target:
                self._close(name, ltp * (1 - slip), "TARGET")
                continue
            if self.paper or not p.sl_order_id:
                if ltp <= p.sl:
                    self._close(name, ltp * (1 - slip), "BE" if p.trail_on else "SL")
            elif ltp <= p.sl * (1 - c["gap_fallback_pct"] / 100):
                log.warning("%s gapped through SL band - forcing exit", p.symbol)
                self._close(name, ltp, "SL_GAP")

    def flatten(self, reason):
        slip = self.cfg["paper_slippage_pct"] / 100 if self.paper else 0.0
        prices = self.broker.ltp([p.symbol for p in self.positions.values()]) if self.positions else {}
        for name in list(self.positions):
            p = self.positions[name]
            self._close(name, prices.get(p.symbol, p.entry) * (1 - slip), reason)

    # ---------------- main loop ----------------
    def run(self):
        c = self.cfg
        now = g.now_ist()
        today_expiries = {n: e.date() for n in c["indices"] if c["indices"][n]["enabled"]
                          for e in [self.master.nearest_expiry(n, now)] if e}
        active = [n for n, d in today_expiries.items() if d == now.date()]
        log.info("Expiry Blast | mode=%s | strategy=%s | expiry today: %s", "PAPER" if self.paper else "LIVE",
                 c["strategy"]["mode"], active or "NONE")
        if not active and not self.positions:
            log.info("No index expires today (%s) - nothing to do.", today_expiries)
            return
        while True:
            now = g.now_ist()
            if now.weekday() >= 5:
                log.info("Weekend - exiting"); return
            if now.time() < dt.time(9, 15):
                time.sleep(20); continue
            if os.path.exists(KILL_FILE):
                log.warning("KILL file found - squaring off"); self.flatten("KILL"); return
            if now.time() >= g.hhmm(c["square_off"]):
                self.flatten("SQUARE_OFF")
                log.info("Day done | realised P&L %.0f", self.realised); return
            self.manage()
            minute_key = now.strftime("%H:%M")
            if now.second >= 3:                                  # let the 1-min candle close
                for name in active:
                    if self.last_scan_min.get(name) != minute_key:
                        self.last_scan_min[name] = minute_key
                        try:
                            self.scan(name, now)
                        except Exception as e:
                            log.exception("scan error %s: %s", name, e)
            time.sleep(c["manage_interval_sec"])


if __name__ == "__main__":
    ExpiryBlast(CONFIG).run()
