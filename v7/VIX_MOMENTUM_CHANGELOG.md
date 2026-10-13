# Gamma Blast V7.1 research plan

Keep **paper-only** execution. Watch completed 1-minute India VIX and NIFTY/SENSEX candles, using five-minute VIX percent momentum, five-minute index momentum, EMA9/EMA21, VWAP when actual volume is available, and recent candle breakouts. Index direction selects CE or PE; VIX provides market-regime confirmation. Reject stale VIX.

Strategies: VIX bull CE 5% premium stop/30% target; VIX bear PE 5% premium stop/30% target; momentum scalp 10 premium points stop/20 target; optional 12 premium points stop/24 target; expiry VIX impulse with 5% stop/40% target; GEX+VIX filter only if real and timely OI and Greeks are supplied. For expiry only, allow fresh signals through 15:15 IST and force exit by 15:25 IST. Never treat a 12/20 point trade as 1:2.

Shared capital ₹100,000; planned risk 1% per trade; 4% daily lock and 20% peak-to-equity drawdown cutoff. Gaps may exceed those controls. Historical P&L requires actual option candles and indicative bid/ask. No synthetic return proves profitability.

This changelog is a design record only; the full engine and dashboard must be independently committed and tested before considering this GitHub branch feature-complete.