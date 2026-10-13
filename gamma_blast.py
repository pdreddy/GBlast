"""
GAMMA BLAST v2 - Hero-or-Zero index option BUYER for FYERS API v3
Indices: NIFTY, BANKNIFTY, SENSEX

Original aggressive spec (restored):
  * Premium stop loss ........ 40% below entry
  * Profit objective ......... +200% (premium triples)        -> risk:reward 1:5 (meets >= 1:4)
  * Trailing stop ............ activates once peak gain >= +50%
  * Trail distance ........... 25% below PEAK premium (only ever moves up)
  * Inputs ................... GAMMA (Black-Scholes, from live IV), MOMENTUM (index breakout),
                               EXPIRY BEHAVIOUR (DTE-aware strike selection & entry windows)

How it decides a trade
  1. Momentum (1-min index candles, closed candles only):
       CALL: close > highest high of prior 10 bars AND > opening-range (09:15-09:29) high,
             EMA9 > EMA21, strong bullish body (>=60% of range), range expansion (>=1.2x ATR14),
             5-bar rate of change >= +0.10%.            PUT = exact mirror.
  2. Gamma blast filter: for ATM / OTM strikes of the NEAREST expiry, the bot backs out IV from the
     live premium, computes delta/gamma/theta and estimates the % premium gain for a
     `blast_move_pct` move in the index (delta*dS + 0.5*gamma*dS^2 - theta over the hold).
     It buys the strike with the highest expected % gain, and only if that gain >= `min_blast_gain_pct`.
     This automatically favours expiry-day / near-expiry contracts (max gamma) - the essence of hero-or-zero.
  3. Option confirmation: the chosen option's LTP must be above the high of its last 3 closed 1-min bars.
  4. Size from risk: lots = (capital * risk%) / (entry * 40% * lot size), capped.

Order safety (LIVE mode)
  * Entry: MARKET buy. Immediately a broker-side SL-LIMIT sell is placed at -40% so the stop
    survives a crash / disconnect of this script.
  * Trailing stop = modifying that broker SL order upward.
  * Target / square-off: cancel the SL order FIRST, confirm, then sell -> no accidental double sell.
  * If price gaps through the SL-limit band and the order is stuck, the bot cancels and market-sells.

PAPER mode is ON by default. Run it for several weeks before setting paper_trading = False.
Kill switch: create a file named KILL in this folder -> bot squares off everything and stops.

Requires:  pip install fyers-apiv3
Daily: double-click start.bat (Windows) or run ./start.sh (Mac/Linux) - it logs in, then starts the bot.
"""

import csv
import io
import json
import logging
import math
import os
import time
import urllib.request
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Optional
from zoneinfo import ZoneInfo

from config import CLIENT_ID

IST = ZoneInfo("Asia/Kolkata")

