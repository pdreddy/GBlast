"""Offline checks for pattern_lab: each pattern fires on a hand-made chart and stays quiet otherwise,
patterns never look ahead, and the outcome rules (stop first, target, time-out is not a win) are right."""
import random
import sys
import types

fake = types.ModuleType("fyers_apiv3")
fake.fyersModel = types.SimpleNamespace()
sys.modules["fyers_apiv3"] = fake
import pattern_lab as pl

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


def bar(o, h, l, c, k, t15=0, atr=10.0, vwap=None):
    return {"o": o, "h": h, "l": l, "c": c, "mclose": 555 + 5 * (k + 1), "atr": atr, "t15": t15, "t60": t15,
            "vwap": vwap if vwap is not None else (o + c) / 2, "i1": k * 5 + 4}


def flat_bars(n, px=25000.0, rng=4.0, t15=0):
    return [bar(px - 1, px + rng / 2, px - rng / 2, px + 1 - 2 * (k % 2), k, t15) for k in range(n)]


D = {"prev_hi": None, "prev_lo": None}
print("\n[1] Outcome rules")
m = [[0, 100, 100, 100, 100, 0]] + [[i, 100, 100.5, 99.5, 100, 0] for i in range(1, 20)]
m[3] = [3, 100, 106, 94, 100, 0]                               # bar touches BOTH target (+5 at 1R, risk 5) and stop
check("bar touching both stop and target -> stop first", pl.outcome(m, 1, 1, 100, 5, 1.0, 19) == (-1.0, False))
m2 = [[i, 100, 100.5, 99.5, 100, 0] for i in range(20)]
m2[4] = [4, 100, 105.5, 99.6, 105, 0]
check("target hit -> +RR and a win", pl.outcome(m2, 1, 1, 100, 5, 1.0, 19) == (1.0, True))
r, hit = pl.outcome(m2, 1, -1, 100, 5, 1.0, 3)
check("short: stop is above entry", pl.outcome([[i, 100, 100.5, 99.5, 100, 0] for i in range(10)], 1, -1, 100, 5, 1, 9)[1] is False)
m3 = [[i, 100, 100.5, 99.5, 100.4, 0] for i in range(20)]
r, hit = pl.outcome(m3, 1, 1, 100, 5, 2.0, 19)
check("time-out is marked to market and is NOT a win", hit is False and abs(r - 0.08) < 0.01, f"r={r:.2f}")
check("break-even win rate = 1/(1+RR)", [round(100 / (1 + rr)) for rr in pl.RRS] == [67, 50, 33])

print("\n[2] Patterns fire on hand-made charts")
# inside bar
B = flat_bars(12, t15=1)
B[9] = bar(25000, 25020, 24990, 25010, 9, 1)                   # mother
B[10] = bar(25005, 25015, 24995, 25000, 10, 1)                 # inside
B[11] = bar(25001, 25030, 25000, 25025, 11, 1)                 # closes above mother high
check("inside-bar break up with 15m up -> long", pl.p_inside(B, 11, D) == 1)
B[11]["t15"] = 0
check("inside-bar break without trend agreement -> nothing", pl.p_inside(B, 11, D) == 0)
# flag
B = flat_bars(14)
for k, (o, c) in zip((6, 7, 8), ((25000, 25020), (25020, 25040), (25040, 25060))):
    B[k] = bar(o, c + 2, o - 2, c, k)                          # impulse +60 > 1.5 ATR (10)
for k in (9, 10, 11, 12):
    B[k] = bar(25058, 25062, 25052, 25057, k)                  # tight pause (range 10 <= 50% of 60)
B[13] = bar(25057, 25070, 25056, 25068, 13)
check("flag: impulse + tight pause + close above -> long", pl.p_flag(B, 13, D) == 1)
B[13] = bar(25057, 25061, 25040, 25045, 13)
check("flag: close below the pause against the impulse -> nothing", pl.p_flag(B, 13, D) == 0)
# double bottom
px = [25040, 25030, 25020, 25010, 25000, 25010, 25020, 25030, 25040, 25050, 25040, 25030, 25020, 25010, 25001, 25012, 25025, 25040, 25060]
B = [bar(p, p + 3, p - 3, p, k) for k, p in enumerate(px)]
B[4]["l"] = B[14]["l"] = 24996                                  # two equal lows
B[9]["h"] = 25056                                               # neckline
check("double bottom: close above the neckline -> long", pl.p_double(B, 18, D) == 1)
check("double bottom: NOT before the neckline break", pl.p_double(B, 17, D) == 0)
# trap
B = flat_bars(12, 25000, 6)
for k in range(3, 9):
    B[k] = bar(25000, 25010, 24990, 25000, k)                  # 30-min range 24990-25010
B[9] = bar(25000, 25016, 25000, 25014, 9)                       # closes above = breakout
B[10] = bar(25014, 25016, 25004, 25006, 10)
B[11] = bar(25006, 25008, 24996, 25003, 11)                    # closes back inside -> fade the failed break
check("trap: failed up-break closes back inside -> SHORT", pl.p_trap(B, 11, D) == -1)
B[11] = bar(25006, 25030, 25005, 25028, 11)
check("trap: break that holds -> no fade", pl.p_trap(B, 11, D) == 0)
# NR7
B = [bar(25000, 25012, 24988, 25000, k, 1) for k in range(8)]
B[7] = bar(25000, 25001, 24999, 25000, 7, 1)                    # narrowest
B.append(bar(25000, 25012, 24999, 25010, 8, 1))
check("NR7: close above the narrow bar with trend -> long", pl.p_nr7(B, 8, D) == 1)
# retest of opening range (09:15-09:30 = first 3 five-min bars)
B = [bar(25000, 25010, 24990, 25000, k) for k in range(3)]
B += [bar(25000, 25020, 25000, 25018, 3), bar(25018, 25030, 25015, 25028, 4), bar(25028, 25032, 25012, 25014, 5),
      bar(25014, 25014, 25008, 25012, 6), bar(25012, 25030, 25009, 25026, 7)]   # bar 7 retests 25010 and closes up
check("retest: break, pullback to the edge, hold -> long", pl.p_retest(B, 7, D) == 1, f"{pl.p_retest(B, 7, D)}")
check("retest: not on a random bar", pl.p_retest(B, 5, D) == 0)

print("\n[3] No look-ahead (changing future bars never changes a signal)")
rnd = random.Random(3)
px, B = 25000.0, []
for k in range(60):
    o = px
    px += rnd.gauss(0, 6)
    B.append(bar(o, max(o, px) + rnd.random() * 3, min(o, px) - rnd.random() * 3, px, k, rnd.choice((-1, 0, 1))))
B2 = [dict(b) for b in B]
for k in range(40, 60):                                         # rewrite the future
    B2[k] = dict(B2[k], h=B2[k]["h"] + 50, l=B2[k]["l"] - 50, c=B2[k]["c"] + 30)
for name, fn in pl.PATTERNS:
    same = all(fn(B[:j + 1], j, D) == fn(B2[:j + 1], j, D) for j in range(12, 40))
    check(f"{name}: no look-ahead", same)
print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
