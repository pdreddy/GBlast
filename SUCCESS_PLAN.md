# Success Plan (built from the Jun-Aug 2026 F&O statement + the backtests)

## 1. What your own trades say
| Finding | Number | Rule it creates |
|---|---|---|
| Net result | -Rs 1,30,657 (trading -45,568, charges -85,089) | Charges are a bigger enemy than the market. Trade less, trade smaller. |
| Turnover | Rs 5.2 crore on a ~Rs 2L account; charges ~16 bps of turnover vs gross -9 bps | No scalping. Max 2 trades per index per expiry day. |
| Win rate / payoff | 36% wins, avg win +40%, avg loss -34% (about 1:1.2) | Hero-or-zero needs 1:4. Do not cut winners at +2-10%. |
| Holding to zero | 8 contracts expired worthless: -Rs 1,85,427 (a -25% stop would have cost about -46,000) | Stop-loss is mandatory and sits at the broker (SL-L). |
| Size | 16 rows over Rs 5 lakh bought lost Rs 4.07 lakh; 62 of 110 rows lost more than Rs 2,000 (1% of 2L) | Risk is 1% of capital per trade, enforced by the code. |
| Concentration | Two tickets made all the profit; without the best one the net is about -Rs 5.1 lakh | Do not count on tail winners; judge the process over 100+ trades. |
| Direction | Calls +4.12 lakh, puts -5.43 lakh while SENSEX drifted up ~7% | Trade WITH the 5m/15m/1h trend (built into the bot). |
| Worst day | SENSEX 6 Aug: -Rs 4.5 lakh in one expiry day | Daily loss limit 2%, stop after 3 losses in a row. |

## 2. What "success" means (and what it does not)
- Honest backtests found **no edge** in the entry rules tried so far. A return target such as 25% a month is not something a plan can promise: it needs about +25R every month. At 1% risk per trade that is, for example, 50 trades at +0.5R each, which no tested rule is near.
- Realistic goals, in order: (1) **do not lose more than the plan allows**, (2) **reach break-even after costs over 100+ trades**, (3) only then consider scaling. A good expectancy is +0.1 to +0.3R per trade, i.e. roughly 1-4% a month at 1% risk. Anything larger should make you suspicious, not excited.
- Success is measured by **process score** first: stops placed, size within 1%, no entry after 14:45, journal filled. Money results come later.

## 3. Phases with go/no-go gates (do not skip)
| Phase | What | Gate to move on |
|---|---|---|
| 0. Setup (week 1) | `.env`, login works, run `run_tests.command`, run `signal_lab.command` and `expiry_backtest.command` on 365 days, read both banners | Reports open, you understand each number |
| 1. Record (weeks 1-8) | `expiry_recorder.command` on every expiry day (real premiums, spreads) | 8+ expiry days recorded |
| 2. Paper trade (weeks 1-8) | `expiry_blast.command` in paper mode; journal every trade | 40+ paper trades, **process score >= 90%**, paper P&L after costs > 0 and max drawdown < 6% of capital |
| 3. Live, tiny (weeks 9-16) | Set `paper_trading` False only if the gate above is met. Capital Rs 20,000 only, risk 1% = Rs 200 per trade | 60+ live trades, expectancy after costs > 0, process score >= 90% |
| 4. Scale | Increase capital by at most 25% per step, never right after a winning streak | Repeat the Phase 3 gate at each step |
If a gate fails: stay in the phase, or go back one phase. A failed gate is information, not a reason to loosen rules.

## 4. Hard rules (the bot enforces the ones marked [code])
1. Trade only the expiry-day contract of NIFTY (Tue) and SENSEX (Thu). [code]
2. Risk 1% of capital per trade; skip if 1 lot risks more. [code]
3. Broker-side stop at -25% placed immediately after entry; target +100%. Never remove or widen a stop. [code]
4. Max 2 trades per index per day, 1 open position per index. [code]
5. No entries after 14:45; flat by 15:15. [code]
6. Stop for the day after losing 2% of capital or 3 losses in a row. [code]
7. Stop for the month after losing 6% of capital. [code]
8. 5m, 15m and 1h charts must agree with the trade direction. [code]
9. Never average down, never add to a loser, never revenge trade, never trade at larger size to "win it back".
10. Never trade the day after a halt without writing down what happened.

## 5. Daily routine (15 minutes)
- Before 09:15: login (`.command`), check `expiry_blast.log` shows PAPER/LIVE as intended, check the KILL file is absent, note VIX and any event (RBI, US data, budget).
- During: do nothing manually. If you override the bot, write why.
- After 15:30: journal each trade (time, side, premium in/out, reason, followed rules? yes/no, emotion 1-5). Save the recorder files.
- Weekly (Sunday): process score, number of trades, P&L in R (not rupees), costs paid, biggest rule break.
- Monthly: re-run `expiry_backtest` and `signal_lab` with the new data. Change a rule only if a pre-written hypothesis passes on unseen data; never because of last week.

## 6. What would change the plan
- If `signal_lab` shows no signal that predicts the index, no option-buying rule built on those entries can win; test new, written-down ideas instead of tuning old ones.
- If recorded real premiums show spreads wider than the 2% per side assumed, the backtest numbers get worse; redo the backtest with real prices.
- If you break the hard rules twice in a month, go back to paper for 4 weeks.