# =====================================================================================
# CONFIG - edit here
# =====================================================================================
CONFIG = {
    "client_id": CLIENT_ID,                 # from .env
    "access_token_file": "access_token.txt",
    "paper_trading": True,                  # <-- keep True until you have paper results

    # ---- money management ----
    "capital": 200000,                      # trading capital in INR
    "risk_per_trade_pct": 1.0,              # INR lost if the 40% stop is hit = 1% of capital
    "max_capital_per_trade_pct": 40.0,      # premium outlay cap per trade
    "max_daily_loss_pct": 3.0,              # stop opening new trades after this realised loss
    "max_open_positions": 2,                # across all indices
    "max_trades_per_index": 2,              # per day
    "loss_cooldown_min": 15,                # wait after a losing trade on the same index

    # ---- Gamma Blast exits (your original spec) ----
    "stop_loss_pct": 40.0,
    "target_pct": 200.0,
    "trail_trigger_pct": 50.0,
    "trail_gap_pct": 25.0,
    "trail_min_step_pct": 2.0,              # only modify broker SL if it rises >= 2% of entry
    "sl_limit_buffer_pct": 5.0,             # SL-L limit price this % below the trigger
    "gap_fallback_pct": 8.0,                # if LTP is this % below SL and SL is still pending -> force exit
    "no_progress_exit_min": 0,              # 0 = OFF. e.g. 25 -> exit if not +15% after 25 minutes
    "no_progress_min_gain_pct": 15.0,

    # ---- timing (IST) ----
    "entry_start": "09:30",
    "entry_end": "14:30",
    "expiry_day_entry_end": "14:45",        # gamma is highest late on expiry day
    "square_off": "15:15",
    "manage_interval_sec": 2,

    # ---- momentum ----
    "breakout_lookback": 10,
    "ema_fast": 9,
    "ema_slow": 21,
    "atr_period": 14,
    "atr_expansion_mult": 1.2,
    "min_body_ratio": 0.6,
    "roc_bars": 5,
    "min_roc_pct": 0.10,
    "option_confirm_bars": 3,               # 0 disables premium confirmation

    # ---- gamma model ----
    "risk_free_rate": 0.065,
    "blast_move_pct": 0.35,                 # index move we are betting on (0.35% ~ 90 NIFTY pts)
    "expected_hold_min": 30,
    "min_abs_delta": 0.25,                  # avoid far-OTM lottery tickets

    # ---- instruments ----
    "indices": {
        "NIFTY": {
            "enabled": True,
            "spot": "NSE:NIFTY50-INDEX",
            "master": "NSE_FO",
            "underlying": "NIFTY",
            "min_premium": 15,
            "max_lots": 10,
            "min_blast_gain_pct": 35,
        },
        "BANKNIFTY": {
            # Only MONTHLY options exist for BANKNIFTY, so gamma is lower most of the month.
            # A lower threshold is used; it will still trade far less often than NIFTY/SENSEX.
            "enabled": True,
            "spot": "NSE:NIFTYBANK-INDEX",
            "master": "NSE_FO",
            "underlying": "BANKNIFTY",
            "min_premium": 40,
            "max_lots": 10,
            "min_blast_gain_pct": 20,
        },
        "SENSEX": {
            "enabled": True,
            "spot": "BSE:SENSEX-INDEX",
            "master": "BSE_FO",
            "underlying": "SENSEX",
            "min_premium": 40,
            "max_lots": 10,
            "min_blast_gain_pct": 35,
        },
    },
}

MASTER_URLS = {
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "BSE_FO": "https://public.fyers.in/sym_details/BSE_FO.csv",
}

STATE_FILE = "gamma_blast_state.json"
TRADE_LOG = "gamma_blast_trades.csv"
KILL_FILE = "KILL"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("gamma_blast.log"), logging.StreamHandler()],
)
log = logging.getLogger("gamma_blast")


# =====================================================================================
# Helpers
# =====================================================================================
def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def ema(values, period):
    k = 2 / (period + 1)
    out, e = [], values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


# ---------------- Black-Scholes ----------------
def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _n(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(S, K, T, r, sig, cp):
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if cp == "CE":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)


def implied_vol(price, S, K, T, r, cp):
    intrinsic = max(0.0, S - K) if cp == "CE" else max(0.0, K - S)
    if price <= intrinsic + 0.05:
        return None
    lo, hi = 0.01, 4.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, cp) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def greeks(S, K, T, r, sig, cp):
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    gamma = _n(d1) / (S * sq)
    if cp == "CE":
        delta = _N(d1)
        theta = -S * _n(d1) * sig / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _N(d2)
    else:
        delta = _N(d1) - 1
        theta = -S * _n(d1) * sig / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _N(-d2)
    return delta, gamma, theta  # theta per year


# =====================================================================================
# Symbol master (expiries, strikes, lot sizes - never hard-coded)
# =====================================================================================
@dataclass
class OptionRow:
    ticker: str
    strike: float
    opt_type: str
    expiry: dt.datetime
    lot: int
    tick: float


