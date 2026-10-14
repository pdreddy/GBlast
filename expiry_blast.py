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
  ANCHOR time-anchored breakouts: 09:30 (break of the 09:15-09:30 range), 12:00 (break of 11:30-12:00),
         14:00 (break of 13:30-14:00); each allowed for a fixed span after its anchor. `anchors` lists which are on.
  ORB  opening-range breakout   : close crosses the 09:15-09:30 high/low
  EXT  day-extreme breakout     : close makes a new high/low of the day (use with an afternoon window)
  MOM  momentum candle          : body >= 60% of range AND range >= 1.5 x ATR14
  All modes also need: price on the right side of VWAP, EMA9 vs EMA21 in the trade direction, AND the
  5-minute, 15-minute and 1-hour charts trending the same way (EMA9 vs EMA21 of completed bars).

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
    "use_backtest_result": False,           # True = load strategy + breakeven from expiry_best.json (written by
                                            # expiry_backtest). Ignored if the backtest verdict was "NO EDGE".

    # ---- money management ----
    "capital": 200000,
    "risk_per_trade_pct": 1.0,              # INR lost if the 25% stop is hit = 1% of capital
    "max_capital_per_trade_pct": 40.0,      # premium outlay cap
    "max_open_positions": 2,                # 1 per index x 2 indices
    "max_trades_per_index": 2,              # per expiry day
    # ---- discipline guards (from the real-trade review: oversizing, no stops, revenge trading) ----
    "max_daily_loss_pct": 2.0,              # stop new entries for the day after losing this % of capital
    "max_consecutive_losses": 3,            # ...or after this many losing trades in a row
    "max_monthly_loss_pct": 6.0,            # stop trading for the rest of the month (paper or live) at this loss

    # ---- exits (strict 1:4) ----
    "stop_loss_pct": 25.0,
    "target_pct": 100.0,
    "breakeven_enabled": False,             # test with and without (the backtest does both)
    "breakeven_trigger_pct": 60.0,          # at +60% move the stop to entry
    # auto trailing stop: once the option is up trail_trigger_pct, the stop follows trail_gap_pct below the peak premium
    "trail_enabled": True,
    "trail_trigger_pct": 10.0,
    "trail_gap_pct": 15.0,
    "trail_min_step_pct": 1.0,              # only modify the broker SL if it rises >= 1% of entry
    # which days: "expiry" = expiry day only, "non_expiry" = other days (nearest weekly), "all" = both
    "trade_days": "all",
    # NON-expiry days are not hero-or-zero: tight stop (5% to 7.5% MAX), target 4x the stop (1:4), tighter trail
    "stop_loss_pct_ne": 7.5,                # must stay within [5.0, 7.5] - the bot refuses to start otherwise
    "target_pct_ne": 30.0,                  # 4 x 7.5
    "trail_gap_pct_ne": 7.5,
    "sl_limit_buffer_pct": 5.0,             # SL-L limit price this % below the trigger
    "gap_fallback_pct": 8.0,                # LTP this % below SL while SL still pending -> force exit
    "paper_slippage_pct": 2.0,              # paper fills are made worse by this % per side

    # ---- timing (IST) ----
    "entry_end": "14:45",
    "square_off": "15:15",
    "manage_interval_sec": 2,

    # ---- entry rules (see module docstring) ----
    "strategy": {
        "mode": "ANCHOR",                   # ANCHOR (time-anchored breakouts) | ORB | EXT | MOM
        "win_start": "09:30",               # ANCHOR mode uses its own windows below; this is the outer limit
        "win_end": "14:45",
        # time-anchored breakouts: (anchor time, minutes of range before it, minutes allowed to break after it)
        #   09:30 = break of the 09:15-09:30 range | 12:00 = break of 11:30-12:00 | 14:00 = break of 13:30-14:00
        "anchors": [("09:30", 15, 90), ("12:00", 30, 90), ("14:00", 30, 45)],
        "otm_steps": 1,                     # preferred strike: 0 = ATM, 1 or 2 = OTM
        "max_otm_steps": 2,
        "min_premium": 20,                  # expiry day premium band (Rs)
        "max_premium": 150,
        "min_premium_ne": 20,               # other days: options have more time value, so a wider band
        "max_premium_ne": 600,
        "ema_fast": 9, "ema_slow": 21,
        "atr_period": 14, "atr_mult": 1.5,  # MOM: range >= 1.5 x ATR14
        "body_ratio": 0.6,                  # MOM: body >= 60% of range
        "orb_end": "09:30",
        "warmup_bars": 22,
        # multi-timeframe filter: the 5m / 15m / 1h charts must ALL trend the same way as the trade
        # (EMA9 vs EMA21 on completed bars, built from 1-minute data across days). [] switches it off.
        "mtf_tfs": [5, 15, 60],
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

STOP_NE_RANGE = (5.0, 7.5)


def validate_config(cfg):
    lo, hi = STOP_NE_RANGE
    if not (lo <= cfg["stop_loss_pct_ne"] <= hi):
        raise ValueError(f"stop_loss_pct_ne must be between {lo} and {hi} (got {cfg['stop_loss_pct_ne']}): "
                         "non-expiry-day trades use a tight stop, max 7.5%.")
    return cfg


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
    out["anchors"] = [(_m(a), int(w), int(sp)) for a, w, sp in s.get("anchors", [])]
    return out


def _ema_series(vals, p):
    k, e, out = 2 / (p + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def htf_trend(candles, tfs=(5, 15, 60), fast=9, slow=21, min_bars=None):
    """Higher-timeframe trend from 1-minute candles (several days, session bars only, ascending).
    Buckets are aligned to the 09:15 open. Returns {tf: (nominal_end_ts list, sign list)} where sign is +1 if
    EMA(fast) > EMA(slow) of the bucket closes, -1 if below, 0 while there are too few bars (blocks trading)."""
    min_bars = min_bars or slow
    out = {}
    for tf in tfs:
        buckets, key = [], None
        for c in candles:
            t = c[0]
            m = ((t + 19800) % 86400) // 60
            k = (m - 555) // tf
            kk = ((t + 19800) // 86400, k)
            if kk != key:
                key = kk
                start = t - ((m - 555) - k * tf) * 60 - t % 60
                day_open = start - k * tf * 60
                buckets.append([min(start + tf * 60, day_open + 375 * 60), c[4]])
            else:
                buckets[-1][1] = c[4]                                  # close of the bucket so far
        closes = [b_[1] for b_ in buckets]
        ef, es = _ema_series(closes, fast), _ema_series(closes, slow)
        signs = [(1 if ef[i] > es[i] else -1 if ef[i] < es[i] else 0) if i + 1 >= min_bars else 0
                 for i in range(len(buckets))]
        out[tf] = ([b_[0] for b_ in buckets], signs)
    return out


def mtf_signs_at(trend, close_ts):
    """{tf: sign} using only higher-timeframe bars that were COMPLETE at close_ts (no look-ahead)."""
    import bisect
    res = {}
    for tf, (ends, signs) in trend.items():
        j = bisect.bisect_right(ends, close_ts) - 1
        res[tf] = signs[j] if j >= 0 else 0
    return res


def live_mtf(full_candles, close_ts, tfs):
    """Live helper: same maths as the backtest, on candles up to close_ts (drops the still-forming bucket)."""
    sub = [c for c in full_candles if c[0] < close_ts]
    return mtf_signs_at(htf_trend(sub, tfs), close_ts)


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

    anchor = {}
    for A, w, _sp in S.get("anchors", []):
        idx = [i for i in range(n) if A - w < mclose[i] <= A]
        anchor[A] = (max(h[i] for i in idx), min(l[i] for i in idx)) if len(idx) >= 0.8 * w else None

    orb_idx = [i for i in range(n) if mclose[i] <= S["orb_end"]]
    orb_hi = max(h[i] for i in orb_idx) if orb_idx else None
    orb_lo = min(l[i] for i in orb_idx) if orb_idx else None
    return {"n": n, "ts": ts, "o": o, "h": h, "l": l, "c": c_, "mclose": mclose, "atr": atr,
            "ema_f": ema(c_, S["ema_fast"]), "ema_s": ema(c_, S["ema_slow"]), "vwap": vwap,
            "hi_prior": hi_prior, "lo_prior": lo_prior, "orb_hi": orb_hi, "orb_lo": orb_lo,
            "orb_n": len(orb_idx), "anchor": anchor}


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
    tfs = S.get("mtf_tfs") or []
    if tfs:                                                    # 5m / 15m / 1h must all agree with the direction
        m = (f.get("mtf") or [None] * f["n"])[i]
        if not m:
            return None
        up = up and all(m.get(tf) == 1 for tf in tfs)
        dn = dn and all(m.get(tf) == -1 for tf in tfs)
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
    elif mode == "ANCHOR":
        pc = f["c"][i - 1]
        for A, _w, span in S["anchors"]:
            if A < t <= A + span and f["anchor"].get(A):
                hi, lo = f["anchor"][A]
                if up and c > hi and pc <= hi:
                    return "CE"
                if dn and c < lo and pc >= lo:
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


def apply_backtest_result(cfg, path="expiry_best.json"):
    """Optionally take the strategy chosen by expiry_backtest. Never adopts a result whose verdict is 'bad'."""
    if not cfg.get("use_backtest_result"):
        return cfg
    if not os.path.exists(path):
        log.warning("use_backtest_result is on but %s does not exist - run expiry_backtest first. Using CONFIG.", path)
        return cfg
    best = json.load(open(path))
    if best.get("verdict") == "bad":
        log.warning("Backtest verdict: %s - NOT adopting it. Using CONFIG.", best.get("headline"))
        return cfg
    log.info("Using backtest result '%s' (generated %s, verdict: %s)", best["name"], best["generated"], best["headline"])
    return {**cfg, "strategy": {**cfg["strategy"], **best["strategy"]}, "breakeven_enabled": best["breakeven"]}


# =====================================================================================
# Live / paper bot (reuses gamma_blast's Broker, SymbolMaster, enter, SL placement and exits)
# =====================================================================================
class ExpiryBlast(g.GammaBlast):
    def __init__(self, cfg):                                   # deliberately not calling the parent __init__
        self.cfg = cfg = validate_config(apply_backtest_result(cfg))
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
                "trades_today": self.trades_today, "loss_streak": getattr(self, "loss_streak", 0),
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
        self.loss_streak = data.get("loss_streak", 0)
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

    def month_pnl(self, now=None):
        """Realised P&L of the current calendar month from the trade log (includes today)."""
        month = (now or g.now_ist()).strftime("%Y-%m")
        if not os.path.exists(TRADE_LOG):
            return 0.0
        total = 0.0
        for row in csv.reader(open(TRADE_LOG)):
            if row and row[0].startswith(month):
                try:
                    total += float(row[9])
                except (ValueError, IndexError):
                    pass
        return total

    def halted(self, now=None):
        """Reason string if a discipline guard forbids new entries, else None."""
        c = self.cfg
        if self.realised <= -c["capital"] * c["max_daily_loss_pct"] / 100:
            return f"daily loss limit hit ({self.realised:,.0f})"
        if getattr(self, "loss_streak", 0) >= c["max_consecutive_losses"]:
            return f"{self.loss_streak} losses in a row"
        if self.month_pnl(now) <= -c["capital"] * c["max_monthly_loss_pct"] / 100:
            return f"monthly loss limit hit ({self.month_pnl(now):,.0f})"
        return None

    def _close(self, name, px, reason, broker_done=False):
        before = self.realised
        super()._close(name, px, reason, broker_done)
        if name not in self.positions:                           # really closed
            pnl = self.realised - before
            self.loss_streak = getattr(self, "loss_streak", 0) + 1 if pnl < 0 else 0
            self._save_state()

    def can_enter(self, name, now, expiry=None):
        c = self.cfg
        why = self.halted(now)
        if why:
            if getattr(self, "_halt_logged", None) != why:
                log.warning("TRADING HALTED for new entries: %s", why)
                self._halt_logged = why
            return False
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
        if not expiry:
            return
        is_exp = expiry.date() == now.date()
        td = self.cfg.get("trade_days", "expiry")
        if (td == "expiry" and not is_exp) or (td == "non_expiry" and is_exp):
            return
        if not self.can_enter(name, now, expiry):
            return
        S = self.S if is_exp else {**self.S, "min_premium": self.S.get("min_premium_ne", self.S["min_premium"]),
                                   "max_premium": self.S.get("max_premium_ne", self.S["max_premium"])}
        ic = self.cfg["indices"][name]
        full = self._closed(self.broker.candles_range(ic["spot"], 8 if self.S.get("mtf_tfs") else 0))
        today0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        candles = [c for c in full if c[0] >= today0]
        if not candles or now.timestamp() - candles[-1][0] > 150:
            return                                              # stale data - never trade on it
        f = day_features(candles, self.S)
        if self.S.get("mtf_tfs"):
            f["mtf"] = [None] * (len(candles) - 1) + [live_mtf(full, candles[-1][0] + 60, self.S["mtf_tfs"])]
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
                           lambda K: prices.get(rows[K].ticker) if K in rows else None, S)
        if not pick:
            log.info("%s %s: no strike with premium in [%s, %s] - skip", name, side,
                     S["min_premium"], S["max_premium"])
            return
        K, ltp = pick
        row = rows[K]
        fill_px = ltp * (1 + self.cfg["paper_slippage_pct"] / 100) if self.paper else ltp
        c_ = self.cfg
        over = {} if is_exp else {"stop_loss_pct": c_["stop_loss_pct_ne"], "target_pct": c_["target_pct_ne"]}
        saved, self.cfg = self.cfg, {**self.cfg, **over}          # enter()/size() read stop and target from self.cfg
        try:
            self.enter(name, side, row, g.round_tick(fill_px, row.tick),
                       {"trail_gap": c_["trail_gap_pct"] if is_exp else c_["trail_gap_pct_ne"],
                        "stop_pct": self.cfg["stop_loss_pct"],
                        "mode": self.S["mode"], "expiry_day": is_exp, "mtf": f.get("mtf", [None])[-1], "spot": spot, "ltp_seen": ltp, "expiry": expiry.strftime("%Y-%m-%d")})
        finally:
            self.cfg = saved

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
                    self._close(name, px or p.sl, self._stop_label(p), broker_done=True)
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

            # auto trailing stop: after +trigger%, stop = peak premium minus gap% (only ever moves up)
            if c.get("trail_enabled") and (p.peak / p.entry - 1) * 100 >= c["trail_trigger_pct"]:
                new_sl = g.round_tick(p.peak * (1 - p.meta.get("trail_gap", c["trail_gap_pct"]) / 100), p.tick)
                if new_sl - p.sl >= p.entry * c["trail_min_step_pct"] / 100:
                    if not p.meta.get("trailing"):
                        log.info("TRAIL ON %s peak %.2f (+%.0f%%)", p.symbol, p.peak, (p.peak / p.entry - 1) * 100)
                    p.meta["trailing"], p.trail_on, p.sl = True, True, new_sl
                    if not self.paper and p.sl_order_id:
                        lim = g.round_tick(new_sl * (1 - c["sl_limit_buffer_pct"] / 100), p.tick)
                        if not self.broker.modify_sl(p.sl_order_id, p.qty, new_sl, lim):
                            log.warning("SL modify failed for %s", p.symbol)
                    log.info("SL -> %.2f on %s", p.sl, p.symbol)
                    self._save_state()

            if ltp >= p.target:
                self._close(name, ltp * (1 - slip), "TARGET")
                continue
            if self.paper or not p.sl_order_id:
                if ltp <= p.sl:
                    self._close(name, ltp * (1 - slip), self._stop_label(p))
            elif ltp <= p.sl * (1 - c["gap_fallback_pct"] / 100):
                log.warning("%s gapped through SL band - forcing exit", p.symbol)
                self._close(name, ltp, "SL_GAP")

    @staticmethod
    def _stop_label(p):
        return "TRAIL" if p.meta.get("trailing") else "BE" if p.trail_on else "SL"

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
        td = c.get("trade_days", "expiry")
        active = [n for n, d in today_expiries.items()
                  if td == "all" or (td == "expiry" and d == now.date()) or (td == "non_expiry" and d != now.date())]
        log.info("Expiry Blast | mode=%s | strategy=%s | trade_days=%s | trading today: %s | expiry dates: %s",
                 "PAPER" if self.paper else "LIVE", c["strategy"]["mode"], td, active or "NONE", today_expiries)
        if not active and not self.positions:
            log.info("Nothing to trade today under trade_days=%s.", td)
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
