# Gamma Blast – FYERS Auto Trader (NIFTY · BANKNIFTY · SENSEX)

Hero-or-zero intraday **option buying** bot. Buys high-gamma ATM/near-OTM options of the nearest expiry when the index breaks out with momentum.

| Rule | Setting |
|---|---|
| Stop loss | 40% below entry premium (placed as a real SL order at FYERS) |
| Target | +200% (premium triples) → risk:reward 1:5 |
| Trailing stop | Starts at +50%, trails 25% below the peak premium |
| Entries | 09:30 – 14:30 (14:45 on expiry day) |
| Square-off | 15:15 IST |
| Daily limits | 2 trades per index, 2 open positions, stop after −3% of capital |

## Project files

```
GammaBlast/
├── .env              <- YOUR App ID + Secret (copy from .env.example; git-ignored, never commit)
├── config.py         <- reads .env (no secrets in code)
├── gamma_blast.py    <- the strategy; CONFIG block at the top for capital/risk/indices
├── fyers_login.py    <- daily login, creates access_token.txt
├── start.bat         <- Windows: double-click every morning
├── start.sh          <- Mac/Linux: ./start.sh every morning
├── stop.bat / stop.command  <- emergency stop: squares off everything
├── backtest.py / backtest.command  <- test the rules on past data
├── test_offline.py / test_connection.py / run_tests.command  <- checks
└── requirements.txt
```

Created automatically while running: `gamma_blast.log` (full log), `gamma_blast_trades.csv` (every trade, for review), `gamma_blast_state.json` (lets the bot resume if restarted the same day).

## One-time setup

1. Install **Python 3.9 or newer** (Windows: tick "Add Python to PATH").
2. Create an API app at <https://myapi.fyers.in/dashboard>. Set the redirect URL to
   `https://trade.fyers.in/api-login/redirect-uri/index.html`. Give it order permissions.
3. Copy `.env.example` to `.env` and fill in `FYERS_CLIENT_ID` and `FYERS_SECRET_KEY` (this file is git-ignored).
4. Open `gamma_blast.py`, and in `CONFIG` set `capital` to your real trading amount.

## Every trading day (start before 09:15 IST)

1. Double-click **start.bat** (or run `./start.sh`).
2. A FYERS login page opens. Log in, then copy the `auth_code` from the address bar (or paste the whole URL) into the window.
3. Leave the window open. The bot waits for 09:30, trades, and squares off at 15:15.

**Emergency stop:** double-click `stop.bat` (or create a file named `KILL` in this folder). Delete `KILL` before the next start.

## Backtest (do this first)

Mac: right-click → Open **backtest.command** (Windows: `python backtest.py`). Log in, choose how many days (default 180).
It downloads real 1-minute index data, replays the exact bot rules, and opens **backtest_report.html**.
Index prices are real; option premiums are modelled from India VIX (FYERS has no history for expired options),
so results are an estimate. `python backtest.py --demo` runs on fake data without login, just to check it works.

## Important

- **Paper mode is ON** (`paper_trading = True`). It logs trades without placing orders. Paper trade 4–6 weeks and review `gamma_blast_trades.csv` before switching to `False`.
- **Position size:** risk per trade = `capital × risk_per_trade_pct`. If one lot at a 40% stop costs more than that, the trade is skipped. With ₹2L and 1%, most NIFTY trades are skipped — raise risk to 1.5–2% or raise capital.
- **BANKNIFTY** has only monthly expiries, so it qualifies for trades far less often.
- Keep the PC awake and online during market hours. The FYERS-side stop protects open trades if it disconnects, but the trailing stop only moves while the bot is running.
- This is software, not investment advice. Option buying with a 40% stop loses on most trades and relies on rare big winners. Trade only money you can afford to lose.


---

# Expiry Blast (new) - expiry-day hero-or-zero, NIFTY Tue + SENSEX Thu

Separate strategy with its own CONFIG. Reuses the login, broker wrapper, symbol master, broker-side SL, cancel-then-sell exits, KILL file, state file and paper mode of Gamma Blast.

| Rule | Setting |
|---|---|
| Days | Only the expiry day of each index, only the expiring contract |
| Strike | ATM or 1-2 OTM, premium must be Rs 20-150 |
| Stop loss | -25% of entry, placed as a broker SL-L order right after entry |
| Target | +100% (1:4). Optional breakeven: at +60% stop moves to entry (`breakeven_enabled`) |
| Limits | 2 trades per index per day, 1 open position per index |
| Time | No entries after 14:45, square-off 15:15 |
| Risk | 1% of capital per trade; skipped if 1 lot risks more |
| Entry modes | `ORB` opening-range break, `EXT` day-high/low break, `MOM` momentum candle (all need VWAP side + EMA9/21 direction) |

Files: `expiry_blast.py` (bot + shared signal code), `expiry_backtest.py`, `expiry_recorder.py`, `test_expiry_offline.py`, and Mac launchers `expiry_blast.command`, `expiry_backtest.command`, `expiry_recorder.command` (right-click > Open the first time). `stop.command` stops it too.

**Backtest** (`expiry_backtest.command`, or `python expiry_backtest.py`): put your 1-minute CSVs in `data/` (names must contain NIFTY / SENSEX / VIX; BANKNIFTY ignored; columns epoch,open,high,low,close,volume). It evaluates 20 simple variants on the **first half** of the data, freezes the best, and reports the **second half** unchanged, with a plain-language verdict, equity curve, monthly P&L, per-index / per-time-window tables, breakeven on/off, and a robustness table (worse slippage / IV). Open `expiry_report.html`.
`--demo null` (random walk with fair option pricing - must show no edge) and `--demo trend` (edge built in - engine must find it) check the machinery without data.

**Premiums are modelled** (Black-Scholes, India VIX x expiry-day IV profile, extra fade after 13:00, 2%/side slippage, full charges; stop assumed first if a bar touches both). Assumptions sit in `BT` at the top of `expiry_backtest.py` - verify STT/lot sizes against your contract note.

**Recorder**: run `expiry_recorder.command` before 09:15 on each expiry day. It saves ATM +/-3 strikes (CE+PE) 1-minute quotes to `data/expiry_premiums/`. After a few weeks you have real premiums.

**Honest expectations**: breakeven win rate for 1:4 is ~20% before costs. 1% risk x max 4 trades/day cannot produce +200%/month; do not raise risk to chase it. The default strategy in CONFIG is a prior guess, not a validated edge - trust only what the test half of *your real data* shows, then paper trade.