class SymbolMaster:
    # FYERS master CSV columns (no header): 0 fytoken, 1 description, 2 instr type, 3 lot size,
    # 4 tick size, 5 ISIN, 6 session, 7 last update, 8 expiry epoch, 9 symbol ticker, 10 exchange,
    # 11 segment, 12 scrip code, 13 underlying symbol, 14 underlying scrip code, 15 strike, 16 option type
    def __init__(self):
        self.options: dict[str, list[OptionRow]] = {}

    def load(self, indices: dict):
        cache = {}
        for name, ic in indices.items():
            if not ic["enabled"]:
                continue
            m = ic["master"]
            if m not in cache:
                log.info("Downloading symbol master %s", m)
                with urllib.request.urlopen(MASTER_URLS[m], timeout=60) as resp:
                    cache[m] = list(csv.reader(io.StringIO(resp.read().decode("utf-8"))))
            rows = []
            for r in cache[m]:
                try:
                    if r[13].strip().upper() != ic["underlying"] or r[16].strip() not in ("CE", "PE"):
                        continue
                    rows.append(OptionRow(
                        ticker=r[9].strip(),
                        strike=float(r[15]),
                        opt_type=r[16].strip(),
                        expiry=dt.datetime.fromtimestamp(int(float(r[8])), IST).replace(hour=15, minute=30),
                        lot=int(float(r[3])),
                        tick=float(r[4]) or 0.05,
                    ))
                except (IndexError, ValueError):
                    continue
            if not rows:
                sample = sorted({r[13] for r in cache[m] if len(r) > 13})[:40]
                raise RuntimeError(f"No options found for {name} ({ic['underlying']}). "
                                   f"Underlyings seen in {m}: {sample}")
            self.options[name] = rows
            log.info("%s: %d option contracts, lot size %d", name, len(rows), rows[0].lot)

    def nearest_expiry(self, name, now):
        exps = sorted({o.expiry for o in self.options[name] if o.expiry > now})
        return exps[0] if exps else None

    def chain(self, name, expiry):
        return [o for o in self.options[name] if o.expiry == expiry]


