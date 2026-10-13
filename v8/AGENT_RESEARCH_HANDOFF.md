# Gamma Blast V8 — Agent Research Handoff

This development branch remains PAPER/RESEARCH ONLY. Do not activate live order placement or FYERS webhooks.

## Purpose

Use the private FYERS contract-level P&L statement to diagnose loss concentration and trading charges; combine India VIX and NIFTY/SENSEX price momentum to screen CE/PE signals; veto entries that fail data quality, liquidity, risk, whole-lot, post-loss cooldown or cost-adjusted reward tests; and backtest on actual historical option premium candles when available.

## Risk defaults

Capital ₹100,000. Planned trade risk ₹500; daily loss lock ₹2,000; weekly lock ₹5,000; peak-to-equity drawdown research lock 20%. Only one position at a time. Minimum two whole lots if selling 50% of original quantity; +25% premium activates trailing on approximately 80% lots; +50% sells half; continue trailing the rest. Stops and drawdown locks do not bound gap losses.

## Evidence status

The source FYERS P&L spreadsheet has 110 aggregated contract rows, NOT individual timestamps or stop-order events. **Do not commit** private P&L, token, or account data to this public repository.

The complete V8 project is delivered separately to the owner as a private downloadable research package, with 63 automated tests passing, synthetic paper fixtures, a local Streamlit dashboard and a chronological walk-forward validator. This file is not a claim that the full package was pushed to GitHub.

No genuine option-by-option six-month backtest or profitable trading edge is established. Real historical options candles, index/VIX feeds, costs, and the individual executed order book remain required. No live trading is authorized or enabled.
