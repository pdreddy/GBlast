"""
RUN ALL - one command: log in once, download data once, run Signal Lab + Pattern Lab + Expiry backtest,
then write master_report.html with ONE overall verdict and the live-trading gate status.

  python run_all.py                  (real data, 365 days)         python run_all.py --demo null|trend   (synthetic, no login)
  python run_all.py --days 365 --no-download

There is no "full success rate" strategy. This tool tells you honestly which (if any) pieces have evidence behind them,
and trading_gate.json / gate.py keep the bot in paper mode until the evidence exists.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys

import expiry_backtest as xb
import expiry_blast as eb
import gate


def run(script, extra):
    p = subprocess.run([sys.executable, script, *extra], capture_output=True, text=True)
    out = p.stdout + p.stderr
    print(f"--- {script}: exit {p.returncode}")
    print("\n".join(l for l in out.splitlines() if " INFO " not in l)[-1500:])
    return out


def load(path):
    return json.load(open(path)) if os.path.exists(path) else None


def overall(bt_kind, passes):
    if bt_kind == "good":
        return ("good", "POSSIBLE EDGE (not proven)", "The backtest and the index tests both show something. Next: paper trade 40+ trades; the gate then decides about live trading.")
    if bt_kind == "warn":
        return ("warn", "WEAK / INCONCLUSIVE", "Some evidence but not convincing. Paper trade only; do not go live. Re-run monthly as data grows.")
    if passes:
        return ("warn", "THE INDEX MOVES, BUT THE OPTION TRADE DOES NOT PAY",
                "At least one signal predicts the index, but after option costs the backtest has no edge. Next: test that one signal at option level with real recorded premiums.")
    return ("bad", "NOTHING IS VALIDATED", "No signal, pattern or strategy beat chance on unseen data after costs. Do not trade real money. "
            "Use paper mode and the recorder to learn; test new written-down ideas instead of tuning old ones.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", nargs="?", const="null", choices=["null", "trend"])
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()
    indices = [k for k, v in eb.CONFIG["indices"].items() if v["enabled"]]
    common = ["--days", str(a.days)]
    if a.demo:
        common += ["--demo", a.demo]
    else:
        if not a.no_download:
            try:
                xb.sync_data(xb.connect(), indices, "data", a.days)
            except SystemExit as e:
                print(f"Could not log in / download: {e}\nContinuing with the CSVs already in data/.")
        common += ["--no-download"]
    out_bt = run("expiry_backtest.py", common)
    run("signal_lab.py", common)
    run("pattern_lab.py", common)

    verdict = (re.findall(r"VERDICT: (.*)", out_bt) or ["(backtest did not finish)"])[-1]
    tune = (re.findall(r"TUNE: (.*)", out_bt) or [""])[-1]
    test = (re.findall(r"TEST: (.*)", out_bt) or [""])[-1]
    chosen = (re.findall(r"Chosen on TUNE half only: (.*)", out_bt) or [""])[-1]
    kind = "bad" if "NO EDGE" in verdict else "good" if "POSSIBLE EDGE" in verdict else "warn"
    sl, pl = load("signal_lab_summary.json") or {}, load("pattern_lab_summary.json") or {}
    passes = (sl.get("passes") or 0) + (pl.get("passes") or 0)
    ok_live, reasons = gate.live_allowed(eb.CONFIG["capital"])
    ov_kind, ov_head, ov_body = overall(kind, passes)
    demo = f" (DEMO {a.demo} - synthetic data)" if a.demo else ""
    rep = ("expiry_report_DEMO_%s.html" % a.demo) if a.demo else "expiry_report.html"
    sig_rep = ("signal_lab_report_DEMO_%s.html" % a.demo) if a.demo else "signal_lab_report.html"
    pat_rep = ("pattern_lab_report_DEMO_%s.html" % a.demo) if a.demo else "pattern_lab_report.html"

    def row(name, k, text, link):
        return f"<tr><td>{name}</td><td class='{k}'>{html.escape(text)}</td><td><a href='{link}'>open report</a></td></tr>"
    sb = sl.get("best")
    pb = pl.get("best")
    rows = (row("1. Signal Lab - does any entry predict the INDEX?", "pos" if sl.get("passes") else "neg",
                f"{sl.get('passes', '?')} of {sl.get('tests', '?')} tests passed" + (f"; best: {sb['signal']} ({sb['side']} {sb['H']}m, {sb['unseen_bps']:+.1f} bps, t={sb['t']:.1f})" if sb else ""), sig_rep) +
            row("2. Pattern Lab - do chart patterns beat break-even after costs?", "pos" if pl.get("passes") else "neg",
                f"{pl.get('passes', '?')} of {pl.get('tests', '?')} tests passed" + (f"; best: {pb['pattern']} {pb['rr']:g}R (win {pb['win']:.0f}%, {pb['net_r']:+.2f}R, t={pb['t']:.1f})" if pb else ""), pat_rep) +
            row("3. Option backtest (expiry + non-expiry, trails, tight stop)", {"bad": "neg", "good": "pos"}.get(kind, ""),
                f"{verdict} | best on tuning: {chosen} | tune: {tune} | UNSEEN: {test}", rep))
    gate_html = ("<b class='pos'>PASSED</b> - live trading is permitted by the gate (you still choose)." if ok_live else
                 "<b class='neg'>CLOSED</b> - live trading is refused by the gate until:<ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in reasons) + "</ul>")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Master Report</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{xb.CSS} td:first-child{{width:34%}} td{{text-align:left!important}}</style></head><body>
<h1>Master report {html.escape(demo)}</h1><div class="sub">{dt.date.today()} · last {a.days} days · NIFTY + SENSEX</div>
<div class="banner {ov_kind}"><b>{html.escape(ov_head)}</b><br>{html.escape(ov_body)}</div>
<h2>What was tested</h2><table><tr><th>Test</th><th>Result</th><th></th></tr>{rows}</table>
<h2>Live-trading gate</h2><div class="banner">{gate_html}</div>
<div class="note"><b>There is no strategy with a full success rate.</b> Over 90% of individual F&amp;O traders lose money (SEBI studies FY22-FY25). This report only
says which ideas have survived honest tests on data they were not tuned on. Win rate alone is meaningless: break-even win rate is 1/(1+payoff).
Re-run monthly; never change a rule because of last week's trades.</div></body></html>"""
    with open("master_report.html" if not a.demo else f"master_report_DEMO_{a.demo}.html", "w", encoding="utf-8") as f:
        f.write(page)
    json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"), "overall": ov_head, "backtest_verdict": verdict,
               "predictive_passes": passes, "live_gate_open": ok_live, "reasons": reasons, "demo": bool(a.demo)},
              open("trading_gate.json", "w"), indent=2)
    print(f"\nOVERALL: {ov_head}\nLive gate: {'PASSED' if ok_live else 'CLOSED - ' + '; '.join(reasons)}\nSaved master report.")


if __name__ == "__main__":
    main()