# =====================================================================================
# Broker wrapper (FYERS API v3)
# =====================================================================================
class Broker:
    # FYERS order status: 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending, 7 expired
    def __init__(self, cfg):
        from fyers_apiv3 import fyersModel
        token = open(cfg["access_token_file"]).read().strip()
        self.f = fyersModel.FyersModel(client_id=cfg["client_id"], token=token,
                                       is_async=False, log_path="")
        self._last = 0.0

    def _call(self, fn, data=None, retries=3):
        for attempt in range(retries):
            gap = time.time() - self._last
            if gap < 0.15:                      # stay well under 10 req/sec
                time.sleep(0.15 - gap)
            self._last = time.time()
            try:
                resp = fn(data=data) if data is not None else fn()
                if isinstance(resp, dict) and resp.get("s") == "error" and attempt < retries - 1:
                    log.warning("API error %s - retrying", resp.get("message"))
                    time.sleep(1)
                    continue
                return resp
            except Exception as e:
                log.warning("API exception %s (attempt %d)", e, attempt + 1)
                time.sleep(1 + attempt)
        return {"s": "error", "message": "retries exhausted"}

    def ltp(self, symbols):
        out = {}
        for i in range(0, len(symbols), 50):
            resp = self._call(self.f.quotes, {"symbols": ",".join(symbols[i:i + 50])})
            for d in resp.get("d", []) or []:
                lp = (d.get("v") or {}).get("lp")
                if lp:
                    out[d["n"]] = float(lp)
        return out

    def candles_today(self, symbol):
        today = now_ist().strftime("%Y-%m-%d")
        resp = self._call(self.f.history, {
            "symbol": symbol, "resolution": "1", "date_format": "1",
            "range_from": today, "range_to": today, "cont_flag": "1"})
        return resp.get("candles", []) or []

    def candles_range(self, symbol, days_back=0):
        """1-minute candles for today and the previous `days_back` calendar days."""
        today = now_ist().date()
        resp = self._call(self.f.history, {
            "symbol": symbol, "resolution": "1", "date_format": "1",
            "range_from": (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d"),
            "range_to": today.strftime("%Y-%m-%d"), "cont_flag": "1"})
        return resp.get("candles", []) or []

    def place(self, symbol, qty, side, otype=2, limit=0.0, stop=0.0):
        resp = self._call(self.f.place_order, {
            "symbol": symbol, "qty": int(qty), "type": otype, "side": side,
            "productType": "INTRADAY", "limitPrice": limit, "stopPrice": stop,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False})
        if resp.get("s") != "ok":
            log.error("Order rejected %s: %s", symbol, resp)
            return None
        return resp.get("id")

    def modify_sl(self, oid, qty, stop, limit):
        resp = self._call(self.f.modify_order, {
            "id": oid, "type": 4, "qty": int(qty), "stopPrice": stop, "limitPrice": limit})
        return resp.get("s") == "ok"

    def cancel(self, oid):
        return self._call(self.f.cancel_order, {"id": oid}).get("s") == "ok"

    def status(self, oid):
        resp = self._call(self.f.orderbook, {"id": oid})
        ob = resp.get("orderBook") or []
        if not ob:
            return None, 0.0
        o = ob[0]
        return int(o.get("status", 0)), float(o.get("tradedPrice") or 0)

    def wait_fill(self, oid, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            st, px = self.status(oid)
            if st == 2:
                return px
            if st in (1, 5, 7):
                return None
            time.sleep(0.5)
        return None


# =====================================================================================
# Strategy
# =====================================================================================
@dataclass
class Position:
    index: str
    symbol: str
    opt_type: str
    qty: int
    entry: float
    sl: float
    target: float
    peak: float
    tick: float
    entry_time: str
    trail_on: bool = False
    sl_order_id: Optional[str] = None
    meta: dict = field(default_factory=dict)


class GammaBlast:
    def __init__(self, cfg):
        self.cfg = cfg
        self.paper = cfg["paper_trading"]
        self.broker = Broker(cfg)
        self.master = SymbolMaster()
        self.master.load(cfg["indices"])
        self.positions: dict[str, Position] = {}
        self.trades_today = {k: 0 for k in cfg["indices"]}
        self.last_loss_time: dict[str, dt.datetime] = {}
        self.realised = 0.0
        self.last_scan_min: dict[str, str] = {}
        self._load_state()

    # ---------------- persistence ----------------
    def _save_state(self):
        data = {"date": now_ist().strftime("%Y-%m-%d"), "realised": self.realised,
                "trades_today": self.trades_today,
                "positions": {k: asdict(v) for k, v in self.positions.items()}}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        data = json.load(open(STATE_FILE))
        if data.get("date") != now_ist().strftime("%Y-%m-%d"):
            return
        self.realised = data.get("realised", 0.0)
        self.trades_today.update(data.get("trades_today", {}))
        self.positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        log.info("Restored state: %d open position(s), realised %.0f", len(self.positions), self.realised)

    def _log_trade(self, p: Position, exit_px, reason):
        new = not os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "index", "symbol", "type", "qty", "entry_time", "entry",
                            "exit_time", "exit", "pnl", "pnl_pct", "reason", "mode", "meta"])
            pnl = (exit_px - p.entry) * p.qty
            w.writerow([now_ist().date(), p.index, p.symbol, p.opt_type, p.qty, p.entry_time,
                        p.entry, now_ist().strftime("%H:%M:%S"), exit_px, round(pnl, 2),
                        round((exit_px / p.entry - 1) * 100, 1), reason,
                        "PAPER" if self.paper else "LIVE", json.dumps(p.meta)])

    # ---------------- momentum ----------------
    def momentum_signal(self, candles):
        c = self.cfg
        lb = c["breakout_lookback"]
        if len(candles) < max(c["ema_slow"], lb, c["atr_period"]) + 2:
            return None, {}
        ts = [dt.datetime.fromtimestamp(x[0], IST) for x in candles]
        o = [x[1] for x in candles]; h = [x[2] for x in candles]
        l = [x[3] for x in candles]; cl = [x[4] for x in candles]

        orb = [i for i, t in enumerate(ts) if t.time() < dt.time(9, 30)]
        if not orb:
            return None, {}
        orb_hi = max(h[i] for i in orb); orb_lo = min(l[i] for i in orb)

        e_fast = ema(cl, c["ema_fast"])[-1]; e_slow = ema(cl, c["ema_slow"])[-1]
        trs = [max(h[i] - l[i], abs(h[i] - cl[i - 1]), abs(l[i] - cl[i - 1])) for i in range(1, len(cl) - 1)]
        atr = sum(trs[-c["atr_period"]:]) / c["atr_period"]
        rng = h[-1] - l[-1]
        body = abs(cl[-1] - o[-1])
        roc = (cl[-1] / cl[-1 - c["roc_bars"]] - 1) * 100
        prior_hi = max(h[-1 - lb:-1]); prior_lo = min(l[-1 - lb:-1])
        strong = rng > 0 and body >= c["min_body_ratio"] * rng and rng >= c["atr_expansion_mult"] * atr

        info = {"close": cl[-1], "orb_hi": orb_hi, "orb_lo": orb_lo, "ema_f": round(e_fast, 2),
                "ema_s": round(e_slow, 2), "atr": round(atr, 2), "roc": round(roc, 3)}
        if strong and cl[-1] > o[-1] and cl[-1] > prior_hi and cl[-1] > orb_hi \
                and e_fast > e_slow and roc >= c["min_roc_pct"]:
            return "CE", info
        if strong and cl[-1] < o[-1] and cl[-1] < prior_lo and cl[-1] < orb_lo \
                and e_fast < e_slow and roc <= -c["min_roc_pct"]:
            return "PE", info
        return None, info

    # ---------------- gamma strike selection ----------------
    def pick_strike(self, name, opt_type, spot, expiry, now):
        c = self.cfg
        ic = c["indices"][name]
        chain = [o for o in self.master.chain(name, expiry) if o.opt_type == opt_type]
        strikes = sorted({o.strike for o in chain})
        if not strikes:
            return None
        atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        T = max((expiry - now).total_seconds(), 600) / (365 * 24 * 3600)
        n_otm = 2 if (expiry - now) < dt.timedelta(days=1) else 1   # expiry behaviour
        idxs = [atm_i + k for k in range(0, n_otm + 1)] if opt_type == "CE" \
            else [atm_i - k for k in range(0, n_otm + 1)]
        picks = [strikes[i] for i in idxs if 0 <= i < len(strikes)]
        rows = {o.strike: o for o in chain if o.strike in picks}
        prices = self.broker.ltp([r.ticker for r in rows.values()])

        dS = spot * c["blast_move_pct"] / 100
        hold_T = c["expected_hold_min"] / (365 * 24 * 60)
        best = None
        for k, row in rows.items():
            px = prices.get(row.ticker)
            if not px or px < ic["min_premium"]:
                continue
            iv = implied_vol(px, spot, k, T, c["risk_free_rate"], opt_type)
            if not iv:
                continue
            delta, gamma, theta = greeks(spot, k, T, c["risk_free_rate"], iv, opt_type)
            if abs(delta) < c["min_abs_delta"]:
                continue
            gain = abs(delta) * dS + 0.5 * gamma * dS * dS + theta * hold_T  # theta is negative
            gain_pct = gain / px * 100
            cand = {"row": row, "ltp": px, "iv": round(iv * 100, 1), "delta": round(delta, 3),
                    "gamma": round(gamma, 6), "blast_gain_pct": round(gain_pct, 1)}
            if best is None or gain_pct > best["blast_gain_pct"]:
                best = cand
        if best and best["blast_gain_pct"] >= ic["min_blast_gain_pct"]:
            return best
        if best:
            log.info("%s %s: best blast gain %.1f%% < %s%% threshold - skip",
                     name, opt_type, best["blast_gain_pct"], ic["min_blast_gain_pct"])
        return None

    def option_confirms(self, symbol, ltp):
        n = self.cfg["option_confirm_bars"]
        if n <= 0:
            return True
        cs = self._closed(self.broker.candles_today(symbol))
        if len(cs) < n:
            return True
        return ltp > max(x[2] for x in cs[-n:])

    @staticmethod
    def _closed(candles):
        if not candles:
            return candles
        cur_min = now_ist().replace(second=0, microsecond=0)
        if dt.datetime.fromtimestamp(candles[-1][0], IST) >= cur_min:
            return candles[:-1]
        return candles

    # ---------------- guards ----------------
    def can_enter(self, name, now, expiry):
        c = self.cfg
        if not c["indices"][name]["enabled"] or name in self.positions:
            return False
        if len(self.positions) >= c["max_open_positions"]:
            return False
        if self.trades_today.get(name, 0) >= c["max_trades_per_index"]:
            return False
        if self.realised <= -c["capital"] * c["max_daily_loss_pct"] / 100:
            return False
        lt = self.last_loss_time.get(name)
        if lt and now - lt < dt.timedelta(minutes=c["loss_cooldown_min"]):
            return False
        end = c["expiry_day_entry_end"] if expiry and expiry.date() == now.date() else c["entry_end"]
        return hhmm(c["entry_start"]) <= now.time() <= hhmm(end)

    # ---------------- scan & enter ----------------
    def scan(self, name, now):
        expiry = self.master.nearest_expiry(name, now)
        if not expiry or not self.can_enter(name, now, expiry):
            return
        ic = self.cfg["indices"][name]
        candles = self._closed(self.broker.candles_today(ic["spot"]))
        side, info = self.momentum_signal(candles)
        if not side:
            return
        spot = self.broker.ltp([ic["spot"]]).get(ic["spot"], info["close"])
        pick = self.pick_strike(name, side, spot, expiry, now)
        if not pick:
            return
        row, ltp = pick["row"], pick["ltp"]
        if not self.option_confirms(row.ticker, ltp):
            log.info("%s %s: premium not confirming breakout - skip", name, row.ticker)
            return
        self.enter(name, side, row, ltp, {**info, **{k: v for k, v in pick.items() if k != "row"},
                                          "expiry": expiry.strftime("%Y-%m-%d"), "spot": spot})

    def size(self, name, price, lot):
        c = self.cfg
        risk_amt = c["capital"] * c["risk_per_trade_pct"] / 100
        lots = int(risk_amt // (price * c["stop_loss_pct"] / 100 * lot))
        lots = min(lots, c["indices"][name]["max_lots"],
                   int((c["capital"] * c["max_capital_per_trade_pct"] / 100) // (price * lot)))
        return max(lots, 0)

    def enter(self, name, side, row, ltp, meta):
        c = self.cfg
        lots = self.size(name, ltp, row.lot)
        if lots < 1:
            log.info("%s: 1 lot of %s at %.2f exceeds risk budget - skip", name, row.ticker, ltp)
            return
        qty = lots * row.lot
        if self.paper:
            fill = ltp
        else:
            oid = self.broker.place(row.ticker, qty, side=1, otype=2)
            fill = self.broker.wait_fill(oid) if oid else None
            if not fill:
                log.error("%s: entry not filled - abort", row.ticker)
                return
        sl = round_tick(fill * (1 - c["stop_loss_pct"] / 100), row.tick)
        tgt = round_tick(fill * (1 + c["target_pct"] / 100), row.tick)
        p = Position(name, row.ticker, side, qty, fill, sl, tgt, fill, row.tick,
                     now_ist().strftime("%H:%M:%S"), meta=meta)
        if not self.paper:
            p.sl_order_id = self._place_sl(p)
        self.positions[name] = p
        self.trades_today[name] = self.trades_today.get(name, 0) + 1
        self._save_state()
        log.info("ENTRY %s %s x%d @ %.2f | SL %.2f | TGT %.2f | %s",
                 name, row.ticker, qty, fill, sl, tgt, meta)

    def _place_sl(self, p: Position):
        limit = round_tick(p.sl * (1 - self.cfg["sl_limit_buffer_pct"] / 100), p.tick)
        oid = self.broker.place(p.symbol, p.qty, side=-1, otype=4, limit=limit, stop=p.sl)
        if not oid:
            log.error("Broker SL failed for %s - software stop will be used", p.symbol)
        return oid

    # ---------------- manage ----------------
    def manage(self):
        if not self.positions:
            return
        c = self.cfg
        prices = self.broker.ltp([p.symbol for p in self.positions.values()])
        for name, p in list(self.positions.items()):
            ltp = prices.get(p.symbol)
            if ltp is None:
                continue

            # live: has the broker stop already executed?
            if not self.paper and p.sl_order_id:
                st, px = self.broker.status(p.sl_order_id)
                if st == 2:
                    self._close(name, px or p.sl, "TRAIL" if p.trail_on else "SL", broker_done=True)
                    continue
                if st in (1, 5, 7):
                    log.warning("%s broker SL inactive (status %s) - re-placing", p.symbol, st)
                    p.sl_order_id = self._place_sl(p)

            p.peak = max(p.peak, ltp)
            peak_gain = (p.peak / p.entry - 1) * 100

            # trailing stop: after +50%, stop = 25% below peak premium
            if peak_gain >= c["trail_trigger_pct"]:
                new_sl = round_tick(p.peak * (1 - c["trail_gap_pct"] / 100), p.tick)
                if new_sl - p.sl >= p.entry * c["trail_min_step_pct"] / 100:
                    if not p.trail_on:
                        log.info("TRAIL ON %s peak %.2f (+%.0f%%)", p.symbol, p.peak, peak_gain)
                    p.trail_on = True
                    p.sl = new_sl
                    if not self.paper and p.sl_order_id:
                        lim = round_tick(new_sl * (1 - c["sl_limit_buffer_pct"] / 100), p.tick)
                        if not self.broker.modify_sl(p.sl_order_id, p.qty, new_sl, lim):
                            log.warning("SL modify failed for %s", p.symbol)
                    log.info("SL -> %.2f on %s", p.sl, p.symbol)
                    self._save_state()

            if ltp >= p.target:
                self._close(name, ltp, "TARGET")
                continue

            # stop handling
            if self.paper or not p.sl_order_id:
                if ltp <= p.sl:
                    self._close(name, ltp, "TRAIL" if p.trail_on else "SL")
                    continue
            elif ltp <= p.sl * (1 - c["gap_fallback_pct"] / 100):
                log.warning("%s gapped through SL band - forcing exit", p.symbol)
                self._close(name, ltp, "SL_GAP")
                continue

            if c["no_progress_exit_min"] > 0:
                held = now_ist() - dt.datetime.combine(now_ist().date(), hhmm(p.entry_time[:5]), IST)
                if held >= dt.timedelta(minutes=c["no_progress_exit_min"]) \
                        and (ltp / p.entry - 1) * 100 < c["no_progress_min_gain_pct"] and not p.trail_on:
                    self._close(name, ltp, "NO_PROGRESS")

    def _close(self, name, px, reason, broker_done=False):
        p = self.positions[name]
        exit_px = px
        if not self.paper and not broker_done:
            if p.sl_order_id:
                self.broker.cancel(p.sl_order_id)
                time.sleep(0.5)
                st, spx = self.broker.status(p.sl_order_id)
                if st == 2:                      # stop filled while we were cancelling
                    exit_px, reason = spx or p.sl, "SL_RACE"
                    broker_done = True
            if not broker_done:
                oid = self.broker.place(p.symbol, p.qty, side=-1, otype=2)
                fill = self.broker.wait_fill(oid) if oid else None
                if fill is None:
                    log.error("EXIT FAILED for %s - CHECK FYERS TERMINAL NOW", p.symbol)
                    return
                exit_px = fill
        pnl = (exit_px - p.entry) * p.qty
        self.realised += pnl
        if pnl < 0:
            self.last_loss_time[name] = now_ist()
        self._log_trade(p, exit_px, reason)
        del self.positions[name]
        self._save_state()
        log.info("EXIT %s %s @ %.2f | %s | P&L %.0f (%.1f%%) | day %.0f",
                 name, p.symbol, exit_px, reason, pnl, (exit_px / p.entry - 1) * 100, self.realised)

    def flatten(self, reason):
        prices = self.broker.ltp([p.symbol for p in self.positions.values()]) if self.positions else {}
        for name in list(self.positions):
            self._close(name, prices.get(self.positions[name].symbol, self.positions[name].entry), reason)

    # ---------------- main loop ----------------
    def run(self):
        c = self.cfg
        log.info("Gamma Blast started | mode=%s | indices=%s", "PAPER" if self.paper else "LIVE",
                 [k for k, v in c["indices"].items() if v["enabled"]])
        while True:
            now = now_ist()
            if now.weekday() >= 5:
                log.info("Weekend - exiting"); return
            if now.time() < dt.time(9, 15):
                time.sleep(20); continue
            if os.path.exists(KILL_FILE):
                log.warning("KILL file found - squaring off"); self.flatten("KILL"); return
            if now.time() >= hhmm(c["square_off"]):
                self.flatten("SQUARE_OFF")
                log.info("Day done | realised P&L %.0f", self.realised); return

            self.manage()

            minute_key = now.strftime("%H:%M")
            if now.second >= 3:                      # give the 1-min candle time to close
                for name in c["indices"]:
                    if self.last_scan_min.get(name) != minute_key:
                        self.last_scan_min[name] = minute_key
                        try:
                            self.scan(name, now)
                        except Exception as e:
                            log.exception("scan error %s: %s", name, e)
            time.sleep(c["manage_interval_sec"])


if __name__ == "__main__":
    GammaBlast(CONFIG).run()
