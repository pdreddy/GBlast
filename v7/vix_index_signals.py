"""Offline-only VIX/index momentum filters. Does not place orders."""
def ema(xs,n):
    a=2/(n+1);v=xs[0]
    for x in xs[1:]:v+=a*(x-v)
    return v

def decide(strategy, index_bars, vix_bars, expiry=False):
    """Completed, time-aligned one-minute candles, with no forward inputs."""
    if len(index_bars)<26 or len(vix_bars)<6:return None
    i=index_bars[-1];v=vix_bars[-1]
    if i["timestamp"]!=v["timestamp"]:return None
    from datetime import datetime
    recent=vix_bars[-6:]
    if any((datetime.fromisoformat(b["timestamp"])-datetime.fromisoformat(a["timestamp"])).total_seconds()!=60
           for a,b in zip(recent,recent[1:])):return None
    closes=[float(x["close"]) for x in index_bars]
    v0=float(recent[0]["close"])
    if v0<=0 or closes[-6]<=0:return None
    vroc=(float(v["close"])/v0-1)*100
    momentum=(closes[-1]/closes[-6]-1)*10000
    avg9=ema(closes,9);avg21=ema(closes,21)
    hi=max(float(x["high"]) for x in index_bars[-6:-1])
    lo=min(float(x["low"]) for x in index_bars[-6:-1])
    call=momentum>=8 and avg9>avg21 and closes[-1]>hi
    put=momentum<=-8 and avg9<avg21 and closes[-1]<lo
    if strategy=="scalp_10_20" or strategy=="scalp_12_24":
        return "CE" if call and vroc<=.5 else "PE" if put and vroc>=-.5 else None
    if strategy=="vix_momentum_call":
        return "CE" if call and momentum>=12 and vroc<=-.4 else None
    if strategy=="vix_momentum_put":
        return "PE" if put and momentum<=-12 and vroc>=.4 else None
    if strategy=="expiry_vix_impulse" and expiry and abs(vroc)>=.5:
        return "CE" if call else "PE" if put else None
    return None

STRATEGIES={
    "vix_momentum_call":(0.05,0.30,"percent"),
    "vix_momentum_put":(0.05,0.30,"percent"),
    "scalp_10_20":(10,20,"premium_points"),
    "scalp_12_24":(12,24,"premium_points"),
    "expiry_vix_impulse":(0.05,0.40,"percent"),
}
