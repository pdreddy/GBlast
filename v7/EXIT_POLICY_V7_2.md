# V7.2 — Scaled exit policy (paper research)

**Branch:** development/gamma-blast-v7-dashboard

* At **+25% option premium**: arm a trailing stop on approximately **80% of the original whole lots**. Remaining lots stay under initial stop.
* Default trail gap: **10% below highest premium**, activated starting the **next completed one-minute bar**. Trailing stop never moves down.
* At **+50% option premium**: sell **50% of the ORIGINAL quantity**, rounding to whole lots; keep the remaining lots on the trailing stop through square-off.
* Initial stop for VIX momentum: **5% of option premium**; for point-based scalps: 10 or 12 option-premium points. Prior fixed +30%/+40% target settings are now research reference levels, not actual exits.
* Trades with **fewer than two lots are rejected**, since half a lot cannot be sold.
* Stops are assumed to fill before favorable intrabar triggers; gaps can fill worse than any stop.
* Intended 80% may differ from actual whole-lot percentage. Report allocation (example: two lots allows only 50% trail and 50% initial stop before partial sale).
* Simulator logs each individual leg, total P&L, execution costs and a partial-exit audit.
* There is **no broker API execution** in the new scaled-exit research engine. The legacy bot is kept blocked from live startup on this branch.

**Current packaging:** Download the full tested V7.2 kit from the ChatGPT handoff. The full offline simulator/dashboard may require synchronization to GitHub; this document alone does not implement the new exit behavior.

Tests run locally: **42 passed** (including stop-first collisions, stale/missing bars, gaps, lot rounding, scaled exit and fees). The bundled data are **synthetic**, not verified historical options profit.
