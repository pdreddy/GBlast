"""Offline-only scaled exits: +25% arm trailing on ~80% lots; +50% sell
half original lots and trail remainder. This module never submits orders."""
import math
from datetime import datetime

def simulate_scaled_trade(bars,start_idx,entry,lots,lot_size,initial_stop,cfg,
    square_off="15:10",trail_trigger_pct=.25,trail_fraction=.80,
    partial_trigger_pct=.50,partial_fraction=.50,trail_gap_pct=.10):
    def rejection(why):
        return {"reason":why,"net":None,"exit":None,"timestamp":None,
                "fees":None,"fills":[],"partial_taken":False,"trailing_activated":False}
    if not math.isfinite(entry) or not math.isfinite(initial_stop) or not 0<initial_stop<entry:
        return rejection("INVALID_LEVELS")
    if not isinstance(lots,int) or not isinstance(lot_size,int) or lots<2 or lot_size<1:
        return rejection("INSUFFICIENT_LOTS_FOR_PARTIAL_EXIT")
    if not 0<trail_fraction<1 or not 0<partial_fraction<1 or not 0<trail_gap_pct<1:
        return rejection("INVALID_POLICY")
    if not bars or start_idx>=len(bars):
        return rejection("MISSING_OPTION_DATA")
    remaining=lots
    fixed_lots=lots
    trailing_lots=0
    trail_stop=None
    peak=entry
    activated=False
    partial_taken=False
    armed=0
    fills=[]
    previous=None
    def fill(ts,reason,n_lots,price):
        nonlocal remaining
        qty=n_lots*lot_size
        px=max(.01,price*(1-float(cfg["slip_pct"])))
        fees=(entry+px)*qty*float(cfg["fee_bps"])/10000
        if not fills:
            fees+=float(cfg["fixed_roundtrip"])
        net=(px-entry)*qty-fees
        remaining-=n_lots
        fills.append({"timestamp":ts,"reason":reason,"lots":n_lots,
            "qty":qty,"exit":round(px,4),"fees":round(fees,4),
            "net":round(net,4),"remaining_lots":remaining})
    for bar in bars[start_idx:]:
        ts=bar.get("timestamp")
        try:
            cur=datetime.fromisoformat(ts)
            if previous is not None and (cur-previous).total_seconds()!=60:
                return rejection("MISSING_OPTION_DATA")
            previous=cur
            op,hi,lo=(float(bar[k]) for k in ("open","high","low"))
            if not all(math.isfinite(x) for x in (op,hi,lo)) or min(op,hi,lo)<=0 or hi<max(op,lo):
                return rejection("INVALID_OPTION_BAR")
        except (ValueError,TypeError,KeyError,AttributeError):
            return rejection("INVALID_OPTION_BAR")
        if ts[11:16]>=square_off:
            if fixed_lots:
                fill(ts,"TIME_EXIT_FIXED",fixed_lots,op)
                fixed_lots=0
            if trailing_lots:
                fill(ts,"TIME_EXIT_TRAIL",trailing_lots,op)
                trailing_lots=0
            break
        stopped=False
        if fixed_lots and lo<=initial_stop:
            fill(ts,"INITIAL_STOP",fixed_lots,min(op,initial_stop))
            fixed_lots=0
            stopped=True
        if trailing_lots and trail_stop is not None and lo<=trail_stop:
            fill(ts,"TRAIL_STOP",trailing_lots,min(op,trail_stop))
            trailing_lots=0
            stopped=True
        if remaining==0: break
        if stopped: continue
        if not partial_taken and hi>=entry*(1+partial_trigger_pct):
            n=max(1,min(lots-1,int(math.floor(lots*partial_fraction+.5))))
            part=min(fixed_lots,n)
            if part:
                fill(ts,"TAKE_PROFIT_50",part,entry*(1+partial_trigger_pct))
                fixed_lots-=part
            if n-part:
                fill(ts,"TAKE_PROFIT_50",n-part,entry*(1+partial_trigger_pct))
                trailing_lots-=n-part
            partial_taken=True
            trailing_lots+=fixed_lots
            fixed_lots=0
            activated=True
            armed=max(armed,trailing_lots)
        elif not activated and hi>=entry*(1+trail_trigger_pct):
            n=max(1,min(lots-1,int(math.floor(lots*trail_fraction+.5))))
            trailing_lots=n
            fixed_lots=lots-n
            activated=True
            armed=n
        if trailing_lots:
            peak=max(peak,hi)
            nxt=peak*(1-trail_gap_pct)
            trail_stop=nxt if trail_stop is None else max(trail_stop,nxt)
            # Only the NEXT candle can hit this newly calculated stop.
        if remaining==0: break
    if remaining:
        return rejection("MISSING_OPTION_DATA")
    return {"reason":"SCALED_EXIT","net":round(sum(x["net"] for x in fills),2),
      "exit":fills[-1]["exit"],"timestamp":fills[-1]["timestamp"],
      "fees":round(sum(x["fees"] for x in fills),2),"fills":fills,
      "partial_taken":partial_taken,"trailing_activated":activated,
      "intended_trail_pct":round(trail_fraction*100,2),"armed_trail_lots":armed,
      "armed_trail_pct":round(armed/lots*100,2),
      "final_peak":round(peak,4),
      "final_trail_stop":round(trail_stop,4) if trail_stop is not None else None}
