# QUICK START: credentials, daily token, running

## 1. One-time: get your App ID and Secret ID, save them in `.env`
1. Log in at <https://myapi.fyers.in/dashboard> and click **Create App**.
2. Set the **Redirect URL** to `https://trade.fyers.in/api-login/redirect-uri/index.html` and give the app order permissions.
3. Copy the **App ID** (looks like `AB1CD2EF3G-100`) and the **Secret ID**.
4. In the project folder run `cp .env.example .env`, open `.env` in any text editor, and fill in:
   ```
   FYERS_CLIENT_ID=AB1CD2EF3G-100
   FYERS_SECRET_KEY=your_secret_id
   ```
   `.env` is git-ignored, so it is never committed. Never share it. If a Secret ID leaks, regenerate it on the dashboard.

## 2. Every trading day: get the access token (it expires daily)
The access token is created by `fyers_login.py`. The `.command` launchers run it for you automatically before the bot starts. To run it by hand, use `python3 fyers_login.py`.
1. A FYERS login page opens in your browser. Log in with your FYERS ID, PIN and TOTP.
2. You land on a page whose address bar contains `...?auth_code=XXXX...`. Copy the `auth_code` value, or copy the whole address, into the terminal prompt **Paste auth_code** and press Enter.
3. It prints `Login OK - saved access_token.txt`. The token is saved in the project folder in `access_token.txt` (git-ignored) and the bot reads it from there.
4. If a token from today already exists, the login step is skipped. A new token is needed each trading day.

## 3. Running: nothing to copy except your secrets
The only things you provide are the secrets in `.env` and the daily `auth_code` paste at login (FYERS requires a login every day; nothing else is manual).
| What | Mac | Terminal |
|---|---|---|
| **Backtest**: logs in, downloads 1 year of 1-minute NIFTY / SENSEX / India VIX from FYERS into `data/` (later runs fetch only new days), then tests | `expiry_backtest.command` | `python3 expiry_backtest.py` |
| Record real expiry-day premiums (before 09:15) | `expiry_recorder.command` | `python3 expiry_recorder.py` |
| Expiry Blast bot, paper mode (before 09:15) | `expiry_blast.command` | `python3 expiry_blast.py` |
| Offline checks | `run_tests.command` | `python3 test_expiry_offline.py` |
| Emergency stop (squares off and stops) | `stop.command` | `touch KILL` |

(Mac: right-click > Open the first time, or `chmod +x *.command`.)
Backtest options: `--days 365` (calendar days to download and test; default 365, the launcher asks), `--no-download` uses only the CSVs already in `data/`; `--demo null|trend` uses synthetic data.

**Using the backtest's choice without copying anything:** the backtest writes `expiry_best.json`. In `expiry_blast.py` set `"use_backtest_result": True` (one flag) and the bot loads the chosen entry mode, window, strike and breakeven setting from it. It refuses to adopt a result whose verdict is "NO EDGE".

Suggested order: backtest first, then record on expiry days, then paper trade 4-6 weeks. Paper mode is ON by default (`"paper_trading": True`). Delete the `KILL` file before the next start.

**Troubleshooting:** `Credentials missing` means `.env` is absent or its names are wrong. `Login failed` usually means a wrong or old `auth_code`, or a Redirect URL that does not match the app. `access_token.txt not found` means run the login first.

---

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
| Multi-timeframe | The **5-minute, 15-minute and 1-hour** charts must all trend the same way as the trade (EMA9 vs EMA21 of completed bars, built from 1-minute data across days; `mtf_tfs` in CONFIG, `[]` = off). The backtest shows what the filter does versus 1-minute only. |

Files: `expiry_blast.py` (bot + shared signal code), `expiry_backtest.py`, `expiry_recorder.py`, `test_expiry_offline.py`, and Mac launchers `expiry_blast.command`, `expiry_backtest.command`, `expiry_recorder.command` (right-click > Open the first time). `stop.command` stops it too.

**Backtest** (`expiry_backtest.command`, or `python expiry_backtest.py`): put your 1-minute CSVs in `data/` (names must contain NIFTY / SENSEX / VIX; BANKNIFTY ignored; columns epoch,open,high,low,close,volume). It evaluates 20 simple variants on the **first half** of the data, freezes the best, and reports the **second half** unchanged, with a plain-language verdict, equity curve, monthly P&L, per-index / per-time-window tables, breakeven on/off, and a robustness table (worse slippage / IV). Open `expiry_report.html`.
`--demo null` (random walk with fair option pricing - must show no edge) and `--demo trend` (edge built in - engine must find it) check the machinery without data.

**Premiums are modelled** (Black-Scholes, India VIX x expiry-day IV profile, extra fade after 13:00, 2%/side slippage, full charges; stop assumed first if a bar touches both). Assumptions sit in `BT` at the top of `expiry_backtest.py` - verify STT/lot sizes against your contract note.

**Recorder**: run `expiry_recorder.command` before 09:15 on each expiry day. It saves ATM +/-3 strikes (CE+PE) 1-minute quotes to `data/expiry_premiums/`. After a few weeks you have real premiums.

**Honest expectations**: breakeven win rate for 1:4 is ~20% before costs. 1% risk x max 4 trades/day cannot produce +200%/month; do not raise risk to chase it. The default strategy in CONFIG is a prior guess, not a validated edge - trust only what the test half of *your real data* shows, then paper trade.


---

# Signal Lab (does anything predict the index?)

`signal_lab.command` (or `python3 signal_lab.py`) tests 8 pre-declared entry signals on your real 1-minute NIFTY + SENSEX data **without any option-price modelling**: after each signal it measures the signed index move after 15/30/60 minutes, discovery half vs unseen half, with a multiple-testing-aware pass rule (|t| >= 2.5 on discovery AND same sign |t| >= 1.65 on the unseen half). It tells you plainly if nothing predicts the index. Only a signal that passes is worth turning into an option strategy. `--demo null|trend` self-checks the lab (random walk must give no passes; the trend data must).
