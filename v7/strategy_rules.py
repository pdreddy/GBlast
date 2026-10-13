"""Research rules, not order execution."""
CAPITAL = 100_000
DRAWDOWN_GUARD = 0.20
OPTION_PREMIUM_STOP = 0.05
STRATEGIES = {'orb_retest': 0.30, 'ema_pullback': 0.30, 'volatility_breakout': 0.40, 'gamma_breakout': 0.40}
MARKETS = ('NIFTY','SENSEX')
RISK_PER_TRADE = 0.01
MAX_TRADES_PER_DAY = 6
