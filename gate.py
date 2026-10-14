"""
LIVE-TRADING GATE - turns the success plan into code.
The bot may only trade with real money (paper_trading = False) if ALL of these hold:
  1. the latest backtest verdict (expiry_best.json) exists and is not 'bad' (NO EDGE),
  2. the paper-trade log has at least MIN_PAPER_TRADES trades,
  3. paper net P&L after costs is positive and profit factor >= MIN_PF,
  4. paper max drawdown is below MAX_DD_PCT of capital.
You can switch the gate off with "live_gate": False in CONFIG - the bot then logs a loud warning. That is your call to make
knowingly; the default is ON.
"""
import csv
import json
import os

MIN_PAPER_TRADES = 40
MIN_PF = 1.2
MAX_DD_PCT = 6.0


def paper_stats(log="expiry_blast_trades.csv"):
    pnls = []
    if os.path.exists(log):
        for row in csv.reader(open(log)):
            try:
                if row[12] == "PAPER":
                    pnls.append(float(row[9]))
            except (IndexError, ValueError):
                continue
    wins, losses = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p <= 0)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(pnls), "net": sum(pnls), "pf": (wins / losses) if losses else (99.0 if wins else 0.0), "max_dd": dd}


def live_allowed(capital, best_path="expiry_best.json", log="expiry_blast_trades.csv"):
    """-> (allowed: bool, reasons: list[str] of what is missing)."""
    reasons = []
    if not os.path.exists(best_path):
        reasons.append("no backtest result yet - run expiry_backtest on real data (expiry_best.json missing)")
    else:
        best = json.load(open(best_path))
        if best.get("verdict") == "bad":
            reasons.append(f"latest backtest verdict is '{best.get('headline')}'")
    s = paper_stats(log)
    if s["n"] < MIN_PAPER_TRADES:
        reasons.append(f"only {s['n']} paper trades (need {MIN_PAPER_TRADES}+)")
    else:
        if s["net"] <= 0:
            reasons.append(f"paper net P&L is Rs {s['net']:,.0f} (must be positive after costs)")
        if s["pf"] < MIN_PF:
            reasons.append(f"paper profit factor {s['pf']:.2f} (need >= {MIN_PF})")
        if s["max_dd"] > capital * MAX_DD_PCT / 100:
            reasons.append(f"paper max drawdown Rs {s['max_dd']:,.0f} exceeds {MAX_DD_PCT}% of capital")
    return (not reasons), reasons
